"""Client issue intake and SaaS-owner production issue console APIs."""

from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import get_redis
from app.core.permissions import check_agent_access
from app.core.security import get_current_user, get_saas_admin
from app.database import get_db
from app.models.audit import AuditLog
from app.models.production_issue import ProductionIssue, ProductionIssueEvent
from app.models.user import User
from app.schemas.production_issue import (
    ClientIssueReportIn,
    ProductionIssueEventOut,
    ProductionIssueOut,
    ProductionIssueStatusIn,
    ProductionIssueSummaryOut,
)
from app.services.production_issue_monitor import (
    RELEASE_ALERT_CANARY_SOURCE,
    record_production_issue,
)


client_router = APIRouter(prefix="/production-issues", tags=["production-issues"])
admin_router = APIRouter(prefix="/saas/production-issues", tags=["saas-production-issues"])
CLIENT_REPORT_RATE_LIMIT = 30
CLIENT_REPORT_RATE_WINDOW_SECONDS = 60
_FALLBACK_CLIENT_REPORT_MAX_USERS = 1000
_fallback_client_report_timestamps: dict[str, deque[float]] = {}
CLIENT_REPORT_RATE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
local limit = tonumber(ARGV[4])
if count >= limit then
    redis.call('EXPIRE', KEYS[1], ARGV[5])
    return count + 1
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return count + 1
"""


async def _record_and_count_client_reports(user_id: uuid.UUID) -> int:
    """Return this authenticated user's rolling client-report count."""

    now = time.time()
    try:
        redis = await get_redis()
        key = f"production-issue-report:rate:{user_id}"
        member = f"{now}:{uuid.uuid4().hex}"
        count = await redis.eval(
            CLIENT_REPORT_RATE_SCRIPT,
            1,
            key,
            now - CLIENT_REPORT_RATE_WINDOW_SECONDS,
            now,
            member,
            CLIENT_REPORT_RATE_LIMIT,
            CLIENT_REPORT_RATE_WINDOW_SECONDS * 2,
        )
        return int(count)
    except Exception as exc:  # noqa: BLE001 - telemetry must not break the caller
        # Redis is the shared limiter in normal operation.  During a short Redis
        # outage, keep a bounded process-local window so the telemetry endpoint
        # still returns its intended 202/429 response instead of a misleading 500.
        logger.warning(
            "[production-issues] redis rate limiter unavailable; using process-local fallback error_type={}",
            type(exc).__name__,
        )
        key = str(user_id)
        timestamps = _fallback_client_report_timestamps.setdefault(key, deque())
        cutoff = time.monotonic() - CLIENT_REPORT_RATE_WINDOW_SECONDS
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        if len(timestamps) >= CLIENT_REPORT_RATE_LIMIT:
            return len(timestamps) + 1
        timestamps.append(time.monotonic())
        if len(_fallback_client_report_timestamps) > _FALLBACK_CLIENT_REPORT_MAX_USERS:
            stale_key = min(
                _fallback_client_report_timestamps,
                key=lambda candidate: _fallback_client_report_timestamps[candidate][-1]
                if _fallback_client_report_timestamps[candidate]
                else 0,
            )
            _fallback_client_report_timestamps.pop(stale_key, None)
        return len(timestamps)


