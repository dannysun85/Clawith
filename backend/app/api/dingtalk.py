"""DingTalk Channel API routes.

Provides Config CRUD and message handling for DingTalk bots using Stream mode.
"""

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import privacy_safe_shape
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db, transaction
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut
from app.services.agent_runtime.channel_chat import (
    channel_message_id,
    enqueue_channel_chat_runtime,
)
from app.services.media_message_content import sanitize_inline_media_content
from app.services.auth_provider import DingTalkAuthProvider
from app.services.external_identity_policy import external_user_can_authenticate
from app.services.identity_provider_lookup import get_login_identity_provider_by_id
from app.services.sso_service import ExternalIdentityProvisioningDeniedError
from app.services.sso_scan_session_service import (
    authorize_sso_session,
    get_pending_sso_session,
    parse_sso_scan_state,
    verify_sso_callback_initiator,
)

router = APIRouter(tags=["dingtalk"])


class DingTalkWebhookDeliveryError(RuntimeError):
    """A session webhook returned a transport or provider-level failure."""


async def _post_dingtalk_session_webhook(url: str, payload: dict) -> None:
    """Deliver once and validate both HTTP and DingTalk business status."""

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return
    if body.get("success") is False:
        raise DingTalkWebhookDeliveryError("DingTalk rejected the webhook payload")
    for key in ("errcode", "code"):
        if key in body and body[key] not in (None, 0, "0", "ok", "success"):
            raise DingTalkWebhookDeliveryError("DingTalk rejected the webhook payload")


async def _deliver_dingtalk_session_reply(
    *,
    session_webhook: str,
    title: str,
    reply_text: str,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID,
) -> bool:
    """Try markdown then text without ever logging the secret-bearing URL."""

    try:
        await _post_dingtalk_session_webhook(
            session_webhook,
            {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": reply_text},
            },
        )
        return True
    except Exception as exc:
        logger.error(
            "[DingTalk] Session webhook markdown delivery failed error_type={}",
            type(exc).__name__,
        )

    try:
        await _post_dingtalk_session_webhook(
            session_webhook,
            {"msgtype": "text", "text": {"content": reply_text}},
        )
        return True
    except Exception as exc:
        logger.error(
            "[DingTalk] Session webhook text fallback failed error_type={}",
            type(exc).__name__,
        )
        try:
            from app.services.production_issue_monitor import record_production_issue

            await record_production_issue(
                source="dingtalk",
                category="channel_delivery",
                summary="DingTalk reply delivery failed after text fallback",
                severity="error",
                error_code=type(exc).__name__,
                route="/dingtalk/session-webhook",
                operation="reply",
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                metadata={"fallback_attempted": True},
            )
        except Exception as monitor_exc:
            logger.error(
                "[DingTalk] Delivery issue capture failed error_type={}",
                type(monitor_exc).__name__,
            )
        return False


