"""Passively prove production-issue alert delivery before release cutover.

The command creates or resumes one release-keyed synthetic issue, then only
observes durable outbox state. The candidate worker is the sole component that
may claim and deliver it. A completed canary is never reopened, so recovery can
rebuild lost evidence without repeating an external notification.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database import async_session, engine
# This command runs outside the FastAPI bootstrap that normally registers all
# ORM tables. ProductionIssueEvent has a tenant FK, so load that target before
# SQLAlchemy sorts mapper tables during the canary flush.
import app.models.tenant  # noqa: F401
from app.models.production_issue import (
    ProductionIssue,
    ProductionIssueAlertDelivery,
    ProductionIssueEvent,
)
from app.services.mcp_security import validate_public_mcp_url
from app.services.production_issue_monitor import (
    RELEASE_ALERT_CANARY_SOURCE,
    _enqueue_issue_alert_deliveries,
    resolve_production_alert_owner_ids,
)


ALERT_CANARY_EVIDENCE_SCHEMA = 1
ALERT_CANARY_CATEGORY = "observability"
ALERT_CANARY_ERROR_CODE = "ReleaseAlertCanary"
ALERT_CANARY_SUMMARY = "Astra production alert delivery canary; no customer incident"
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,49}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_CONFIG_FINGERPRINT_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")


class AlertCanaryVerificationError(RuntimeError):
    """A privacy-safe production alert canary failure."""


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    release_id: str
    version: str
    commit: str


@dataclass(frozen=True, slots=True)
class AlertCanarySnapshot:
    issue_id: uuid.UUID
    alert_epoch: int
    status: str
    alerted_at: datetime | None
    resolved_at: datetime | None
    deliveries: dict[str, dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "issue_id": str(self.issue_id),
            "alert_epoch": self.alert_epoch,
            "status": self.status,
            "alerted_at": (self.alerted_at.isoformat() if self.alerted_at else None),
            "resolved_at": (self.resolved_at.isoformat() if self.resolved_at else None),
            "deliveries": self.deliveries,
        }


def _validated_release_identity(release_id: str) -> ReleaseIdentity:
    value = str(release_id or "").strip()
    version = os.environ.get("ASTRA_RELEASE_VERSION", "").strip()
    commit = os.environ.get("ASTRA_RELEASE_COMMIT", "").strip().lower()
    effective = os.environ.get("ASTRA_RELEASE_ID", "").strip()
    if not _RELEASE_ID_RE.fullmatch(value):
        raise AlertCanaryVerificationError("invalid release identity")
    if not effective or effective != value:
        raise AlertCanaryVerificationError("candidate release identity mismatch")
    if not _VERSION_RE.fullmatch(version):
        raise AlertCanaryVerificationError("candidate release version is invalid")
    if not _COMMIT_RE.fullmatch(commit):
        raise AlertCanaryVerificationError("candidate release commit is invalid")
    return ReleaseIdentity(release_id=value, version=version, commit=commit)


def _configured_sinks(settings) -> frozenset[str]:
    sinks: set[str] = set()
    if (settings.SAAS_ADMIN_EMAIL or "").strip():
        sinks.add("notification")
    if (settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL or "").strip():
        sinks.add("webhook")
    if not sinks:
        raise AlertCanaryVerificationError("no production alert sink configured")
    return frozenset(sinks)


def _sink_configuration_fingerprint(settings) -> str:
    """Bind evidence to sink config without exposing endpoint or identity."""

    secret_value = str(settings.JWT_SECRET_KEY or "").strip()
    secret = secret_value.encode("utf-8")
    if len(secret) < 32 or secret_value == "change-me-jwt-secret":
        raise AlertCanaryVerificationError("alert fingerprint key is unsafe")
    material = "\n".join(
        (
            f"notification={(settings.SAAS_ADMIN_EMAIL or '').strip().lower()}",
            f"webhook={(settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL or '').strip()}",
        )
    ).encode("utf-8")
    return "hmac-sha256:" + hmac.new(secret, material, hashlib.sha256).hexdigest()


async def _validate_alert_configuration(
    settings,
    sinks: frozenset[str],
) -> None:
    if "notification" in sinks:
        async with async_session() as db:
            owner_ids = await resolve_production_alert_owner_ids(db, settings)
        if not owner_ids:
            raise AlertCanaryVerificationError("configured platform alert owner is unavailable")
    if "webhook" in sinks:
        try:
            await validate_public_mcp_url((settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL or "").strip())
        except Exception:
            raise AlertCanaryVerificationError("production alert webhook violates outbound URL policy") from None


def _canary_operation(release_id: str) -> str:
    return f"release.alert_canary.{release_id}"


def _canary_fingerprint(
    identity: ReleaseIdentity,
    config_fingerprint: str,
) -> str:
    """Bind the durable canary row to release code and exact sink config."""

    if _CONFIG_FINGERPRINT_RE.fullmatch(config_fingerprint) is None:
        raise AlertCanaryVerificationError("alert sink configuration fingerprint is invalid")
    material = json.dumps(
        {
            "identity_version": 2,
            "release_id": identity.release_id,
            "release_version": identity.version,
            "release_commit": identity.commit,
            "sink_config_fingerprint": config_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"astra-release-alert-canary-v2\n" + material).hexdigest()


def _canary_metadata(
    identity: ReleaseIdentity,
    config_fingerprint: str,
) -> dict[str, object]:
    if _CONFIG_FINGERPRINT_RE.fullmatch(config_fingerprint) is None:
        raise AlertCanaryVerificationError("alert sink configuration fingerprint is invalid")
    return {
        "canary_identity_version": 2,
        "release_id": identity.release_id,
        "release_version": identity.version,
        "release_commit": identity.commit,
        "sink_config_fingerprint": config_fingerprint,
    }


def _assert_canary_issue_identity(
    issue: ProductionIssue,
    identity: ReleaseIdentity,
    config_fingerprint: str,
) -> None:
    expected_metadata = _canary_metadata(identity, config_fingerprint)
    if (
        issue.fingerprint != _canary_fingerprint(identity, config_fingerprint)
        or issue.source != RELEASE_ALERT_CANARY_SOURCE
        or issue.operation != _canary_operation(identity.release_id)
        or issue.release_version != identity.version
        or int(issue.alert_epoch or 1) != 1
        or not isinstance(issue.last_metadata, dict)
        or issue.last_metadata != expected_metadata
    ):
        raise AlertCanaryVerificationError("alert canary identity drifted")


def _snapshot_from_rows(
    issue: ProductionIssue,
    deliveries: list[ProductionIssueAlertDelivery],
) -> AlertCanarySnapshot:
    return AlertCanarySnapshot(
        issue_id=issue.id,
        alert_epoch=int(issue.alert_epoch or 1),
        status=str(issue.status),
        alerted_at=issue.alerted_at,
        resolved_at=issue.resolved_at,
        deliveries={
            row.sink: {
                "status": row.status,
                "attempts": int(row.attempts or 0),
                "error_code": row.last_error_code,
                "delivered_at": (row.delivered_at.isoformat() if row.delivered_at else None),
                "idempotency_key": row.idempotency_key,
                "attribution_version": int(row.attribution_version or 0),
                "delivered_by": {
                    "worker_actor_id": (
                        str(row.delivered_by_worker_actor_id)
                        if row.delivered_by_worker_actor_id
                        else None
                    ),
                    "release_id": row.delivered_by_release_id,
                    "release_commit": row.delivered_by_release_commit,
                },
            }
            for row in deliveries
        },
    )


def _snapshot_is_delivered(
    snapshot: AlertCanarySnapshot,
    expected_sinks: frozenset[str],
    identity: ReleaseIdentity,
) -> bool:
    actual_sinks = frozenset(snapshot.deliveries)
    if actual_sinks - expected_sinks:
        raise AlertCanaryVerificationError("alert canary contains an unexpected delivery sink")
    actor_ids: set[uuid.UUID] = set()
    for sink in expected_sinks:
        delivery = snapshot.deliveries.get(sink) or {}
        if delivery.get("status") != "delivered":
            continue
        delivered_by = delivery.get("delivered_by")
        try:
            actor_id = uuid.UUID(str((delivered_by or {}).get("worker_actor_id")))
        except (AttributeError, ValueError):
            raise AlertCanaryVerificationError("delivered alert canary lacks worker attribution") from None
        if (
            delivery.get("attribution_version") != 1
            or not isinstance(delivered_by, dict)
            or delivered_by.get("release_id") != identity.release_id
            or delivered_by.get("release_commit") != identity.commit
            or delivery.get("delivered_at") is None
        ):
            raise AlertCanaryVerificationError("delivered alert canary worker identity drifted")
        actor_ids.add(actor_id)
    all_delivered = bool(
        snapshot.alerted_at is not None
        and actual_sinks == expected_sinks
        and all(
            snapshot.deliveries[sink].get("status") == "delivered"
            for sink in expected_sinks
        )
    )
    if all_delivered and len(actor_ids) != 1:
        raise AlertCanaryVerificationError("alert canary was delivered by mixed workers")
    return all_delivered


async def _create_or_resume_canary(
    identity: ReleaseIdentity,
    settings,
    expected_sinks: frozenset[str],
    config_fingerprint: str,
) -> uuid.UUID:
    """Insert once, or resume the exact release/config epoch without reopen."""

    now = datetime.now(timezone.utc)
    fingerprint = _canary_fingerprint(identity, config_fingerprint)
    metadata = _canary_metadata(identity, config_fingerprint)
    candidate_id = uuid.uuid4()
    async with async_session() as db:
        inserted_id = (
            await db.execute(
                pg_insert(ProductionIssue)
                .values(
                    id=candidate_id,
                    fingerprint=fingerprint,
                    category=ALERT_CANARY_CATEGORY,
                    severity="warning",
                    status="open",
                    source=RELEASE_ALERT_CANARY_SOURCE,
                    error_code=ALERT_CANARY_ERROR_CODE,
                    summary=ALERT_CANARY_SUMMARY,
                    route=None,
                    operation=_canary_operation(identity.release_id),
                    event_count=1,
                    first_seen_at=now,
                    last_seen_at=now,
                    release_version=identity.version,
                    last_metadata=metadata,
                    alert_epoch=1,
                    alert_attempts=0,
                )
                .on_conflict_do_nothing(index_elements=[ProductionIssue.fingerprint])
                .returning(ProductionIssue.id)
            )
        ).scalar_one_or_none()
        issue = (
            await db.execute(
                select(ProductionIssue).where(ProductionIssue.fingerprint == fingerprint).with_for_update()
            )
        ).scalar_one_or_none()
        if issue is None:
            raise AlertCanaryVerificationError("alert canary create-or-resume failed")
        issue_id = issue.id
        if inserted_id is not None:
            db.add(
                ProductionIssueEvent(
                    issue_id=issue.id,
                    tenant_id=None,
                    user_id=None,
                    agent_id=None,
                    trace_id=None,
                    severity="warning",
                    route=None,
                    operation=_canary_operation(identity.release_id),
                    metadata_json=metadata,
                    created_at=now,
                )
            )
        _assert_canary_issue_identity(issue, identity, config_fingerprint)

        deliveries = list(
            (
                await db.execute(
                    select(ProductionIssueAlertDelivery)
                    .where(
                        ProductionIssueAlertDelivery.issue_id == issue.id,
                        ProductionIssueAlertDelivery.alert_epoch == issue.alert_epoch,
                    )
                    .order_by(ProductionIssueAlertDelivery.sink)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if issue.status == "resolved":
            snapshot = _snapshot_from_rows(issue, deliveries)
            if not _snapshot_is_delivered(snapshot, expected_sinks, identity):
                raise AlertCanaryVerificationError("resolved alert canary lacks complete delivery evidence")
            await db.rollback()
            return issue_id
        if issue.status != "open":
            raise AlertCanaryVerificationError("alert canary is in a non-resumable state")

        await _enqueue_issue_alert_deliveries(db, issue, settings=settings)
        await db.flush()
        actual_sinks = frozenset(
            (
                await db.execute(
                    select(ProductionIssueAlertDelivery.sink).where(
                        ProductionIssueAlertDelivery.issue_id == issue.id,
                        ProductionIssueAlertDelivery.alert_epoch == issue.alert_epoch,
                    )
                )
            )
            .scalars()
            .all()
        )
        if actual_sinks != expected_sinks:
            raise AlertCanaryVerificationError("alert canary sink set does not match release configuration")
        await db.commit()
        return issue_id


async def _verify_and_resolve_if_delivered(
    issue_id: uuid.UUID,
    expected_sinks: frozenset[str],
    identity: ReleaseIdentity,
    config_fingerprint: str,
) -> tuple[bool, AlertCanarySnapshot]:
    """Lock Issue before Delivery rows, matching the worker outbox lock order."""

    async with async_session() as db:
        issue = await db.get(ProductionIssue, issue_id, with_for_update=True)
        if issue is None:
            raise AlertCanaryVerificationError("alert canary issue is missing")
        _assert_canary_issue_identity(issue, identity, config_fingerprint)
        deliveries = list(
            (
                await db.execute(
                    select(ProductionIssueAlertDelivery)
                    .where(
                        ProductionIssueAlertDelivery.issue_id == issue.id,
                        ProductionIssueAlertDelivery.alert_epoch == issue.alert_epoch,
                    )
                    .order_by(ProductionIssueAlertDelivery.sink)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        snapshot = _snapshot_from_rows(issue, deliveries)
        delivered = _snapshot_is_delivered(snapshot, expected_sinks, identity)
        if issue.status == "resolved":
            if not delivered or issue.resolved_at is None:
                raise AlertCanaryVerificationError("resolved alert canary evidence is incomplete")
            await db.rollback()
            return True, snapshot
        if issue.status != "open":
            raise AlertCanaryVerificationError("alert canary is in a non-verifiable state")
        if delivered:
            now = datetime.now(timezone.utc)
            issue.status = "resolved"
            issue.resolved_at = now
            issue.acknowledged_at = now
            await db.commit()
            return True, AlertCanarySnapshot(
                issue_id=snapshot.issue_id,
                alert_epoch=snapshot.alert_epoch,
                status="resolved",
                alerted_at=snapshot.alerted_at,
                resolved_at=now,
                deliveries=snapshot.deliveries,
            )
        await db.rollback()
        return False, snapshot


async def verify_production_alert_configuration(
    *,
    release_id: str,
) -> tuple[ReleaseIdentity, frozenset[str], str]:
    """Read-only preflight for sink, owner, URL policy, and release identity."""

    identity = _validated_release_identity(release_id)
    settings = get_settings()
    if not settings.PRODUCTION_ISSUE_MONITOR_ENABLED:
        raise AlertCanaryVerificationError("production issue monitor is disabled")
    expected_sinks = _configured_sinks(settings)
    await _validate_alert_configuration(settings, expected_sinks)
    return identity, expected_sinks, _sink_configuration_fingerprint(settings)


async def verify_production_issue_alerts(
    *,
    release_id: str,
    timeout_seconds: int = 180,
    poll_interval_seconds: float = 1.0,
) -> tuple[ReleaseIdentity, str, AlertCanarySnapshot]:
    """Wait for the sole candidate worker to deliver a resumable canary."""

    timeout = min(max(int(timeout_seconds), 60), 300)
    poll_interval = min(max(float(poll_interval_seconds), 0.25), 5.0)
    last_snapshot: AlertCanarySnapshot | None = None
    try:
        async with asyncio.timeout(timeout):
            identity, expected_sinks, config_fingerprint = (
                await verify_production_alert_configuration(
                    release_id=release_id,
                )
            )
            settings = get_settings()
            issue_id = await _create_or_resume_canary(
                identity,
                settings,
                expected_sinks,
                config_fingerprint,
            )
            while True:
                delivered, last_snapshot = await _verify_and_resolve_if_delivered(
                    issue_id,
                    expected_sinks,
                    identity,
                    config_fingerprint,
                )
                if delivered:
                    return identity, config_fingerprint, last_snapshot
                await asyncio.sleep(poll_interval)
    except TimeoutError:
        pass

    error_codes = (
        sorted(
            {
                str(delivery.get("error_code"))
                for delivery in last_snapshot.deliveries.values()
                if delivery.get("error_code")
            }
        )
        if last_snapshot
        else []
    )
    suffix = f" ({','.join(error_codes)})" if error_codes else ""
    raise AlertCanaryVerificationError(f"production alert canary delivery timed out{suffix}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the production issue alert delivery pipeline",
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    try:
        if args.preflight_only:
            try:
                identity, configured_sinks, config_fingerprint = await asyncio.wait_for(
                    verify_production_alert_configuration(
                        release_id=args.release_id,
                    ),
                    timeout=30,
                )
            except TimeoutError:
                raise AlertCanaryVerificationError("production alert configuration preflight timed out") from None
            payload: dict[str, object] = {
                "ok": True,
                "schema_version": ALERT_CANARY_EVIDENCE_SCHEMA,
                "mode": "preflight",
                "release_id": identity.release_id,
                "release_version": identity.version,
                "release_commit": identity.commit,
                "configured_sinks": sorted(configured_sinks),
                "sink_config_fingerprint": config_fingerprint,
            }
        else:
            identity, config_fingerprint, snapshot = await verify_production_issue_alerts(
                release_id=args.release_id,
                timeout_seconds=args.timeout_seconds,
            )
            payload = {
                "ok": True,
                "schema_version": ALERT_CANARY_EVIDENCE_SCHEMA,
                "mode": "delivery",
                "release_id": identity.release_id,
                "release_version": identity.version,
                "release_commit": identity.commit,
                "sink_config_fingerprint": config_fingerprint,
                **snapshot.as_dict(),
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except AlertCanaryVerificationError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": type(exc).__name__,
                    "reason": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