async def _authorized_client_agent_id(
    db: AsyncSession,
    current_user: User,
    requested_agent_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Associate client telemetry only with an Agent in the caller's tenant."""

    if requested_agent_id is None:
        return None
    try:
        agent, _access_level = await check_agent_access(db, current_user, requested_agent_id)
    except HTTPException:
        return None
    return agent.id


@client_router.post("/client-report", status_code=status.HTTP_202_ACCEPTED)
async def report_client_issue(
    data: ClientIssueReportIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept only operational metadata; prompts, bodies and messages are rejected by allowlist."""

    report_count = await _record_and_count_client_reports(current_user.id)
    if report_count > CLIENT_REPORT_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many client issue reports",
        )
    summaries = {
        "api": "Client observed an API operation failure",
        "runtime": "Client runtime operation failed",
        "websocket": "Client WebSocket operation failed",
    }
    agent_id = await _authorized_client_agent_id(db, current_user, data.agent_id)
    await record_production_issue(
        source=f"client_{data.category}",
        category=data.category,
        summary=summaries[data.category],
        severity=data.severity,
        error_code=data.error_code,
        route=data.route,
        operation=data.operation,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        agent_id=agent_id,
        trace_id=getattr(request.state, "trace_id", None),
        metadata=data.metadata.model_dump(exclude_none=True),
    )
    return {"accepted": True}


def _issue_out(issue: ProductionIssue, affected_tenant_count: int) -> ProductionIssueOut:
    return ProductionIssueOut.model_validate(issue).model_copy(
        update={"affected_tenant_count": int(affected_tenant_count or 0)}
    )


@admin_router.get("", response_model=list[ProductionIssueOut])
async def list_production_issues(
    issue_status: str | None = Query(default="open", alias="status"),
    severity: str | None = None,
    category: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant_counts = (
        select(
            ProductionIssueEvent.issue_id.label("issue_id"),
            func.count(distinct(ProductionIssueEvent.tenant_id)).label("tenant_count"),
        )
        .where(ProductionIssueEvent.tenant_id.is_not(None))
        .group_by(ProductionIssueEvent.issue_id)
        .subquery()
    )
    query = (
        select(ProductionIssue, func.coalesce(tenant_counts.c.tenant_count, 0))
        .outerjoin(tenant_counts, tenant_counts.c.issue_id == ProductionIssue.id)
        .order_by(ProductionIssue.last_seen_at.desc())
        .limit(limit)
    )
    if issue_status:
        query = query.where(ProductionIssue.status == issue_status)
    if severity:
        query = query.where(ProductionIssue.severity == severity)
    if category:
        query = query.where(ProductionIssue.category == category)
    result = await db.execute(query)
    return [_issue_out(issue, tenant_count) for issue, tenant_count in result.all()]


@admin_router.get("/summary", response_model=ProductionIssueSummaryOut)
async def production_issue_summary(
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    open_counts = await db.execute(
        select(
            func.count(ProductionIssue.id),
            func.count(ProductionIssue.id).filter(ProductionIssue.severity == "warning"),
            func.count(ProductionIssue.id).filter(ProductionIssue.severity == "error"),
            func.count(ProductionIssue.id).filter(ProductionIssue.severity == "critical"),
        ).where(
            ProductionIssue.status == "open",
            ProductionIssue.source != RELEASE_ALERT_CANARY_SOURCE,
        )
    )
    total, warning, error, critical = open_counts.one()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = await db.execute(
        select(
            func.count(ProductionIssueEvent.id),
            func.count(distinct(ProductionIssueEvent.tenant_id)),
        )
        .join(
            ProductionIssue,
            ProductionIssue.id == ProductionIssueEvent.issue_id,
        )
        .where(
            ProductionIssueEvent.created_at >= cutoff,
            ProductionIssue.source != RELEASE_ALERT_CANARY_SOURCE,
        )
    )
    event_count, tenant_count = recent.one()
    return ProductionIssueSummaryOut(
        open_total=int(total or 0),
        open_warning=int(warning or 0),
        open_error=int(error or 0),
        open_critical=int(critical or 0),
        events_last_24h=int(event_count or 0),
        affected_tenants_last_24h=int(tenant_count or 0),
    )


@admin_router.get("/{issue_id}/events", response_model=list[ProductionIssueEventOut])
async def list_production_issue_events(
    issue_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    if not await db.get(ProductionIssue, issue_id):
        raise HTTPException(status_code=404, detail="Production issue not found")
    result = await db.execute(
        select(ProductionIssueEvent)
        .where(ProductionIssueEvent.issue_id == issue_id)
        .order_by(ProductionIssueEvent.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@admin_router.patch("/{issue_id}", response_model=ProductionIssueOut)
async def update_production_issue_status(
    issue_id: uuid.UUID,
    data: ProductionIssueStatusIn,
    current_user: User = Depends(get_saas_admin),
    db: AsyncSession = Depends(get_db),
):
    issue = await db.get(ProductionIssue, issue_id, with_for_update=True)
    if not issue:
        raise HTTPException(status_code=404, detail="Production issue not found")
    if issue.source == RELEASE_ALERT_CANARY_SOURCE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Release alert canary status is managed by the release verifier",
        )
    now = datetime.now(timezone.utc)
    before = issue.status
    issue.status = data.status
    issue.acknowledged_at = now if data.status == "acknowledged" else None
    issue.resolved_at = now if data.status in {"resolved", "ignored"} else None
    if data.status == "open":
        if before != "open":
            issue.alert_epoch = int(issue.alert_epoch or 1) + 1
        issue.alerted_at = None
        issue.alert_attempts = 0
        issue.alert_next_attempt_at = None
        issue.alert_last_error_code = None
        issue.alert_notification_sent_at = None
    db.add(
        AuditLog(
            user_id=current_user.id,
            action="production_issue_status_update",
            details={"issue_id": str(issue.id), "before": before, "after": data.status},
        )
    )
    await db.commit()
    await db.refresh(issue)
    tenant_count = (
        await db.execute(
            select(func.count(distinct(ProductionIssueEvent.tenant_id))).where(
                ProductionIssueEvent.issue_id == issue.id,
                ProductionIssueEvent.tenant_id.is_not(None),
            )
        )
    ).scalar_one()
    return _issue_out(issue, tenant_count)
