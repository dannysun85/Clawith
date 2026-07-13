"""Privacy-safe production issue capture, aggregation, alerting and retention."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.notification import Notification
from app.models.production_issue import ProductionIssue, ProductionIssueEvent
from app.models.user import Identity, User


_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
_LONG_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d{6,}(?![A-Za-z])")
_SECRETISH_RE = re.compile(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|api[_ -]?key\s*[:=]\s*\S+)")
_ALLOWED_SEVERITIES = {"warning", "error", "critical"}
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
}


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

    key = "|".join((
        source.strip().lower(),
        category.strip().lower(),
        (error_code or "unknown").strip().lower(),
        normalize_issue_route(route) or "",
        (operation or "").strip().lower(),
    ))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _safe_summary(summary: str) -> str:
    value = _safe_operational_text(summary, 500)
    return (value or "Production operation failed")[:500]


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
        now = datetime.now(timezone.utc)
        severity = severity if severity in _ALLOWED_SEVERITIES else "error"
        normalized_route = normalize_issue_route(route)
        normalized_operation = _safe_operational_text(operation, 100) if operation else None
        normalized_source = _safe_operational_text(source, 64).lower() or "unknown"
        normalized_category = _safe_operational_text(category, 64).lower() or "unknown"
        normalized_error_code = _safe_operational_text(error_code, 100) if error_code else None
        clean_metadata = sanitize_issue_metadata(metadata)
        release_version = str(clean_metadata.get("release_version") or get_settings().APP_VERSION)[:50]
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
                    "acknowledged_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.acknowledged_at,
                    ),
                    "alerted_at": case(
                        (ProductionIssue.status == "resolved", None),
                        else_=ProductionIssue.alerted_at,
                    ),
                },
            )
            .returning(ProductionIssue.id)
        )
        async with async_session() as db:
            if tenant_id is None and agent_id is not None:
                tenant_id = await db.scalar(
                    select(Agent.tenant_id).where(Agent.id == agent_id)
                )
            issue_id = (await db.execute(statement)).scalar_one()
            db.add(ProductionIssueEvent(
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
            ))
            await db.commit()
        return issue_id
    except Exception as exc:
        logger.error("[production-issues] capture failed error_type={}", type(exc).__name__)
        return None


def issue_requires_alert(issue: ProductionIssue, threshold: int) -> bool:
    return bool(
        issue.status == "open"
        and issue.alerted_at is None
        and (issue.severity == "critical" or issue.event_count >= max(threshold, 1))
    )


def _production_issue_notification(issue: ProductionIssue, user_id: uuid.UUID) -> Notification:
    level = "严重" if issue.severity == "critical" else "错误"
    location = issue.route or issue.operation or issue.category
    return Notification(
        user_id=user_id,
        type="system",
        title=f"[{level}] 生产问题告警",
        body=f"{issue.summary} · {issue.event_count} 次 · {location}",
        link="/admin/saas?tab=production-issues",
        ref_id=issue.id,
        sender_name="Astra Monitor",
    )


async def dispatch_production_issue_alerts() -> int:
    """Emit first-alert logs and optionally deliver a privacy-safe webhook."""

    settings = get_settings()
    threshold = max(int(settings.PRODUCTION_ISSUE_ALERT_THRESHOLD), 1)
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        owner_ids: list[uuid.UUID] = []
        owner_email = (settings.SAAS_ADMIN_EMAIL or "").strip().lower()
        if owner_email:
            owner_result = await db.execute(
                select(User.id)
                .join(Identity, User.identity_id == Identity.id)
                .where(
                    func.lower(Identity.email) == owner_email,
                    User.is_active.is_(True),
                )
            )
            owner_ids = list(dict.fromkeys(owner_result.scalars().all()))
        result = await db.execute(
            select(ProductionIssue)
            .where(
                ProductionIssue.status == "open",
                ProductionIssue.alerted_at.is_(None),
                or_(
                    ProductionIssue.severity == "critical",
                    ProductionIssue.event_count >= threshold,
                ),
            )
            .order_by(ProductionIssue.last_seen_at.asc())
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        issues = list(result.scalars().all())
        for issue in issues:
            payload = {
                "issue_id": str(issue.id),
                "severity": issue.severity,
                "category": issue.category,
                "summary": issue.summary,
                "route": issue.route,
                "operation": issue.operation,
                "event_count": issue.event_count,
                "last_seen_at": issue.last_seen_at.isoformat(),
                "release_version": issue.release_version,
            }
            logger.error("[PRODUCTION_ISSUE_ALERT] {}", payload)
            for owner_id in owner_ids:
                db.add(_production_issue_notification(issue, owner_id))
            if settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL:
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                        response = await client.post(
                            settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL,
                            json=payload,
                        )
                        response.raise_for_status()
                except httpx.HTTPError as exc:
                    logger.error(
                        "[production-issues] alert webhook failed issue_id={} error_type={}",
                        issue.id,
                        type(exc).__name__,
                    )
            issue.alerted_at = now
        if issues:
            await db.commit()
        return len(issues)


async def purge_old_production_issue_events() -> int:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(int(settings.PRODUCTION_ISSUE_RETENTION_DAYS), 1)
    )
    async with async_session() as db:
        result = await db.execute(
            delete(ProductionIssueEvent).where(ProductionIssueEvent.created_at < cutoff)
        )
        await db.commit()
        return int(result.rowcount or 0)


async def start_production_issue_monitor_daemon() -> None:
    """Continuously alert and cap event retention on the worker process."""

    settings = get_settings()
    interval = max(int(settings.PRODUCTION_ISSUE_MONITOR_INTERVAL_SECONDS), 10)
    logger.info("[production-issues] monitor started interval={}s", interval)
    purge_counter = 0
    while True:
        try:
            await dispatch_production_issue_alerts()
            purge_counter += 1
            if purge_counter >= max(3600 // interval, 1):
                await purge_old_production_issue_events()
                purge_counter = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[production-issues] monitor iteration failed")
        await asyncio.sleep(interval)