def _append_missing_image_markers(
    user_text: str,
    image_data_urls: list[str] | None,
) -> str:
    """Add each DingTalk image marker exactly once to the provider turn."""
    result = user_text or ""
    for data_url in image_data_urls or []:
        marker = f"[image_data:{data_url}]"
        if marker not in result:
            result = f"{result}\n{marker}" if result else marker
    return result


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_dingtalk_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure DingTalk bot for an agent. Fields: app_key, app_secret, agent_id (optional)."""
    agent, _ = await check_agent_access(
        db,
        current_user,
        agent_id,
        required_level="manage",
        lock_authority=True,
    )

    app_key = data.get("app_key", "").strip()
    app_secret = data.get("app_secret", "").strip()

    # Handle connection mode (Stream/WebSocket vs Webhook) and agent_id
    extra_config = data.get("extra_config", {})
    conn_mode = extra_config.get("connection_mode", "websocket")
    dingtalk_agent_id = extra_config.get("agent_id", "")  # DingTalk AgentId for API messaging

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        app_key = app_key or existing.app_id or ""
        app_secret = app_secret or existing.app_secret or ""
    if not app_key or not app_secret:
        raise HTTPException(status_code=422, detail="app_key and app_secret are required")
    if existing:
        existing.app_id = app_key
        existing.app_secret = app_secret
        existing.is_configured = True
        existing.extra_config = {
            **(existing.extra_config or {}),
            "connection_mode": conn_mode,
            "agent_id": dingtalk_agent_id,
        }
        await db.flush()
        from app.services.channel_user_service import channel_user_service
        await channel_user_service.provision_provider_for_config(
            db, channel_type="dingtalk", tenant_id=agent.tenant_id
        )

        # Restart Stream client if in websocket mode
        if conn_mode == "websocket":
            from app.services.dingtalk_stream import dingtalk_stream_manager
            import asyncio
            asyncio.create_task(dingtalk_stream_manager.start_client(agent_id, app_key, app_secret))
        else:
            # Stop existing Stream client if switched to webhook
            from app.services.dingtalk_stream import dingtalk_stream_manager
            import asyncio
            asyncio.create_task(dingtalk_stream_manager.stop_client(agent_id))

        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="dingtalk",
        app_id=app_key,
        app_secret=app_secret,
        is_configured=True,
        extra_config={
            "connection_mode": conn_mode,
            "agent_id": dingtalk_agent_id,
        },
    )
    db.add(config)
    await db.flush()
    from app.services.channel_user_service import channel_user_service
    await channel_user_service.provision_provider_for_config(
        db, channel_type="dingtalk", tenant_id=agent.tenant_id
    )

    # Start Stream client if in websocket mode
    if conn_mode == "websocket":
        from app.services.dingtalk_stream import dingtalk_stream_manager
        import asyncio
        asyncio.create_task(dingtalk_stream_manager.start_client(agent_id, app_key, app_secret))

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut | None)
async def get_dingtalk_channel(
    agent_id: uuid.UUID,
    missing_ok: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id, required_level="manage")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        if missing_ok:
            return None
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    return ChannelConfigOut.model_validate(config)


@router.delete("/agents/{agent_id}/dingtalk-channel", status_code=204)
async def delete_dingtalk_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(
        db,
        current_user,
        agent_id,
        required_level="manage",
        lock_authority=True,
    )
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    await db.delete(config)

    # Stop Stream client
    from app.services.dingtalk_stream import dingtalk_stream_manager
    import asyncio
    asyncio.create_task(dingtalk_stream_manager.stop_client(agent_id))


# ─── Message Processing (called by Stream callback) ────

async def process_dingtalk_message(
    agent_id: uuid.UUID,
    sender_staff_id: str,
    user_text: str,
    conversation_id: str,
    conversation_type: str,
    session_webhook: str,
    image_base64_list: list[str] | None = None,
    saved_file_paths: list[str] | None = None,
    sender_nick: str = "",
    message_id: str = "",
):
    """Persist one DingTalk input and enqueue exactly one durable Runtime command."""
    from sqlalchemy import select as _select

    from app.api.feishu import _load_agent_and_model
    from app.database import async_session
    from app.models.agent import Agent as AgentModel
    from app.services.channel_session import find_or_create_channel_session
    from app.services.channel_user_service import channel_user_service

    sender_staff_id = (sender_staff_id or "").strip()
    if not sender_staff_id:
        logger.warning("[DingTalk] Skip message attribution because sender_staff_id is empty")
        return

    async with async_session() as db:
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if agent_obj is None:
            logger.warning("[DingTalk] Agent {} not found", agent_id)
            return

        is_group = conversation_type == "2"
        conv_id = (
            f"dingtalk_group_{conversation_id}"
            if is_group
            else f"dingtalk_p2p_{sender_staff_id}"
        )
        platform_user = await channel_user_service.resolve_channel_user(
            db=db,
            agent=agent_obj,
            channel_type="dingtalk",
            external_user_id=sender_staff_id,
            extra_info={"name": sender_nick or f"DingTalk User {sender_staff_id[:8]}"},
        )
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=agent_obj.creator_id if is_group else platform_user.id,
            external_conv_id=conv_id,
            source_channel="dingtalk",
            first_message_title=user_text,
            is_group=is_group,
            group_name=f"DingTalk Group {conversation_id[:8]}" if is_group else None,
            created_by_user_id=platform_user.id,
        )

        display_content = sanitize_inline_media_content(
            user_text,
            file_names=saved_file_paths,
        )
        llm_content = _append_missing_image_markers(user_text, image_base64_list)
        _, model, fallback_model, route_meta = await _load_agent_and_model(
            db,
            agent_id,
        )
        await enqueue_channel_chat_runtime(
            db,
            agent=agent_obj,
            user=platform_user,
            session=session,
            model=model,
            fallback_model=fallback_model,
            route_meta=route_meta,
            content=llm_content,
            display_content=display_content,
            source_channel="dingtalk",
            channel_delivery_target={
                "session_webhook": session_webhook,
                "user_id": sender_staff_id,
                "title": agent_obj.name or "AI Reply",
                "source_message_id": message_id,
                "conversation_id": conversation_id,
            },
            message_id=channel_message_id(agent_id, "dingtalk", message_id),
        )
        await db.commit()

# ─── OAuth Callback (SSO) ──────────────────────────────

@router.get("/auth/dingtalk/callback")
async def dingtalk_callback(
    authCode: str,  # DingTalk uses authCode parameter
    request: Request,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Authorize one valid DingTalk SSO relay session."""
    from app.core.security import create_access_token, identity_auth_version
    from fastapi.responses import HTMLResponse
    parsed_state = parse_sso_scan_state(state, provider_type="dingtalk")
    if not parsed_state:
        raise HTTPException(status_code=400, detail="SSO session is invalid")
    sid, provider_id = parsed_state
    scan_session = await get_pending_sso_session(db, sid)
    verify_sso_callback_initiator(scan_session, request)
    tenant_id = scan_session.tenant_id

    provider = await get_login_identity_provider_by_id(
        db,
        provider_id=provider_id,
        provider_type="dingtalk",
        tenant_id=tenant_id,
    )
    if not provider:
        raise HTTPException(status_code=403, detail="DingTalk SSO is disabled")
    auth_provider = DingTalkAuthProvider(provider=provider, config=provider.config or {})
    # Release the preflight connection before calling DingTalk.
    await db.commit()

    try:
        token_data = await auth_provider.exchange_code_for_token(authCode)
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(
                "DingTalk token exchange failed error_code={}",
                token_data.get("errcode") or token_data.get("code") or "unknown",
            )
            raise ValueError("DingTalk token exchange returned no access token")

        user_info = await auth_provider.get_user_info(access_token)
        if not user_info.provider_union_id:
            logger.error(
                "DingTalk user info missing unionId response_shape={}",
                privacy_safe_shape(user_info.raw_data),
            )
            raise ValueError("DingTalk user info returned no stable subject")

        async with transaction(db):
            current_scan_session = await get_pending_sso_session(
                db,
                sid,
                for_update=True,
            )
            if current_scan_session.tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail="SSO session is invalid")
            current_provider = await get_login_identity_provider_by_id(
                db,
                provider_id=provider_id,
                provider_type="dingtalk",
                tenant_id=tenant_id,
                for_update=True,
            )
            if not current_provider:
                raise HTTPException(status_code=403, detail="DingTalk SSO is disabled")
            current_auth_provider = DingTalkAuthProvider(
                provider=current_provider,
                config=current_provider.config or {},
            )
            user, _is_new = await current_auth_provider.find_or_create_user(
                db,
                user_info,
                tenant_id=str(tenant_id) if tenant_id else None,
            )
            if not external_user_can_authenticate(user):
                raise HTTPException(status_code=403, detail="Account is disabled")
            token = create_access_token(
                str(user.id),
                user.role,
                auth_version=identity_auth_version(user),
            )
            await authorize_sso_session(
                db,
                sid=sid,
                provider_type="dingtalk",
                user_id=user.id,
                access_token=token,
            )
    except ExternalIdentityProvisioningDeniedError as exc:
        raise HTTPException(
            status_code=403,
            detail="This account is not provisioned for the organization.",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("DingTalk login failed error_type={}", type(exc).__name__)
        return HTMLResponse("Auth failed: DingTalk authentication failed", status_code=400)

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )
