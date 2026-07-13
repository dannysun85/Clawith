"""High-level Douyin account, sync, publish, and Agent tool operations."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.douyin import (
    DouyinAccount,
    DouyinComment,
    DouyinMetricSnapshot,
    DouyinOAuthState,
    DouyinOperation,
    DouyinPublishJob,
)
from app.models.user import User
from app.services.douyin.client import DouyinOpenAPIClient, summarize_error
from app.services.douyin.errors import DouyinAuthError
from app.services.douyin.policy import (
    callback_url,
    capability_status,
    configured_scopes,
    direct_publish_enabled,
    has_capability,
    is_configured,
)
from app.services.douyin.token_store import get_valid_access_token, store_oauth_tokens


class DouyinOperationsService:
    """Application service for production Douyin Agent workflows."""

    def config_status(self) -> dict:
        configured = is_configured()
        return {
            "configured": configured,
            "status": "ready" if configured else "not_configured",
            "message": (
                "Douyin OpenAPI app is configured."
                if configured
                else "Douyin OpenAPI credentials are not configured. Set DOUYIN_CLIENT_KEY and DOUYIN_CLIENT_SECRET."
            ),
            "required_scopes": configured_scopes(),
            "callback_url": callback_url(),
        }

    async def tenant_status(self, db: AsyncSession, user: User) -> dict:
        tenant_id = self._tenant_id_or_400(user)
        accounts = await self.list_accounts(db, tenant_id)
        primary = next((account for account in accounts if account.status in {"active", "permission_incomplete"}), None)
        config = self.config_status()
        return {
            **config,
            "accounts": [self.account_payload(account) for account in accounts],
            "primary_account_id": primary.id if primary else None,
        }

    async def list_accounts(self, db: AsyncSession, tenant_id: uuid.UUID) -> list[DouyinAccount]:
        result = await db.execute(
            select(DouyinAccount)
            .where(DouyinAccount.tenant_id == tenant_id, DouyinAccount.status != "disabled")
            .order_by(DouyinAccount.updated_at.desc(), DouyinAccount.created_at.desc())
        )
        return list(result.scalars().all())

    def account_payload(self, account: DouyinAccount) -> dict:
        return {
            "id": account.id,
            "open_id": account.open_id,
            "nickname": account.nickname,
            "avatar_url": account.avatar_url,
            "status": account.status,
            "scopes": list(account.scopes or []),
            "permission_status": account.permission_status or {},
            "capabilities": capability_status(account.scopes or []),
            "authorized_at": account.authorized_at,
            "last_sync_at": account.last_sync_at,
            "last_error": account.last_error,
        }

    async def start_oauth(self, db: AsyncSession, user: User, redirect_after: str | None = None) -> dict:
        tenant_id = self._tenant_id_or_400(user)
        config = self.config_status()
        if not config["configured"]:
            return {
                "status": "not_configured",
                "authorization_url": None,
                "message": config["message"],
                "state_expires_at": None,
            }

        state = secrets.token_urlsafe(40)
        scopes = configured_scopes()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        db.add(
            DouyinOAuthState(
                tenant_id=tenant_id,
                user_id=user.id,
                state=state,
                scopes=scopes,
                redirect_after=redirect_after or "/enterprise#douyin",
                expires_at=expires_at,
            )
        )
        await db.flush()
        settings = get_settings()
        params = {
            "client_key": settings.DOUYIN_CLIENT_KEY,
            "response_type": "code",
            "scope": ",".join(scopes),
            "redirect_uri": callback_url(),
            "state": state,
        }
        auth_url = f"{settings.DOUYIN_AUTHORIZE_URL}?{urlencode(params)}"
        db.add(AuditLog(user_id=user.id, action="douyin_oauth_start", details={"tenant_id": str(tenant_id)}))
        return {
            "status": "ready",
            "authorization_url": auth_url,
            "message": "Redirect the admin to Douyin official OAuth.",
            "state_expires_at": expires_at,
        }

    async def finish_oauth_callback(
        self,
        db: AsyncSession,
        *,
        state: str,
        code: str,
        client: DouyinOpenAPIClient | None = None,
    ) -> tuple[DouyinAccount, str]:
        result = await db.execute(select(DouyinOAuthState).where(DouyinOAuthState.state == state))
        oauth_state = result.scalar_one_or_none()
        if not oauth_state:
            raise HTTPException(status_code=400, detail="Invalid Douyin OAuth state")
        now = datetime.now(timezone.utc)
        if oauth_state.consumed_at:
            raise HTTPException(status_code=400, detail="Douyin OAuth state was already used")
        if oauth_state.expires_at < now:
            raise HTTPException(status_code=400, detail="Douyin OAuth state expired")

        client = client or DouyinOpenAPIClient()
        token_payload = await client.exchange_code(code)
        profile = {}
        try:
            profile = await client.get_user_info(token_payload["access_token"], token_payload["open_id"])
        except Exception:
            # User info should improve display but must not lose a valid OAuth connection.
            profile = {}

        account = await store_oauth_tokens(
            db,
            tenant_id=oauth_state.tenant_id,
            user_id=oauth_state.user_id,
            token_payload=token_payload,
            profile=profile,
        )
        oauth_state.consumed_at = now
        db.add(
            AuditLog(
                user_id=oauth_state.user_id,
                action="douyin_oauth_callback",
                details={"tenant_id": str(oauth_state.tenant_id), "account_id": str(account.id), "scopes": account.scopes},
            )
        )
        await db.flush()
        return account, oauth_state.redirect_after or "/enterprise#douyin"

    async def disable_account(self, db: AsyncSession, user: User, account_id: uuid.UUID) -> DouyinAccount:
        tenant_id = self._tenant_id_or_400(user)
        account = await self._get_account(db, tenant_id, account_id)
        account.status = "disabled"
        account.last_error = None
        db.add(AuditLog(user_id=user.id, action="douyin_account_disabled", details={"account_id": str(account.id)}))
        await db.flush()
        return account

    async def sync_account(
        self,
        db: AsyncSession,
        user: User,
        account_id: uuid.UUID,
        *,
        client: DouyinOpenAPIClient | None = None,
    ) -> DouyinMetricSnapshot:
        tenant_id = self._tenant_id_or_400(user)
        account = await self._get_account(db, tenant_id, account_id)
        now = datetime.now(timezone.utc)
        try:
            await get_valid_access_token(db, account, client=client)
            account.last_sync_at = now
            account.last_error = None
            snapshot = DouyinMetricSnapshot(
                tenant_id=tenant_id,
                account_id=account.id,
                metric_type="account",
                source_api="official_openapi_control_plane",
                data_freshness="sync_checkpoint",
                metrics_json={
                    "account_status": account.status,
                    "capabilities": capability_status(account.scopes or []),
                    "last_sync_at": now.isoformat(),
                },
            )
            db.add(snapshot)
            db.add(AuditLog(user_id=user.id, action="douyin_account_sync", details={"account_id": str(account.id)}))
            await db.flush()
            return snapshot
        except Exception as exc:
            summary = summarize_error(exc)
            account.last_error = summary.get("message")
            if isinstance(exc, DouyinAuthError):
                account.status = "needs_reauth"
            db.add(
                AuditLog(
                    user_id=user.id,
                    action="douyin_account_sync_failed",
                    details={"account_id": str(account.id), "error": summary},
                )
            )
            await db.flush()
            raise

    async def create_publish_job(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        account_id: uuid.UUID | None,
        content_type: str,
        title: str,
        body: str,
        hashtags: list,
        visibility: str,
        asset_refs: list,
        scheduled_at: datetime | None,
        idempotency_key: str | None,
    ) -> DouyinPublishJob:
        await self._assert_agent_in_tenant(db, tenant_id, agent_id)
        key = idempotency_key or f"publish:{agent_id}:{secrets.token_urlsafe(16)}"
        existing = await self._get_existing_publish_job(db, tenant_id, key)
        if existing:
            return existing
        account = await self._get_or_first_account(db, tenant_id, account_id)
        job = DouyinPublishJob(
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account.id if account else None,
            created_by=user_id,
            content_type=content_type,
            title=title,
            body=body,
            hashtags=hashtags,
            visibility=visibility,
            asset_refs=asset_refs,
            scheduled_at=scheduled_at,
            idempotency_key=key,
            publish_mode="collaborative_h5",
            approval_status="pending",
            status="approval_required",
            redacted_request_summary={
                "title": title,
                "body_preview": body[:180],
                "asset_count": len(asset_refs or []),
                "visibility": visibility,
                "publish_mode": "collaborative_h5",
            },
        )
        db.add(job)
        await db.flush()
        approval = ApprovalRequest(
            agent_id=agent_id,
            action_type="douyin_publish_job",
            details={
                "tool": "douyin_run_publish_job",
                "args": {"job_id": str(job.id)},
                "requested_by": str(user_id),
                "title": title,
                "account_id": str(job.account_id) if job.account_id else None,
                "summary": job.redacted_request_summary,
            },
        )
        db.add(approval)
        await db.flush()
        job.approval_id = approval.id
        db.add(AuditLog(user_id=user_id, agent_id=agent_id, action="douyin_publish_job_created", details={"job_id": str(job.id)}))
        await db.flush()
        return job

    async def run_publish_job(
        self,
        db: AsyncSession,
        *,
        job_id: uuid.UUID,
        client: DouyinOpenAPIClient | None = None,
    ) -> DouyinPublishJob:
        result = await db.execute(select(DouyinPublishJob).where(DouyinPublishJob.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError("Douyin publish job not found")
        if job.status in {"created_reviewing", "published_unverified", "awaiting_user_publish", "user_confirmed_waiting_verification"}:
            return job
        if job.approval_id:
            approval_result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == job.approval_id))
            approval = approval_result.scalar_one_or_none()
            if not approval or approval.status != "approved":
                job.status = "approval_required"
                job.approval_status = "pending"
                await db.flush()
                return job
            job.approval_status = "approved"

        account = await self._get_or_first_account(db, job.tenant_id, job.account_id)
        if not account:
            job.status = "blocked"
            job.response_summary = {"message": "需要先连接抖音账号"}
            await db.flush()
            return job

        official_video_id = self._extract_official_video_id(job.asset_refs or [])
        if direct_publish_enabled() and official_video_id and has_capability(account.scopes or [], "direct_publish"):
            return await self._run_direct_publish_job(db, job=job, account=account, official_video_id=official_video_id, client=client)

        if not self._app_has_collaborative_publish_capability(account):
            job.status = "permission_missing"
            job.response_summary = {
                "message": "当前应用还缺少 H5/SDK 协作发布能力。请在抖音开放平台申请 h5.share、open.get.ticket 和 aweme.share 后重新授权。",
            }
            await db.flush()
            return job

        media_payload = self._extract_h5_media_payload(job.asset_refs or [], job.content_type)
        if not media_payload:
            job.status = "blocked"
            job.response_summary = {"message": "缺少可公开访问的视频或图片素材 URL，无法生成抖音 H5 发布包。"}
            await db.flush()
            return job

        operation = DouyinOperation(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            account_id=account.id,
            approval_id=job.approval_id,
            created_by=job.created_by,
            operation_type="prepare_h5_share_package",
            target_id=str(job.id),
            idempotency_key=f"run:{job.id}",
            approval_required=True,
            approval_status="approved",
            status="running",
            request_summary=job.redacted_request_summary,
        )
        db.add(operation)
        job.status = "preparing_share_package"
        job.publish_mode = "collaborative_h5"
        await db.flush()

        try:
            client = client or DouyinOpenAPIClient()
            client_token_payload = await client.get_client_token()
            ticket_payload = await client.get_open_ticket(client_token_payload["client_token"])
            share_payload = await client.create_share_id(
                client_token_payload["client_token"],
                default_hashtag=self._first_hashtag(job),
            )
            schema_payload = self._build_h5_share_schema(
                job=job,
                ticket=ticket_payload["ticket"],
                share_id=share_payload["share_id"],
                media_payload=media_payload,
            )
            job.share_id = share_payload["share_id"]
            job.share_state = share_payload["share_id"]
            job.share_schema_url = schema_payload["schema_url"]
            job.share_nonce = schema_payload["nonce_str"]
            job.share_signature = schema_payload["signature"]
            job.share_expires_at = datetime.now(timezone.utc) + timedelta(seconds=share_payload.get("expires_in") or 3600)
            job.status = "awaiting_user_publish"
            job.official_log_id = share_payload.get("official_log_id")
            job.response_summary = {
                "message": "已生成抖音发布包。请用户用抖音扫码或在移动端打开链接确认发布；这还不等同于公开发布成功。",
                "publish_mode": "collaborative_h5",
                "share_id": job.share_id,
                "share_expires_at": job.share_expires_at.isoformat(),
            }
            operation.status = "succeeded"
            operation.response_summary = job.response_summary
            operation.official_log_id = job.official_log_id
            operation.finished_at = datetime.now(timezone.utc)
            db.add(AuditLog(agent_id=job.agent_id, action="douyin_h5_share_package_prepared", details={"job_id": str(job.id)}))
        except Exception as exc:
            summary = summarize_error(exc)
            job.status = "needs_reauth" if isinstance(exc, DouyinAuthError) else "failed"
            job.response_summary = summary
            job.official_error_code = summary.get("code")
            job.official_log_id = summary.get("log_id")
            operation.status = job.status
            operation.response_summary = summary
            operation.official_error_code = summary.get("code")
            operation.official_log_id = summary.get("log_id")
            operation.finished_at = datetime.now(timezone.utc)
        await db.flush()
        return job

    async def _run_direct_publish_job(
        self,
        db: AsyncSession,
        *,
        job: DouyinPublishJob,
        account: DouyinAccount,
        official_video_id: str,
        client: DouyinOpenAPIClient | None = None,
    ) -> DouyinPublishJob:
        operation = DouyinOperation(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            account_id=account.id,
            approval_id=job.approval_id,
            created_by=job.created_by,
            operation_type="direct_publish_video",
            target_id=str(job.id),
            idempotency_key=f"direct-run:{job.id}",
            approval_required=True,
            approval_status="approved",
            status="running",
            request_summary=job.redacted_request_summary,
        )
        db.add(operation)
        job.status = "creating"
        job.publish_mode = "direct_openapi"
        await db.flush()
        try:
            access_token = await get_valid_access_token(db, account, client=client)
            client = client or DouyinOpenAPIClient()
            official_result = await client.create_video(
                access_token,
                {
                    "video_id": official_video_id,
                    "text": self._compose_publish_text(job),
                },
            )
            job.external_item_id = official_result.get("item_id")
            job.external_video_id = official_video_id
            job.status = "created_reviewing"
            job.published_at = datetime.now(timezone.utc)
            job.official_error_code = official_result.get("official_error_code")
            job.official_log_id = official_result.get("official_log_id")
            job.response_summary = {
                "message": "作品已通过专项后台发布接口创建，等待抖音审核；不能等同于公开发布成功。",
                "publish_mode": "direct_openapi",
                "item_id": job.external_item_id,
            }
            operation.status = "succeeded"
            operation.response_summary = job.response_summary
            operation.official_error_code = job.official_error_code
            operation.official_log_id = job.official_log_id
            operation.finished_at = datetime.now(timezone.utc)
            db.add(AuditLog(agent_id=job.agent_id, action="douyin_direct_publish_job_run", details={"job_id": str(job.id)}))
        except Exception as exc:
            summary = summarize_error(exc)
            job.status = "needs_reauth" if isinstance(exc, DouyinAuthError) else "failed"
            job.response_summary = summary
            job.official_error_code = summary.get("code")
            job.official_log_id = summary.get("log_id")
            operation.status = job.status
            operation.response_summary = summary
            operation.official_error_code = summary.get("code")
            operation.official_log_id = summary.get("log_id")
            operation.finished_at = datetime.now(timezone.utc)
        await db.flush()
        return job

    async def confirm_user_publish(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> DouyinPublishJob:
        result = await db.execute(
            select(DouyinPublishJob).where(DouyinPublishJob.tenant_id == tenant_id, DouyinPublishJob.id == job_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Douyin publish job not found")
        if job.status not in {"awaiting_user_publish", "user_confirmed_waiting_verification", "published_unverified"}:
            raise HTTPException(status_code=400, detail="Publish job is not waiting for user confirmation")
        now = datetime.now(timezone.utc)
        job.confirmed_at = job.confirmed_at or now
        if job.status == "awaiting_user_publish":
            job.status = "user_confirmed_waiting_verification"
        job.response_summary = {
            **(job.response_summary or {}),
            "message": "用户已确认在抖音端完成发布操作，等待 Webhook 或视频数据回查确认最终 item_id。",
            "confirmed_at": job.confirmed_at.isoformat(),
        }
        db.add(
            AuditLog(
                user_id=user_id,
                agent_id=job.agent_id,
                action="douyin_user_publish_confirmed",
                details={"job_id": str(job.id), "share_id": job.share_id},
            )
        )
        await db.flush()
        return job

    async def apply_webhook_event(self, db: AsyncSession, payload: dict) -> None:
        event = payload.get("event")
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        from_user_id = payload.get("from_user_id")
        if event == "create_video" and content.get("share_id"):
            result = await db.execute(select(DouyinPublishJob).where(DouyinPublishJob.share_id == str(content["share_id"])))
            job = result.scalar_one_or_none()
            if job:
                job.external_item_id = content.get("item_id") or job.external_item_id
                job.external_video_id = content.get("video_id") or job.external_video_id
                job.status = "published_unverified"
                job.published_at = datetime.now(timezone.utc)
                job.response_summary = {
                    **(job.response_summary or {}),
                    "message": "抖音已回调用户发布成功，等待后续数据回流。",
                    "item_id": job.external_item_id,
                    "video_id": job.external_video_id,
                }
        elif event == "unauthorize" and from_user_id:
            result = await db.execute(select(DouyinAccount).where(DouyinAccount.open_id == str(from_user_id)))
            for account in result.scalars().all():
                account.status = "needs_reauth"
                account.last_error = "抖音账号已取消授权，需要重新连接。"
        await db.flush()

    async def create_comment_reply_operation(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        account_id: uuid.UUID | None,
        comment_id: str,
        reply_text: str,
        item_id: str | None,
        idempotency_key: str | None,
    ) -> DouyinOperation:
        await self._assert_agent_in_tenant(db, tenant_id, agent_id)
        key = idempotency_key or f"reply:{agent_id}:{comment_id}:{secrets.token_urlsafe(12)}"
        existing_result = await db.execute(
            select(DouyinOperation).where(DouyinOperation.tenant_id == tenant_id, DouyinOperation.idempotency_key == key)
        )
        existing = existing_result.scalar_one_or_none()
        if existing:
            return existing
        account = await self._get_or_first_account(db, tenant_id, account_id)
        operation = DouyinOperation(
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account.id if account else None,
            created_by=user_id,
            operation_type="reply_comment",
            target_id=comment_id,
            idempotency_key=key,
            approval_required=True,
            approval_status="pending",
            status="pending_approval",
            request_summary={
                "comment_id": comment_id,
                "item_id": item_id,
                "reply_preview": reply_text[:180],
            },
        )
        db.add(operation)
        await db.flush()
        approval = ApprovalRequest(
            agent_id=agent_id,
            action_type="douyin_reply_comment",
            details={
                "tool": "douyin_reply_comment",
                "args": {"operation_id": str(operation.id), "reply_text": reply_text, "item_id": item_id},
                "requested_by": str(user_id),
                "comment_id": comment_id,
                "account_id": str(operation.account_id) if operation.account_id else None,
                "summary": operation.request_summary,
            },
        )
        db.add(approval)
        await db.flush()
        operation.approval_id = approval.id
        db.add(AuditLog(user_id=user_id, agent_id=agent_id, action="douyin_reply_operation_created", details={"operation_id": str(operation.id)}))
        await db.flush()
        return operation

    async def run_comment_reply_operation(
        self,
        db: AsyncSession,
        *,
        operation_id: uuid.UUID,
        reply_text: str | None = None,
        item_id: str | None = None,
        client: DouyinOpenAPIClient | None = None,
    ) -> DouyinOperation:
        result = await db.execute(select(DouyinOperation).where(DouyinOperation.id == operation_id))
        operation = result.scalar_one_or_none()
        if not operation:
            raise ValueError("Douyin operation not found")
        if operation.status == "succeeded":
            return operation
        if operation.approval_id:
            approval_result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == operation.approval_id))
            approval = approval_result.scalar_one_or_none()
            if not approval or approval.status != "approved":
                return operation
            operation.approval_status = "approved"
            details = approval.details or {}
            args = details.get("args") or {}
            reply_text = reply_text or args.get("reply_text")
            item_id = item_id or args.get("item_id")

        account = await self._get_or_first_account(db, operation.tenant_id, operation.account_id)
        if not account:
            operation.status = "blocked"
            operation.response_summary = {"message": "需要先连接抖音账号"}
            await db.flush()
            return operation
        if not has_capability(account.scopes or [], "comment_manage"):
            operation.status = "permission_missing"
            operation.response_summary = {"message": "当前授权缺少评论管理权限"}
            await db.flush()
            return operation
        if not reply_text:
            operation.status = "blocked"
            operation.response_summary = {"message": "缺少回复内容"}
            await db.flush()
            return operation

        operation.status = "running"
        await db.flush()
        try:
            access_token = await get_valid_access_token(db, account, client=client)
            client = client or DouyinOpenAPIClient()
            official = await client.reply_comment(
                access_token,
                {
                    "comment_id": operation.target_id,
                    "item_id": item_id,
                    "reply_text": reply_text,
                },
            )
            operation.status = "succeeded"
            operation.response_summary = {
                "message": "评论回复已提交到抖音官方接口。",
                "reply_id": official.get("reply_id"),
            }
            operation.official_error_code = official.get("official_error_code")
            operation.official_log_id = official.get("official_log_id")
            operation.finished_at = datetime.now(timezone.utc)
        except Exception as exc:
            summary = summarize_error(exc)
            operation.status = "needs_reauth" if isinstance(exc, DouyinAuthError) else "failed"
            operation.response_summary = summary
            operation.official_error_code = summary.get("code")
            operation.official_log_id = summary.get("log_id")
            operation.finished_at = datetime.now(timezone.utc)
        await db.flush()
        return operation

    async def agent_dashboard(self, db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict:
        await self._assert_agent_in_tenant(db, tenant_id, agent_id)
        account = await self._get_or_first_account(db, tenant_id, None)
        jobs_result = await db.execute(
            select(DouyinPublishJob)
            .where(DouyinPublishJob.tenant_id == tenant_id, DouyinPublishJob.agent_id == agent_id)
            .order_by(DouyinPublishJob.created_at.desc())
            .limit(20)
        )
        operations_result = await db.execute(
            select(DouyinOperation)
            .where(DouyinOperation.tenant_id == tenant_id, DouyinOperation.agent_id == agent_id)
            .order_by(DouyinOperation.created_at.desc())
            .limit(20)
        )
        snapshots = []
        comments = []
        if account:
            snapshot_result = await db.execute(
                select(DouyinMetricSnapshot)
                .where(DouyinMetricSnapshot.account_id == account.id)
                .order_by(DouyinMetricSnapshot.captured_at.desc())
                .limit(10)
            )
            snapshots = list(snapshot_result.scalars().all())
            comment_result = await db.execute(
                select(DouyinComment)
                .where(DouyinComment.account_id == account.id)
                .order_by(DouyinComment.updated_at.desc())
                .limit(20)
            )
            comments = list(comment_result.scalars().all())
        return {
            "configured": is_configured(),
            "account": self.account_payload(account) if account else None,
            "publish_jobs": list(jobs_result.scalars().all()),
            "operations": list(operations_result.scalars().all()),
            "metric_snapshots": snapshots,
            "comments": comments,
            "message": "已连接抖音账号" if account else "需要先在企业设置连接抖音账号",
        }

    async def account_snapshot_tool(self, db: AsyncSession, *, agent_id: uuid.UUID) -> str:
        agent = await self._get_agent(db, agent_id)
        if not agent or not agent.tenant_id:
            return "需要先将 Agent 归属到企业后才能读取抖音账号。"
        account = await self._get_or_first_account(db, agent.tenant_id, None)
        if not account:
            return "需要先在企业设置连接抖音官方账号，当前不能读取抖音数据。"
        payload = self.account_payload(account)
        last_sync = payload["last_sync_at"].isoformat() if payload["last_sync_at"] else "未同步"
        capability_lines = [f"- {row['label']}: {row['status']}" for row in payload["capabilities"]]
        return "\n".join(
            [
                f"抖音账号：{payload['nickname'] or payload['open_id']}",
                f"状态：{payload['status']}",
                f"最后同步：{last_sync}",
                "能力：",
                *capability_lines,
            ]
        )

    async def make_operation_plan_tool(self, db: AsyncSession, *, agent_id: uuid.UUID, goal: str | None = None) -> str:
        agent = await self._get_agent(db, agent_id)
        if not agent or not agent.tenant_id:
            return "需要先将 Agent 归属到企业。"
        dashboard = await self.agent_dashboard(db, tenant_id=agent.tenant_id, agent_id=agent_id)
        account = dashboard["account"]
        if not account:
            return "当前没有连接抖音账号。建议先连接账号，然后基于真实数据生成计划。"
        latest = dashboard["metric_snapshots"][0] if dashboard["metric_snapshots"] else None
        last_sync = latest.captured_at.isoformat() if latest else "暂无同步数据"
        pending_jobs = [job for job in dashboard["publish_jobs"] if job.status in {"approval_required", "creating", "failed", "blocked"}]
        return "\n".join(
            [
                f"运营目标：{goal or '提升账号稳定内容产出和评论响应质量'}",
                f"数据新鲜度：{last_sync}",
                f"待处理发布任务：{len(pending_jobs)}",
                "建议：",
                "1. 先完成最近一次账号数据同步，再复盘表现最好的 3 条内容。",
                "2. 将新作品先生成发布审批任务，由负责人确认素材权属、文案和账号。",
                "3. 评论回复只处理低风险咨询类评论；投诉、敏感、争议类评论交给人工确认。",
            ]
        )

    def _tenant_id_or_400(self, user: User) -> uuid.UUID:
        if not user.tenant_id:
            raise HTTPException(status_code=400, detail="Current user has no tenant")
        return user.tenant_id

    async def _get_account(self, db: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID) -> DouyinAccount:
        result = await db.execute(
            select(DouyinAccount).where(DouyinAccount.tenant_id == tenant_id, DouyinAccount.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Douyin account not found")
        return account

    async def _get_or_first_account(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID | None,
    ) -> DouyinAccount | None:
        if account_id:
            return await self._get_account(db, tenant_id, account_id)
        result = await db.execute(
            select(DouyinAccount)
            .where(DouyinAccount.tenant_id == tenant_id, DouyinAccount.status != "disabled")
            .order_by(DouyinAccount.updated_at.desc(), DouyinAccount.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_agent(self, db: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        return result.scalar_one_or_none()

    async def _assert_agent_in_tenant(self, db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
        agent = await self._get_agent(db, agent_id)
        if not agent or agent.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Agent not found in current tenant")
        return agent

    async def _get_existing_publish_job(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        idempotency_key: str,
    ) -> DouyinPublishJob | None:
        result = await db.execute(
            select(DouyinPublishJob).where(
                DouyinPublishJob.tenant_id == tenant_id,
                DouyinPublishJob.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    def _extract_official_video_id(self, asset_refs: list) -> str | None:
        for asset in asset_refs or []:
            if not isinstance(asset, dict):
                continue
            value = asset.get("official_video_id") or asset.get("video_id")
            if value:
                return str(value)
        return None

    def _extract_h5_media_payload(self, asset_refs: list, content_type: str) -> dict | None:
        preferred_video = str(content_type or "").lower() == "video"
        video_keys = ("video_path", "video_url", "public_video_url", "url")
        image_keys = ("image_path", "image_url", "public_image_url")
        image_list_keys = ("image_list_path", "image_list", "image_urls")
        for asset in asset_refs or []:
            if not isinstance(asset, dict):
                continue
            if preferred_video:
                for key in video_keys:
                    value = asset.get(key)
                    if value:
                        return {"video_path": str(value)}
            for key in image_list_keys:
                value = asset.get(key)
                if isinstance(value, list) and value:
                    return {"image_list_path": json.dumps([str(item) for item in value], ensure_ascii=False)}
                if isinstance(value, str) and value.strip():
                    return {"image_list_path": value}
            for key in image_keys:
                value = asset.get(key)
                if value:
                    return {"image_path": str(value)}
            if not preferred_video:
                for key in video_keys:
                    value = asset.get(key)
                    if value:
                        return {"video_path": str(value)}
        return None

    def _app_has_collaborative_publish_capability(self, account: DouyinAccount) -> bool:
        scopes = set(account.scopes or []) | set(configured_scopes())
        return has_capability(scopes, "collaborative_publish")

    def _first_hashtag(self, job: DouyinPublishJob) -> str | None:
        for tag in job.hashtags or []:
            clean = str(tag).strip().lstrip("#")
            if clean:
                return clean
        return None

    def _build_h5_share_schema(
        self,
        *,
        job: DouyinPublishJob,
        ticket: str,
        share_id: str,
        media_payload: dict,
        nonce_str: str | None = None,
        timestamp: str | None = None,
    ) -> dict:
        settings = get_settings()
        nonce = nonce_str or secrets.token_urlsafe(12)
        ts = timestamp or str(int(datetime.now(timezone.utc).timestamp()))
        signature_base = f"nonce_str={nonce}&ticket={ticket}&timestamp={ts}"
        signature = hashlib.md5(signature_base.encode("utf-8")).hexdigest()
        params = {
            "share_type": "h5",
            "client_key": settings.DOUYIN_CLIENT_KEY,
            "nonce_str": nonce,
            "timestamp": ts,
            "signature": signature,
            "state": share_id,
            "title": job.title,
            "share_to_type": 0,
            "private_status": self._private_status_value(job.visibility),
        }
        tags = [str(tag).strip().lstrip("#") for tag in (job.hashtags or []) if str(tag).strip()]
        if tags:
            params["hashtag_list"] = json.dumps(tags, ensure_ascii=False)
        params.update(media_payload)
        schema_url = f"snssdk1128://openplatform/share?{urlencode(params)}"
        return {
            "schema_url": schema_url,
            "nonce_str": nonce,
            "timestamp": ts,
            "signature": signature,
        }

    def _private_status_value(self, visibility: str) -> int:
        normalized = (visibility or "").lower()
        if normalized in {"self_only", "private", "only_me"}:
            return 1
        if normalized in {"friends", "friend"}:
            return 2
        return 0

    def _compose_publish_text(self, job: DouyinPublishJob) -> str:
        tags = " ".join(f"#{tag.lstrip('#')}" for tag in (job.hashtags or []) if str(tag).strip())
        text = " ".join(part for part in [job.body.strip(), tags] if part)
        return text[:2200]


douyin_operations_service = DouyinOperationsService()
