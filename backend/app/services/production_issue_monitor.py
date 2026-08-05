"""Privacy-safe production issue capture, aggregation, alerting and retention."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.notification import Notification
from app.models.production_issue import (
    ProductionIssue,
    ProductionIssueAlertDelivery,
    ProductionIssueEvent,
)
from app.models.user import Identity, User


_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
_LONG_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d{6,}(?![A-Za-z])")
_SECRETISH_RE = re.compile(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|api[_ -]?key\s*[:=]\s*\S+)")
_ALERT_WORKER_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,160}")
_ALERT_WORKER_RELEASE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_ALLOWED_SEVERITIES = {"warning", "error", "critical"}
_FAILED_CAPTURE_QUEUE_LIMIT = 1000
PRODUCTION_ISSUE_MONITOR_MAX_CONSECUTIVE_FAILURES = 5
PRODUCTION_ISSUE_ALERT_CLAIM_LEASE_SECONDS = 90
PRODUCTION_ISSUE_ALERT_BATCH_SIZE = 20
PRODUCTION_ISSUE_ALERT_MAX_CONCURRENCY = 4
PRODUCTION_ISSUE_ALERT_WEBHOOK_TOTAL_TIMEOUT_SECONDS = 12
RELEASE_ALERT_CANARY_SOURCE = "release_alert_canary"
_failed_capture_queue: deque[dict[str, Any]] = deque(maxlen=_FAILED_CAPTURE_QUEUE_LIMIT)
_monitor_started_at: datetime | None = None
_monitor_last_db_loop_success_at: datetime | None = None
_monitor_oldest_due_delivery_age_seconds = 0.0
_monitor_consecutive_db_failures = 0
_monitor_interval_seconds = 30
_ALLOWED_METADATA_KEYS = {
    "status_code",
    "error_type",
    "reason_code",
    "provider",
    "model",
    "modality",
    "saas_tier",
    "task_id",
    "provider_task_id",
    "reservation_id",
    "settlement_credits",
    "http_method",
    "duration_ms",
    "attempt_count",
    "consecutive_error_count",
    "component",
    "file",
    "line",
    "column",
    "close_code",
    "release_version",
    "active_credentials",
    "credentials_with_provider_evidence",
}


@dataclass(frozen=True)
class AlertWorkerIdentity:
    actor_id: uuid.UUID
    release_id: str
    release_commit: str


@dataclass(frozen=True)
class AlertDeliveryClaim:
    delivery_id: uuid.UUID
    issue_id: uuid.UUID
    alert_epoch: int
    sink: str
    idempotency_key: str
    claim_token: uuid.UUID
    payload: dict[str, Any]
    worker_identity: AlertWorkerIdentity


def _current_alert_worker_identity() -> AlertWorkerIdentity:
    """Return immutable process identity; release workers fail closed on drift."""

    raw_actor_id = os.environ.get("ASTRA_ALERT_WORKER_ACTOR_ID", "").strip()
    release_id = os.environ.get("ASTRA_RELEASE_ID", "").strip()
    release_commit = os.environ.get("ASTRA_RELEASE_COMMIT", "").strip().lower()
    if not any((raw_actor_id, release_id, release_commit)):
        # Developer/test processes do not participate in release canary proof.
        # Give them a valid, explicit identity without weakening a partially
        # configured release process, which must fail closed below.
        return AlertWorkerIdentity(
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, "astra:local-alert-worker"),
            release_id="local",
            release_commit="0" * 40,
        )
    try:
        actor_id = uuid.UUID(raw_actor_id)
    except (AttributeError, ValueError) as exc:
        raise RuntimeError("invalid production alert worker actor identity") from exc
    if (
        _ALERT_WORKER_RELEASE_ID_RE.fullmatch(release_id) is None
        or _ALERT_WORKER_RELEASE_COMMIT_RE.fullmatch(release_commit) is None
    ):
        raise RuntimeError("invalid production alert worker release identity")
    return AlertWorkerIdentity(
        actor_id=actor_id,
        release_id=release_id,
        release_commit=release_commit,
    )


def _safe_operational_text(value: Any, max_length: int) -> str:
    return _SECRETISH_RE.sub(
        "[redacted]",
        str(value).replace("\n", " ").replace("\r", " "),
    ).strip()[:max_length]


def normalize_issue_route(route: str | None) -> str | None:
    """Remove query strings and high-cardinality path identifiers."""

    if not route:
        return None
    normalized = _safe_operational_text(str(route).split("?", 1)[0], 500)
    normalized = _UUID_RE.sub("{uuid}", normalized)
    normalized = _LONG_NUMBER_RE.sub("{id}", normalized)
    return normalized or None


def sanitize_issue_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Allowlist operational metadata; prompts, bodies and credentials never pass."""

    if not metadata:
        return {}
    clean: dict[str, Any] = {}
    for raw_key, raw_value in metadata.items():
        key = str(raw_key).strip().lower()
        if key not in _ALLOWED_METADATA_KEYS:
            continue
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            clean[key] = raw_value
        elif isinstance(raw_value, str):
            clean[key] = _SECRETISH_RE.sub("[redacted]", raw_value.replace("\n", " "))[:300]
        elif isinstance(raw_value, (list, tuple)):
            clean[key] = [
                _SECRETISH_RE.sub("[redacted]", str(value).replace("\n", " "))[:100]
                for value in raw_value[:20]
                if isinstance(value, (str, int, float, bool))
            ]
    return clean


def issue_fingerprint(
    *,
    source: str,
    category: str,
    error_code: str | None,
    route: str | None,
    operation: str | None,
) -> str:
    """Stable cross-tenant grouping without user content or identity."""

    key = "|".join(
        (
            source.strip().lower(),
            category.strip().lower(),
            (error_code or "unknown").strip().lower(),
            normalize_issue_route(route) or "",
            (operation or "").strip().lower(),
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _safe_summary(summary: str) -> str:
    value = _safe_operational_text(summary, 500)
    return (value or "Production operation failed")[:500]


def _queue_failed_capture(
    *,
    source: str,
    category: str,
    summary: str,
    severity: str,
    error_code: str | None,
    route: str | None,
    operation: str | None,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    trace_id: str | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Retain only sanitized diagnostics while the primary DB is unavailable."""
    if len(_failed_capture_queue) >= _FAILED_CAPTURE_QUEUE_LIMIT:
        logger.error(
            "[production-issues] fallback queue full capacity={}",
            _FAILED_CAPTURE_QUEUE_LIMIT,
        )
    _failed_capture_queue.append(
        {
            "source": _safe_operational_text(source, 64).lower() or "unknown",
            "category": _safe_operational_text(category, 64).lower() or "unknown",
            "summary": _safe_summary(summary),
            "severity": severity if severity in _ALLOWED_SEVERITIES else "error",
            "error_code": _safe_operational_text(error_code, 100) if error_code else None,
            "route": normalize_issue_route(route),
            "operation": _safe_operational_text(operation, 100) if operation else None,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "trace_id": _safe_operational_text(trace_id, 64) if trace_id else None,
            "metadata": sanitize_issue_metadata(metadata),
        }
    )


async def record_production_issue(
    *,
    source: str,
    category: str,
    summary: str,
    severity: str = "error",
    error_code: str | None = None,
    route: str | None = None,
    operation: str | None = None,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Persist one occurrence and update its rollup; never break the caller."""

    try:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        severity = severity if severity in _ALLOWED_SEVERITIES else "error"
        normalized_route = normalize_issue_route(route)
        normalized_operation = _safe_operational_text(operation, 100) if operation else None
        normalized_source = _safe_operational_text(source, 64).lower() or "unknown"
        normalized_category = _safe_operational_text(category, 64).lower() or "unknown"
        normalized_error_code = _safe_operational_text(error_code, 100) if error_code else None
        clean_metadata = sanitize_issue_metadata(metadata)
        release_version = str(clean_metadata.get("release_version") or settings.APP_VERSION)[:50]
        fingerprint = issue_fingerprint(
            source=normalized_source,
            category=normalized_category,
            error_code=normalized_error_code,
            route=normalized_route,
            operation=normalized_operation,
        )
        new_issue_id = uuid.uuid4()
        if severity == "critical":
            merged_severity = "critical"
        elif severity == "error":
            merged_severity = case(
                (ProductionIssue.severity == "critical", "critical"),
                else_="error",
            )
        else:
            merged_severity = ProductionIssue.severity
        statement = (
            pg_insert(ProductionIssue)
            .values(
                id=new_issue_id,
                fingerprint=fingerprint,
                category=normalized_category,
                severity=severity,
                status="open",
                source=normalized_source,
                error_code=normalized_error_code,
                summary=_safe_summary(summary),
                route=normalized_route,
                operation=normalized_operation,
                event_count=1,
                first_seen_at=now,
                last_seen_at=now,
                last_trace_id=(str(trace_id)[:64] if trace_id else None),
                release_version=release_version,
                last_metadata=clean_metadata or None,
                alert_epoch=1,
            )
            .on_conflict_do_update(
                index_elements=[ProductionIssue.fingerprint],
                set_={
                    "severity": merged_severity,
                    "status": case(
                        (ProductionIssue.status == "resolved", "open"),
                        else_=ProductionIssue.status,
                    ),
                    "summary": _safe_summary(summary),
                    "event_count": ProductionIssue.event_count + 1,
                    "last_seen_at": now,
                    "last_trace_id": str(trace_id)[:64] if trace_id else None,
                    "release_version": release_version,
                    "last_metadata": clean_metadata or None,
                    "updated_at": now,
                    "resolved_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.resolved_at,
                    ),
                    "resolution_reason": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.resolution_reason,
                    ),
                    "auto_resolved": case(
                        (ProductionIssue.status == "resolved", False),
                        else_=ProductionIssue.auto_resolved,
                    ),
                    "acknowledged_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.acknowledged_at,
                    ),
                    "alerted_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.alerted_at,
                    ),
                    "alert_epoch": case(
                        (
                            ProductionIssue.status == "resolved",
                            ProductionIssue.alert_epoch + 1,
                        ),
                        else_=ProductionIssue.alert_epoch,
                    ),
                    "alert_attempts": case(
                        (ProductionIssue.status == "resolved", 0),
                        else_=ProductionIssue.alert_attempts,
                    ),
                    "alert_next_attempt_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.alert_next_attempt_at,
                    ),
                    "alert_last_error_code": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.alert_last_error_code,
                    ),
                    "alert_notification_sent_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.alert_notification_sent_at,
                    ),
                },
            )
            .returning(ProductionIssue)
        )
        async with async_session() as db:
            if tenant_id is None and agent_id is not None:
                tenant_id = await db.scalar(select(Agent.tenant_id).where(Agent.id == agent_id))
            issue = (await db.execute(statement)).scalar_one()
            issue_id = issue.id
            db.add(
                ProductionIssueEvent(
                    issue_id=issue_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    trace_id=str(trace_id)[:64] if trace_id else None,
                    severity=severity,
                    route=normalized_route,
                    operation=normalized_operation,
                    metadata_json=clean_metadata or None,
                    created_at=now,
                )
            )
            if issue_requires_alert(
                issue,
                threshold=max(int(settings.PRODUCTION_ISSUE_ALERT_THRESHOLD), 1),
            ):
                await _enqueue_issue_alert_deliveries(db, issue, settings=settings)
            await db.commit()
        return issue_id
    except Exception as exc:
        logger.error("[production-issues] capture failed error_type={}", type(exc).__name__)
        _queue_failed_capture(
            source=source,
            category=category,
            summary=summary,
            severity=severity,
            error_code=error_code,
            route=route,
            operation=operation,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            trace_id=trace_id,
            metadata=metadata,
        )
        return None


async def flush_failed_production_issue_captures() -> int:
    """Replay transiently failed captures after database capacity recovers."""
    flushed = 0
    pending = len(_failed_capture_queue)
    for _ in range(pending):
        payload = _failed_capture_queue.popleft()
        issue_id = await record_production_issue(**payload)
        if issue_id is None:
            # record_production_issue re-queued the sanitized payload. Stop so
            # an unavailable database cannot create a tight retry loop.
            break
        flushed += 1
    return flushed


def issue_requires_alert(issue: ProductionIssue, threshold: int) -> bool:
    return bool(
        issue.status == "open"
        and issue.alerted_at is None
        and (issue.severity == "critical" or issue.event_count >= max(threshold, 1))
    )


def _production_issue_alert_payload(issue: ProductionIssue) -> dict[str, Any]:
    """Freeze a privacy-safe webhook payload for one alert epoch."""

    is_canary = issue.source == RELEASE_ALERT_CANARY_SOURCE
    return {
        "issue_id": str(issue.id),
        "alert_epoch": int(issue.alert_epoch or 1),
        "severity": issue.severity,
        "category": issue.category,
        "summary": _safe_summary(issue.summary),
        "route": normalize_issue_route(issue.route),
        "operation": _safe_operational_text(issue.operation, 100) if issue.operation else None,
        "event_count": int(issue.event_count or 0),
        "last_seen_at": issue.last_seen_at.isoformat(),
        "release_version": _safe_operational_text(issue.release_version, 50) if issue.release_version else None,
        "event_kind": "release_alert_canary" if is_canary else "production_issue",
        "is_canary": is_canary,
    }


async def _enqueue_issue_alert_deliveries(
    db,
    issue: ProductionIssue,
    *,
    settings,
) -> None:
    """Insert each required alert sink once in the aggregation transaction."""

    epoch = int(issue.alert_epoch or 1)
    existing_sinks = set(
        (
            await db.execute(
                select(ProductionIssueAlertDelivery.sink).where(
                    ProductionIssueAlertDelivery.issue_id == issue.id,
                    ProductionIssueAlertDelivery.alert_epoch == epoch,
                )
            )
        )
        .scalars()
        .all()
    )
    # Freeze the real sink set once an epoch has been enqueued. This prevents a
    # later configuration change from silently adding a new required delivery
    # to an incident that is already being processed.
    real_existing_sinks = existing_sinks - {"missing_sink"}
    if real_existing_sinks:
        return

    sinks: list[str] = []
    if (settings.SAAS_ADMIN_EMAIL or "").strip():
        sinks.append("notification")
    if (settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL or "").strip():
        sinks.append("webhook")
    if not sinks:
        sinks.append("missing_sink")
    elif "missing_sink" in existing_sinks:
        # A missing-sink row is an operational placeholder, not a real
        # delivery. Replace it once an operator configures a usable sink.
        await db.execute(
            delete(ProductionIssueAlertDelivery).where(
                ProductionIssueAlertDelivery.issue_id == issue.id,
                ProductionIssueAlertDelivery.alert_epoch == epoch,
                ProductionIssueAlertDelivery.sink == "missing_sink",
            )
        )
    payload = _production_issue_alert_payload(issue)
    for sink in sinks:
        idempotency_key = f"production-issue:{issue.id}:{epoch}:{sink}"
        await db.execute(
            pg_insert(ProductionIssueAlertDelivery)
            .values(
                id=uuid.uuid4(),
                issue_id=issue.id,
                alert_epoch=epoch,
                sink=sink,
                idempotency_key=idempotency_key,
                status="pending",
                payload_snapshot=payload,
                attribution_version=1,
                attempts=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ProductionIssueAlertDelivery.issue_id,
                    ProductionIssueAlertDelivery.alert_epoch,
                    ProductionIssueAlertDelivery.sink,
                ]
            )
        )


def _production_issue_alert_log_level(
    severity: str,
    *,
    is_canary: bool = False,
) -> str:
    """Keep warning-class incidents out of the platform error log stream."""
    if is_canary:
        return "info"
    return "warning" if severity == "warning" else "error"


def _production_issue_notification_ref_id(
    issue_id: uuid.UUID,
    alert_epoch: int,
) -> uuid.UUID:
    """Stable notification identity that remains distinct across reopen epochs."""

    return uuid.uuid5(issue_id, f"astra-production-alert:{alert_epoch}")


def _production_issue_notification(
    issue: ProductionIssue,
    user_id: uuid.UUID,
    *,
    payload: dict[str, Any] | None = None,
    alert_epoch: int | None = None,
) -> Notification:
    snapshot = payload if payload is not None else {}
    is_canary = bool(snapshot.get("is_canary")) or (getattr(issue, "source", None) == RELEASE_ALERT_CANARY_SOURCE)
    severity = str(snapshot.get("severity") or issue.severity)
    level = {
        "critical": "严重",
        "error": "错误",
        "warning": "警告",
    }.get(severity, "错误")
    summary = _safe_summary(str(snapshot.get("summary") or issue.summary))
    event_count = int(snapshot.get("event_count") or issue.event_count or 0)
    location = (
        snapshot.get("route")
        or snapshot.get("operation")
        or snapshot.get("category")
        or issue.route
        or issue.operation
        or issue.category
    )
    frozen_epoch = int(alert_epoch or snapshot.get("alert_epoch") or getattr(issue, "alert_epoch", 1) or 1)
    return Notification(
        user_id=user_id,
        type="system",
        title=("[演练] 生产告警通道验证" if is_canary else f"[{level}] 生产问题告警"),
        body=f"{summary} · {event_count} 次 · {location}",
        link="/admin/saas?tab=production-issues",
        ref_id=_production_issue_notification_ref_id(
            issue.id,
            frozen_epoch,
        ),
        sender_name="Astra Monitor",
    )


async def resolve_production_alert_owner_ids(db, settings) -> list[uuid.UUID]:
    """Resolve active tenant users owned by the configured platform admin."""

    owner_email = (settings.SAAS_ADMIN_EMAIL or "").strip().lower()
    if not owner_email:
        return []
    result = await db.execute(
        select(User.id)
        .join(Identity, User.identity_id == Identity.id)
        .where(
            func.lower(Identity.email) == owner_email,
            Identity.is_active.is_(True),
            Identity.is_platform_admin.is_(True),
            User.is_active.is_(True),
        )
        .order_by(User.id)
    )
    return list(dict.fromkeys(result.scalars().all()))


def _alert_retry_delay_seconds(attempts: int) -> int:
    return min(30 * (2 ** min(max(attempts, 1) - 1, 7)), 3600)


def _schedule_alert_retry(
    target,
    *,
    now: datetime,
    error_code: str,
) -> None:
    """Persist bounded exponential backoff without marking delivery complete."""

    target.alert_attempts = int(target.alert_attempts or 0) + 1
    target.alert_next_attempt_at = now + timedelta(seconds=_alert_retry_delay_seconds(target.alert_attempts))
    target.alert_last_error_code = _safe_operational_text(error_code, 100)


