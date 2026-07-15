"""Douyin official OpenAPI routes for account operations Agents."""

import hashlib
import hmac
import json
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import get_redis
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.audit import AuditLog
from app.models.douyin import DouyinComment, DouyinMetricSnapshot, DouyinPublishJob
from app.models.user import User
from app.schemas.douyin import (
    DouyinAgentDashboardOut,
    DouyinCommentOut,
    DouyinCommentReplyRequest,
    DouyinMetricSnapshotOut,
    DouyinOAuthStartOut,
    DouyinOAuthStartRequest,
    DouyinOperationOut,
    DouyinPublishJobCreate,
    DouyinPublishJobOut,
    DouyinStatusOut,
)
from app.services.autonomy_service import autonomy_service
from app.services.douyin.client import summarize_error
from app.services.douyin.operations import douyin_operations_service

router = APIRouter(prefix="/douyin", tags=["douyin"])
settings = get_settings()

_WEBHOOK_DEDUP_PREFIX = "douyin:webhook:msg:"
_WEBHOOK_DEDUP_TTL_SECONDS = 24 * 60 * 60


def _verify_douyin_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Verify Douyin's SHA-1(client_secret + raw request body) signature."""
    client_secret = settings.DOUYIN_CLIENT_SECRET
    if not client_secret or not signature:
        return False
    expected = hashlib.sha1(client_secret.encode("utf-8") + raw_body).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


async def _claim_douyin_webhook_message(msg_id: str | None) -> bool:
    """Atomically claim a webhook message ID so retries cannot reapply mutations."""
    if not msg_id:
        return False
    redis = await get_redis()
    return bool(
        await redis.set(
            f"{_WEBHOOK_DEDUP_PREFIX}{msg_id}",
            "1",
            ex=_WEBHOOK_DEDUP_TTL_SECONDS,
            nx=True,
        )
    )


async def _release_douyin_webhook_message(msg_id: str) -> None:
    """Allow Douyin to retry when processing failed before the DB transaction committed."""
    redis = await get_redis()
    await redis.delete(f"{_WEBHOOK_DEDUP_PREFIX}{msg_id}")


@router.get("/status", response_model=DouyinStatusOut)
async def get_douyin_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return tenant Douyin account and capability status without tokens."""
    return await douyin_operations_service.tenant_status(db, current_user)


@router.get("/accounts")
async def list_douyin_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    status = await douyin_operations_service.tenant_status(db, current_user)
    return status["accounts"]


@router.post("/oauth/start", response_model=DouyinOAuthStartOut)
async def start_douyin_oauth(
    data: DouyinOAuthStartRequest,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create an official Douyin OAuth authorization URL for the current tenant."""
    return await douyin_operations_service.start_oauth(db, current_user, data.redirect_after)