async def _claim_production_issue_alert_deliveries() -> list[AlertDeliveryClaim]:
    """Claim a bounded batch and commit before any external operation."""

    global _monitor_oldest_due_delivery_age_seconds

    settings = get_settings()
    worker_identity = _current_alert_worker_identity()
    threshold = max(int(settings.PRODUCTION_ISSUE_ALERT_THRESHOLD), 1)
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=PRODUCTION_ISSUE_ALERT_CLAIM_LEASE_SECONDS)
    claims: list[AlertDeliveryClaim] = []
    async with async_session() as db:
        # Backfill eligible rollups created before this outbox contract. New
        # captures enqueue in record_production_issue's own transaction.
        issue_result = await db.execute(
            select(ProductionIssue)
            .where(
                ProductionIssue.status == "open",
                ProductionIssue.alerted_at.is_(None),
                or_(
                    ProductionIssue.source == RELEASE_ALERT_CANARY_SOURCE,
                    ProductionIssue.severity == "critical",
                    ProductionIssue.event_count >= threshold,
                ),
            )
            .order_by(
                case(
                    (
                        ProductionIssue.source == RELEASE_ALERT_CANARY_SOURCE,
                        0,
                    ),
                    else_=1,
                ),
                ProductionIssue.last_seen_at.asc(),
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        issues = list(issue_result.scalars().all())
        for issue in issues:
            await _enqueue_issue_alert_deliveries(db, issue, settings=settings)
        await db.flush()

        # Repair the narrow crash/concurrency window where every sink row was
        # committed as delivered but the aggregate marker was not projected.
        # The issue locks above serialize this reconciliation with finalizers.
        for issue in issues:
            total = await db.scalar(
                select(func.count())
                .select_from(ProductionIssueAlertDelivery)
                .where(
                    ProductionIssueAlertDelivery.issue_id == issue.id,
                    ProductionIssueAlertDelivery.alert_epoch == issue.alert_epoch,
                )
            )
            outstanding = await db.scalar(
                select(func.count())
                .select_from(ProductionIssueAlertDelivery)
                .where(
                    ProductionIssueAlertDelivery.issue_id == issue.id,
                    ProductionIssueAlertDelivery.alert_epoch == issue.alert_epoch,
                    ProductionIssueAlertDelivery.status != "delivered",
                )
            )
            if int(total or 0) > 0 and int(outstanding or 0) == 0:
                issue.alerted_at = now
                issue.alert_attempts = 0
                issue.alert_next_attempt_at = None
                issue.alert_last_error_code = None

        due_time = func.coalesce(
            ProductionIssueAlertDelivery.next_attempt_at,
            ProductionIssueAlertDelivery.created_at,
        )
        oldest_due = await db.scalar(
            select(func.min(due_time))
            .select_from(ProductionIssueAlertDelivery)
            .join(
                ProductionIssue,
                ProductionIssue.id == ProductionIssueAlertDelivery.issue_id,
            )
            .where(
                ProductionIssue.status == "open",
                ProductionIssue.alerted_at.is_(None),
                ProductionIssue.alert_epoch == ProductionIssueAlertDelivery.alert_epoch,
                or_(
                    and_(
                        ProductionIssueAlertDelivery.status == "pending",
                        or_(
                            ProductionIssueAlertDelivery.next_attempt_at.is_(None),
                            ProductionIssueAlertDelivery.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        ProductionIssueAlertDelivery.status == "delivering",
                        ProductionIssueAlertDelivery.claimed_at <= stale_cutoff,
                    ),
                ),
            )
        )
        if oldest_due is None:
            _monitor_oldest_due_delivery_age_seconds = 0.0
        else:
            _monitor_oldest_due_delivery_age_seconds = max(
                (now - oldest_due).total_seconds(),
                0.0,
            )

        # Only claim deliveries whose parent rows were locked above. This
        # keeps every monitor transaction on the same Issue -> Delivery lock
        # order and bounds the parent working set.
        locked_issue_ids = [issue.id for issue in issues]
        if not locked_issue_ids:
            await db.commit()
            return claims

        result = await db.execute(
            select(ProductionIssueAlertDelivery)
            .join(
                ProductionIssue,
                ProductionIssue.id == ProductionIssueAlertDelivery.issue_id,
            )
            .where(
                ProductionIssueAlertDelivery.issue_id.in_(locked_issue_ids),
                ProductionIssue.status == "open",
                ProductionIssue.alerted_at.is_(None),
                ProductionIssue.alert_epoch == ProductionIssueAlertDelivery.alert_epoch,
                or_(
                    and_(
                        ProductionIssueAlertDelivery.status == "pending",
                        or_(
                            ProductionIssueAlertDelivery.next_attempt_at.is_(None),
                            ProductionIssueAlertDelivery.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        ProductionIssueAlertDelivery.status == "delivering",
                        ProductionIssueAlertDelivery.claimed_at <= stale_cutoff,
                    ),
                ),
            )
            .order_by(
                case(
                    (
                        ProductionIssue.source == RELEASE_ALERT_CANARY_SOURCE,
                        0,
                    ),
                    else_=1,
                ),
                ProductionIssueAlertDelivery.next_attempt_at.asc().nullsfirst(),
                ProductionIssueAlertDelivery.created_at.asc(),
            )
            .limit(PRODUCTION_ISSUE_ALERT_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        for delivery in result.scalars().all():
            claim_token = uuid.uuid4()
            delivery.status = "delivering"
            delivery.attribution_version = 1
            delivery.claim_token = claim_token
            delivery.claimed_at = now
            delivery.claim_worker_actor_id = worker_identity.actor_id
            delivery.claim_worker_release_id = worker_identity.release_id
            delivery.claim_worker_release_commit = worker_identity.release_commit
            delivery.delivered_by_worker_actor_id = None
            delivery.delivered_by_release_id = None
            delivery.delivered_by_release_commit = None
            delivery.attempts = int(delivery.attempts or 0) + 1
            delivery.next_attempt_at = now + timedelta(seconds=PRODUCTION_ISSUE_ALERT_CLAIM_LEASE_SECONDS)
            delivery.last_error_code = None
            claims.append(
                AlertDeliveryClaim(
                    delivery_id=delivery.id,
                    issue_id=delivery.issue_id,
                    alert_epoch=delivery.alert_epoch,
                    sink=delivery.sink,
                    idempotency_key=delivery.idempotency_key,
                    claim_token=claim_token,
                    payload=dict(delivery.payload_snapshot or {}),
                    worker_identity=worker_identity,
                )
            )
        await db.commit()
    return claims


async def _finish_alert_delivery_row(
    db,
    issue: ProductionIssue,
    delivery: ProductionIssueAlertDelivery,
    *,
    claim: AlertDeliveryClaim,
    success: bool,
    error_code: str | None = None,
    notification_sent: bool = False,
) -> bool:
    """Finish one claimed row after its parent Issue has been locked."""

    now = datetime.now(timezone.utc)
    if success:
        delivery.attribution_version = 1
        delivery.status = "delivered"
        delivery.delivered_at = now
        delivery.delivered_by_worker_actor_id = claim.worker_identity.actor_id
        delivery.delivered_by_release_id = claim.worker_identity.release_id
        delivery.delivered_by_release_commit = claim.worker_identity.release_commit
        delivery.next_attempt_at = None
        delivery.last_error_code = None
        delivery.claim_token = None
        delivery.claimed_at = None
        delivery.claim_worker_actor_id = None
        delivery.claim_worker_release_id = None
        delivery.claim_worker_release_commit = None
        if delivery.sink == "notification" and notification_sent:
            issue.alert_notification_sent_at = now
        await db.flush()
        outstanding = await db.scalar(
            select(func.count())
            .select_from(ProductionIssueAlertDelivery)
            .where(
                ProductionIssueAlertDelivery.issue_id == delivery.issue_id,
                ProductionIssueAlertDelivery.alert_epoch == delivery.alert_epoch,
                ProductionIssueAlertDelivery.status != "delivered",
            )
        )
        if (
            int(outstanding or 0) == 0
            and issue.status == "open"
            and int(issue.alert_epoch or 1) == delivery.alert_epoch
            and issue.alerted_at is None
        ):
            issue.alerted_at = now
            issue.alert_attempts = 0
            issue.alert_next_attempt_at = None
            issue.alert_last_error_code = None
            return True
        return False

    delivery.status = "pending"
    delivery.claim_token = None
    delivery.claimed_at = None
    delivery.claim_worker_actor_id = None
    delivery.claim_worker_release_id = None
    delivery.claim_worker_release_commit = None
    delivery.delivered_by_worker_actor_id = None
    delivery.delivered_by_release_id = None
    delivery.delivered_by_release_commit = None
    delivery.last_error_code = _safe_operational_text(
        error_code or "UnknownAlertDeliveryError",
        100,
    )
    delivery.next_attempt_at = now + timedelta(seconds=_alert_retry_delay_seconds(delivery.attempts))
    issue.alert_attempts = max(
        int(issue.alert_attempts or 0),
        int(delivery.attempts or 0),
    )
    issue.alert_next_attempt_at = delivery.next_attempt_at
    issue.alert_last_error_code = delivery.last_error_code
    return False


def _cancel_obsolete_alert_delivery_row(
    delivery: ProductionIssueAlertDelivery,
) -> None:
    """Retire an invalid epoch without claiming that a sink was delivered."""

    delivery.attribution_version = 1
    delivery.status = "cancelled"
    delivery.claim_token = None
    delivery.claimed_at = None
    delivery.claim_worker_actor_id = None
    delivery.claim_worker_release_id = None
    delivery.claim_worker_release_commit = None
    delivery.delivered_at = None
    delivery.delivered_by_worker_actor_id = None
    delivery.delivered_by_release_id = None
    delivery.delivered_by_release_commit = None
    delivery.next_attempt_at = None
    delivery.last_error_code = "ObsoleteAlertEpoch"


def _claimed_delivery_predicates(claim: AlertDeliveryClaim) -> tuple[Any, ...]:
    """Fence finalization by both opaque token and immutable worker identity."""

    return (
        ProductionIssueAlertDelivery.id == claim.delivery_id,
        ProductionIssueAlertDelivery.status == "delivering",
        ProductionIssueAlertDelivery.attribution_version == 1,
        ProductionIssueAlertDelivery.claim_token == claim.claim_token,
        ProductionIssueAlertDelivery.claim_worker_actor_id
        == claim.worker_identity.actor_id,
        ProductionIssueAlertDelivery.claim_worker_release_id
        == claim.worker_identity.release_id,
        ProductionIssueAlertDelivery.claim_worker_release_commit
        == claim.worker_identity.release_commit,
    )


async def _finalize_alert_delivery(
    claim: AlertDeliveryClaim,
    *,
    success: bool,
    error_code: str | None = None,
) -> bool:
    async with async_session() as db:
        # Claiming and every finalizer lock the parent before the child. The
        # common order prevents issue->delivery / delivery->issue deadlocks.
        issue = await db.get(
            ProductionIssue,
            claim.issue_id,
            with_for_update=True,
        )
        if issue is None:
            return False
        delivery = (
            await db.execute(
                select(ProductionIssueAlertDelivery)
                .where(*_claimed_delivery_predicates(claim))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if delivery is None:
            return False
        if issue.status != "open" or int(issue.alert_epoch or 1) != claim.alert_epoch:
            _cancel_obsolete_alert_delivery_row(delivery)
            await db.commit()
            return False
        issue_alerted = await _finish_alert_delivery_row(
            db,
            issue,
            delivery,
            claim=claim,
            success=success,
            error_code=error_code,
        )
        await db.commit()
        return issue_alerted


async def _deliver_notification_claim(claim: AlertDeliveryClaim) -> bool:
    """Create owner notifications and finish the outbox row atomically."""

    settings = get_settings()
    async with async_session() as db:
        issue = await db.get(
            ProductionIssue,
            claim.issue_id,
            with_for_update=True,
        )
        if issue is None:
            return False
        delivery = (
            await db.execute(
                select(ProductionIssueAlertDelivery)
                .where(*_claimed_delivery_predicates(claim))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if delivery is None:
            return False
        if issue.status != "open" or int(issue.alert_epoch or 1) != claim.alert_epoch:
            # A resolved or reopened issue makes an older notification
            # obsolete. Complete only that old delivery, without notifying or
            # projecting notification success onto the current epoch.
            _cancel_obsolete_alert_delivery_row(delivery)
            await db.commit()
            return False
        owner_ids = await resolve_production_alert_owner_ids(db, settings)
        if not owner_ids:
            await _finish_alert_delivery_row(
                db,
                issue,
                delivery,
                claim=claim,
                success=False,
                error_code="SaaSAlertOwnerNotFound",
            )
            await db.commit()
            return False

        notification_ref_id = _production_issue_notification_ref_id(
            issue.id,
            claim.alert_epoch,
        )
        existing_ids = set(
            (
                await db.execute(
                    select(Notification.user_id).where(
                        Notification.ref_id == notification_ref_id,
                        Notification.type == "system",
                        Notification.sender_name == "Astra Monitor",
                        Notification.user_id.in_(owner_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for owner_id in owner_ids:
            if owner_id not in existing_ids:
                db.add(
                    _production_issue_notification(
                        issue,
                        owner_id,
                        payload=claim.payload,
                        alert_epoch=claim.alert_epoch,
                    )
                )
        issue_alerted = await _finish_alert_delivery_row(
            db,
            issue,
            delivery,
            claim=claim,
            success=True,
            notification_sent=True,
        )
        await db.commit()
        return issue_alerted


async def _deliver_webhook_claim(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    claim: AlertDeliveryClaim,
    webhook_url: str,
) -> bool:
    """Fence Issue state across the external webhook and durable completion."""

    async with semaphore:
        async with async_session() as db:
            # Status changes use the same parent row lock. Keeping it through
            # the bounded HTTP call guarantees a resolve/reopen cannot race
            # between epoch validation and the external side effect.
            issue = await db.get(
                ProductionIssue,
                claim.issue_id,
                with_for_update=True,
            )
            if issue is None:
                return False
            delivery = (
                await db.execute(
                    select(ProductionIssueAlertDelivery)
                    .where(*_claimed_delivery_predicates(claim))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if delivery is None:
                return False
            if issue.status != "open" or int(issue.alert_epoch or 1) != claim.alert_epoch:
                _cancel_obsolete_alert_delivery_row(delivery)
                await db.commit()
                return False

            error_code: str | None = None
            try:
                async with asyncio.timeout(
                    PRODUCTION_ISSUE_ALERT_WEBHOOK_TOTAL_TIMEOUT_SECONDS
                ):
                    async with client.stream(
                        "POST",
                        webhook_url,
                        json=claim.payload,
                        headers={
                            "X-Astra-Idempotency-Key": claim.idempotency_key
                        },
                    ) as response:
                        # Status headers are sufficient acknowledgement. Never
                        # buffer an untrusted response body into the worker.
                        response.raise_for_status()
            except Exception as exc:
                error_code = type(exc).__name__[:100]
                logger.error(
                    "[production-issues] alert webhook failed delivery_id={} error_type={}",
                    claim.delivery_id,
                    error_code,
                )
            issue_alerted = await _finish_alert_delivery_row(
                db,
                issue,
                delivery,
                claim=claim,
                success=error_code is None,
                error_code=error_code,
            )
            await db.commit()
            return issue_alerted


async def dispatch_production_issue_alerts() -> int:
    """Claim briefly, then deliver each sink under its required state fence."""

    settings = get_settings()
    claims = await _claim_production_issue_alert_deliveries()
    if not claims:
        return 0

    for claim in claims:
        if claim.payload:
            alert_log = getattr(
                logger,
                _production_issue_alert_log_level(
                    str(claim.payload.get("severity") or "error"),
                    is_canary=bool(claim.payload.get("is_canary")),
                ),
            )
            alert_log(
                "[PRODUCTION_ISSUE_ALERT] issue_id={} alert_epoch={} sink={} "
                "severity={} category={} event_count={} release_version={}",
                claim.issue_id,
                claim.alert_epoch,
                claim.sink,
                claim.payload.get("severity"),
                claim.payload.get("category"),
                claim.payload.get("event_count"),
                claim.payload.get("release_version"),
            )

    webhook_claims = [claim for claim in claims if claim.sink == "webhook"]
    webhook_results: dict[uuid.UUID, bool] = {}
    webhook_configuration_error: str | None = None
    webhook_url = (settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL or "").strip()
    if webhook_claims and webhook_url:
        try:
            from app.services.mcp_security import (
                PublicOnlyAsyncHTTPTransport,
                validate_public_mcp_url,
            )

            await validate_public_mcp_url(webhook_url)
            transport = PublicOnlyAsyncHTTPTransport()
        except Exception as exc:
            webhook_configuration_error = type(exc).__name__[:100]
            logger.error(
                "[production-issues] webhook policy initialization failed error_type={}",
                webhook_configuration_error,
            )
        else:
            semaphore = asyncio.Semaphore(PRODUCTION_ISSUE_ALERT_MAX_CONCURRENCY)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                results = await asyncio.gather(
                    *[
                        _deliver_webhook_claim(
                            client,
                            semaphore,
                            claim,
                            webhook_url,
                        )
                        for claim in webhook_claims
                    ]
                )
            webhook_results = {
                claim.delivery_id: issue_alerted
                for claim, issue_alerted in zip(
                    webhook_claims,
                    results,
                    strict=True,
                )
            }

    alerted_count = 0
    for claim in claims:
        if claim.sink == "notification":
            alerted_count += int(await _deliver_notification_claim(claim))
            continue
        if claim.sink == "webhook":
            if webhook_configuration_error:
                alerted_count += int(
                    await _finalize_alert_delivery(
                        claim,
                        success=False,
                        error_code=webhook_configuration_error,
                    )
                )
            elif webhook_url:
                alerted_count += int(webhook_results.get(claim.delivery_id, False))
            else:
                alerted_count += int(
                    await _finalize_alert_delivery(
                        claim,
                        success=False,
                        error_code="WebhookConfigurationMissing",
                    )
                )
            continue
        logger.error(
            "[production-issues] no alert sink configured issue_id={}",
            claim.issue_id,
        )
        await _finalize_alert_delivery(
            claim,
            success=False,
            error_code="NoConfiguredAlertSink",
        )
    return alerted_count


async def purge_old_production_issue_events() -> int:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(settings.PRODUCTION_ISSUE_RETENTION_DAYS), 1))
    async with async_session() as db:
        result = await db.execute(delete(ProductionIssueEvent).where(ProductionIssueEvent.created_at < cutoff))
        await db.commit()
        return int(result.rowcount or 0)


async def auto_resolve_stale_production_issues(
    *,
    now: datetime | None = None,
) -> int:
    """Resolve bounded, evidence-safe noise without deleting its audit trail."""

    current = now or datetime.now(timezone.utc)
    current_release = str(get_settings().APP_VERSION or "")[:50]
    old_release_cutoff = current - timedelta(hours=24)
    transient_socket_cutoff = current - timedelta(hours=1)
    async with async_session() as db:
        result = await db.execute(
            select(ProductionIssue)
            .where(
                ProductionIssue.status == "open",
                ProductionIssue.source != RELEASE_ALERT_CANARY_SOURCE,
                or_(
                    and_(
                        ProductionIssue.release_version.is_not(None),
                        ProductionIssue.release_version != current_release,
                        ProductionIssue.last_seen_at < old_release_cutoff,
                    ),
                    and_(
                        ProductionIssue.category == "websocket",
                        ProductionIssue.error_code.in_(("close_1005", "close_1006")),
                        ProductionIssue.last_seen_at < transient_socket_cutoff,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        issues = list(result.scalars().all())
        for issue in issues:
            old_release = bool(
                issue.release_version
                and issue.release_version != current_release
                and issue.last_seen_at < old_release_cutoff
            )
            issue.status = "resolved"
            issue.resolved_at = current
            issue.auto_resolved = True
            issue.resolution_reason = (
                "superseded_release_inactive"
                if old_release
                else "transient_client_disconnect_inactive"
            )
        await db.commit()
        return len(issues)


def production_issue_monitor_health(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return DB-loop health without querying the database from /api/health."""

    current = now or datetime.now(timezone.utc)
    deadline_seconds = max(3 * int(_monitor_interval_seconds), 120)
    reference = _monitor_last_db_loop_success_at or _monitor_started_at
    stale_seconds = max((current - reference).total_seconds(), 0.0) if reference is not None else float("inf")
    return {
        "healthy": stale_seconds <= deadline_seconds,
        "last_db_loop_success_at": (
            _monitor_last_db_loop_success_at.isoformat() if _monitor_last_db_loop_success_at else None
        ),
        "oldest_due_delivery_age_seconds": round(
            _monitor_oldest_due_delivery_age_seconds,
            3,
        ),
        "consecutive_db_failures": int(_monitor_consecutive_db_failures),
        "stale_seconds": round(stale_seconds, 3),
        "deadline_seconds": deadline_seconds,
    }


async def start_production_issue_monitor_daemon() -> None:
    """Continuously alert and cap event retention on the worker process."""

    global _monitor_consecutive_db_failures
    global _monitor_interval_seconds
    global _monitor_last_db_loop_success_at
    global _monitor_started_at

    settings = get_settings()
    # Validate release identity before the daemon can claim any durable work.
    _current_alert_worker_identity()
    interval = max(int(settings.PRODUCTION_ISSUE_MONITOR_INTERVAL_SECONDS), 10)
    _monitor_interval_seconds = interval
    _monitor_started_at = datetime.now(timezone.utc)
    _monitor_last_db_loop_success_at = None
    _monitor_consecutive_db_failures = 0
    logger.info("[production-issues] monitor started interval={}s", interval)
    purge_counter = 0
    while True:
        try:
            await flush_failed_production_issue_captures()
            await dispatch_production_issue_alerts()
            purge_counter += 1
            if purge_counter >= max(3600 // interval, 1):
                await purge_old_production_issue_events()
                await auto_resolve_stale_production_issues()
                purge_counter = 0
            _monitor_last_db_loop_success_at = datetime.now(timezone.utc)
            _monitor_consecutive_db_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _monitor_consecutive_db_failures += 1
            logger.error(
                "[production-issues] monitor iteration failed error_type={} consecutive_failures={}",
                type(exc).__name__,
                _monitor_consecutive_db_failures,
            )
            if _monitor_consecutive_db_failures >= PRODUCTION_ISSUE_MONITOR_MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    "PRODUCTION_MONITOR_FATAL release={} task={} error_type={} consecutive_failures={}",
                    getattr(settings, "APP_VERSION", "unknown"),
                    "production_issue_monitor",
                    type(exc).__name__,
                    _monitor_consecutive_db_failures,
                )
                raise RuntimeError("Production issue monitor exceeded its failure threshold") from exc
        await asyncio.sleep(interval)