@router.get("/oauth/callback")
async def douyin_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Official Douyin OAuth callback. Redirects back to the product page."""
    target = "/enterprise#douyin"
    if error:
        return RedirectResponse(url=f"{target}?{urlencode({'douyin_oauth': 'error', 'reason': error})}")
    if not code or not state:
        return RedirectResponse(url=f"{target}?{urlencode({'douyin_oauth': 'error', 'reason': 'missing_code_or_state'})}")
    try:
        _account, redirect_after = await douyin_operations_service.finish_oauth_callback(db, state=state, code=code)
        target = redirect_after or target
        return RedirectResponse(url=f"{target}?{urlencode({'douyin_oauth': 'success'})}")
    except Exception as exc:
        reason = getattr(exc, "detail", None) or str(exc)
        return RedirectResponse(url=f"{target}?{urlencode({'douyin_oauth': 'error', 'reason': str(reason)[:180]})}")


@router.delete("/accounts/{account_id}", status_code=204)
async def disable_douyin_account(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    await douyin_operations_service.disable_account(db, current_user, account_id)
    return None


@router.post("/accounts/{account_id}/sync", response_model=DouyinMetricSnapshotOut)
async def sync_douyin_account(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await douyin_operations_service.sync_account(db, current_user, account_id)
    except Exception as exc:
        summary = summarize_error(exc)
        raise HTTPException(status_code=400, detail=summary)


@router.get("/accounts/{account_id}/metrics", response_model=list[DouyinMetricSnapshotOut])
async def list_douyin_metrics(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    result = await db.execute(
        select(DouyinMetricSnapshot)
        .where(DouyinMetricSnapshot.tenant_id == tenant_id, DouyinMetricSnapshot.account_id == account_id)
        .order_by(DouyinMetricSnapshot.captured_at.desc())
        .limit(50)
    )
    return list(result.scalars().all())


@router.get("/videos/{item_id}/comments", response_model=list[DouyinCommentOut])
async def list_douyin_video_comments(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    result = await db.execute(
        select(DouyinComment)
        .where(DouyinComment.tenant_id == tenant_id, DouyinComment.external_item_id == item_id)
        .order_by(DouyinComment.updated_at.desc())
        .limit(100)
    )
    return list(result.scalars().all())


@router.get("/agent/{agent_id}/dashboard", response_model=DouyinAgentDashboardOut)
async def get_douyin_agent_dashboard(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    return await douyin_operations_service.agent_dashboard(db, tenant_id=current_user.tenant_id, agent_id=agent_id)


@router.get("/publish-jobs", response_model=list[DouyinPublishJobOut])
async def list_douyin_publish_jobs(
    agent_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    query = select(DouyinPublishJob).where(DouyinPublishJob.tenant_id == current_user.tenant_id)
    if agent_id:
        query = query.where(DouyinPublishJob.agent_id == agent_id)
    result = await db.execute(query.order_by(DouyinPublishJob.created_at.desc()).limit(100))
    return list(result.scalars().all())


@router.post("/publish-jobs", response_model=DouyinPublishJobOut)
async def create_douyin_publish_job(
    data: DouyinPublishJobCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    return await douyin_operations_service.create_publish_job(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        agent_id=data.agent_id,
        account_id=data.account_id,
        content_type=data.content_type,
        title=data.title,
        body=data.body,
        hashtags=data.hashtags,
        visibility=data.visibility,
        asset_refs=data.asset_refs,
        scheduled_at=data.scheduled_at,
        idempotency_key=data.idempotency_key,
    )


@router.get("/publish-jobs/{job_id}", response_model=DouyinPublishJobOut)
async def get_douyin_publish_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    result = await db.execute(
        select(DouyinPublishJob).where(
            DouyinPublishJob.tenant_id == current_user.tenant_id,
            DouyinPublishJob.id == job_id,
        )
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Douyin publish job not found")
    return job


@router.post(
    "/publish-jobs/{job_id}/approve",
    response_model=DouyinPublishJobOut,
    status_code=202,
)
async def approve_douyin_publish_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await get_douyin_publish_job(job_id, current_user=current_user, db=db)
    if not job.approval_id:
        raise HTTPException(status_code=400, detail="Publish job has no approval request")
    try:
        await autonomy_service.resolve_approval(
            db,
            job.approval_id,
            current_user,
            "approve",
            expected_agent_id=job.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job.approval_status = "approved"
    await db.commit()
    result = await db.execute(select(DouyinPublishJob).where(DouyinPublishJob.id == job_id))
    return result.scalar_one()


@router.post("/publish-jobs/{job_id}/run", response_model=DouyinPublishJobOut)
async def run_douyin_publish_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_douyin_publish_job(job_id, current_user=current_user, db=db)
    raise HTTPException(
        status_code=409,
        detail="Approved Douyin jobs are executed only by the durable approval worker",
    )


@router.post("/publish-jobs/{job_id}/confirm-user-publish", response_model=DouyinPublishJobOut)
async def confirm_douyin_user_publish(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    return await douyin_operations_service.confirm_user_publish(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        job_id=job_id,
    )


@router.post("/comments/reply", response_model=DouyinOperationOut)
async def create_douyin_comment_reply(
    data: DouyinCommentReplyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="Current user has no tenant")
    return await douyin_operations_service.create_comment_reply_operation(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        agent_id=data.agent_id,
        account_id=data.account_id,
        comment_id=data.comment_id,
        reply_text=data.reply_text,
        item_id=data.item_id,
        idempotency_key=data.idempotency_key,
    )


@router.post("/webhooks")
async def douyin_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Accept signed, single-use events from the configured Douyin application."""
    raw_body = await request.body()
    if not _verify_douyin_webhook_signature(raw_body, request.headers.get("X-Douyin-Signature")):
        raise HTTPException(status_code=401, detail="Invalid Douyin webhook signature")

    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Douyin webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Douyin webhook payload")
    if payload.get("client_key") != settings.DOUYIN_CLIENT_KEY:
        raise HTTPException(status_code=403, detail="Douyin webhook client mismatch")

    msg_id = request.headers.get("Msg-Id")
    if not await _claim_douyin_webhook_message(msg_id):
        raise HTTPException(status_code=409, detail="Duplicate or missing Douyin webhook message ID")

    if payload.get("event") == "verify_webhook":
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        return {"challenge": content.get("challenge")}

    try:
        await douyin_operations_service.apply_webhook_event(db, payload)
        db.add(AuditLog(action="douyin_webhook_received", details={"msg_id": msg_id, "keys": sorted(payload.keys())[:20]}))
    except Exception:
        await _release_douyin_webhook_message(msg_id)
        raise
    return {"ok": True}
