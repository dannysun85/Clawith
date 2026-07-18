"""WeCom (企业微信) Channel API routes.

Provides Config CRUD and webhook-based message handling with AES encryption.
"""

import base64
import hashlib
import os
import re
import struct
import time
import uuid
import xml.etree.ElementTree as ET

import asyncio
import httpx
from Crypto.Cipher import AES
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import create_access_token, get_current_user, identity_auth_version
from app.database import async_session, get_db, transaction
from app.models.agent import Agent as AgentModel
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.user import User
from app.services.agent_runtime.channel_chat import (
    channel_message_id,
    enqueue_channel_chat_runtime,
)
from app.services.auth_provider import WeComAuthProvider
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service
from app.services.external_identity_policy import external_user_can_authenticate
from app.services.identity_provider_lookup import get_login_identity_provider_by_id
from app.services.sso_service import ExternalIdentityProvisioningDeniedError
from app.services.sso_scan_session_service import (
    authorize_sso_session,
    get_pending_sso_session,
    parse_sso_scan_state,
    verify_sso_callback_initiator,
)
from app.services.platform_service import platform_service
from app.services.wecom_service import normalize_wecom_agent_id
from app.schemas.schemas import ChannelConfigOut
from app.services.wecom_stream import wecom_stream_manager

router = APIRouter(tags=["wecom"])


# ─── WeCom AES Crypto ──────────────────────────────────

def _pad(text: bytes) -> bytes:
    """PKCS7 padding for AES-CBC."""
    BLOCK_SIZE = 32
    pad_len = BLOCK_SIZE - (len(text) % BLOCK_SIZE)
    return text + bytes([pad_len] * pad_len)


def _unpad(text: bytes) -> bytes:
    """Remove PKCS7 padding."""
    pad_len = text[-1]
    return text[:-pad_len]


def _decrypt_msg(encrypt_key: str, encrypted_text: str) -> tuple[str, str]:
    """Decrypt a WeCom encrypted message.

    Returns (decrypted_xml, corp_id)
    """
    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted = _unpad(cipher.decrypt(base64.b64decode(encrypted_text)))
    # Skip 16 random bytes, then 4 bytes msg_length (network order)
    msg_len = struct.unpack("!I", decrypted[16:20])[0]
    msg_content = decrypted[20:20 + msg_len].decode("utf-8")
    corp_id = decrypted[20 + msg_len:].decode("utf-8")
    return msg_content, corp_id


def _encrypt_msg(encrypt_key: str, reply_msg: str, corp_id: str) -> str:
    """Encrypt a reply message for WeCom."""
    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    msg_bytes = reply_msg.encode("utf-8")
    buf = os.urandom(16) + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(_pad(buf))
    return base64.b64encode(encrypted).decode("utf-8")


def _verify_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Generate WeCom message signature."""
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


# ─── WeCom Domain Verification File Hosting ────────────

# WeCom requires that each self-built app's trusted domain host a
# verification file at: https://domain/WW_verify_<token>.txt
# The file content is just the token string (plain text).
#
# For multi-tenant SaaS, we don't want every tenant to have their own server.
# Instead, tenants paste their verification token into the enterprise settings,
# and this endpoint serves the correct file content for any known token.
#
# Nginx config required to route requests at the root path:
#   location ~ ^/(WW_verify_[A-Za-z0-9_.-]{1,64}\.txt)$ {
#       proxy_pass http://backend:8000/api/wecom-verify/$1;
#   }

_VERIFY_FILENAME_RE = re.compile(r"^WW_verify_[A-Za-z0-9_]{1,64}\.txt$")


@router.get("/wecom-verify/{filename}")
async def serve_wecom_verify_file(
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve a WeCom domain verification file.

    Looks across all active WeCom IdentityProviders for one whose config
    contains the requested filename. Returns the verification content as
    plain text so WeCom's ownership-check bot can confirm it.

    Security: filename is validated against a strict whitelist regex before
    any DB lookup to prevent path traversal or injection attacks.
    """
    # Strict allowlist: only WW_verify_*.txt filenames are legal
    if not _VERIFY_FILENAME_RE.fullmatch(filename):
        return Response(status_code=404)

    # Search all active WeCom providers for a matching verification entry
    result = await db.execute(
        select(IdentityProvider).where(
            IdentityProvider.provider_type == "wecom",
            IdentityProvider.is_active.is_(True),
        )
    )
    providers = result.scalars().all()

    for provider in providers:
        config = provider.config or {}
        verify_files: dict = config.get("wecom_verify_files", {})
        if filename in verify_files:
            content = verify_files[filename]
            logger.info(f"[WeCom Verify] Serving verification file for tenant {provider.tenant_id}")
            return Response(content=content, media_type="text/plain")

    return Response(status_code=404)


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_wecom_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure WeCom bot for an agent.

    Supports two modes:
    - WebSocket (AI Bot): bot_id + bot_secret (no callback URL needed)
    - Webhook (legacy): corp_id, secret, token, encoding_aes_key
    """
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    requested_mode = str(data.get("connection_mode") or "").strip().lower()
    if requested_mode not in {"", "websocket", "webhook"}:
        raise HTTPException(
            status_code=422,
            detail="connection_mode must be websocket or webhook",
        )

    wecom_agent_id_raw = data.get("wecom_agent_id", "")
    wecom_agent_id_text = str(wecom_agent_id_raw or "").strip()
    numeric_wecom_agent_id = normalize_wecom_agent_id(wecom_agent_id_raw)

    # Parse the submitted transport fields before the database read. A complete
    # legacy webhook form (or an explicit webhook request) has enough context
    # to reject a malformed application AgentID immediately. Explicit
    # WebSocket switches deliberately ignore and later clear stale webhook
    # fields from older frontend forms.
    bot_id = str(data.get("bot_id") or "").strip()
    bot_secret = str(data.get("bot_secret") or "").strip()
    corp_id = str(data.get("corp_id") or "").strip()
    secret = str(data.get("secret") or "").strip()
    token = str(data.get("token") or "").strip()
    encoding_aes_key = str(data.get("encoding_aes_key") or "").strip()
    submitted_ws_mode = bool(bot_id and bot_secret)
    submitted_webhook_mode = bool(corp_id and secret and token and encoding_aes_key)
    can_infer_submitted_webhook = (
        requested_mode == "webhook"
        or (
            not requested_mode
            and submitted_webhook_mode
            and not submitted_ws_mode
        )
    )
    if (
        can_infer_submitted_webhook
        and wecom_agent_id_text
        and numeric_wecom_agent_id is None
    ):
        raise HTTPException(
            status_code=422,
            detail="wecom_agent_id must be a positive ASCII numeric value when provided",
        )

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    existing = result.scalar_one_or_none()
    existing_mode = str(
        ((existing.extra_config or {}).get("connection_mode") if existing else "")
        or ""
    )

    # Secret inputs are write-only. Blank values retain credentials only while
    # editing the same transport mode; switching modes always requires the new
    # mode's complete credential set.
    effective_requested_mode = requested_mode or existing_mode
    if existing and effective_requested_mode == existing_mode == "websocket":
        bot_id = bot_id or str((existing.extra_config or {}).get("bot_id") or "")
        bot_secret = bot_secret or str(
            (existing.extra_config or {}).get("bot_secret") or ""
        )
    if existing and effective_requested_mode == existing_mode == "webhook":
        corp_id = corp_id or str(existing.app_id or "")
        secret = secret or str(existing.app_secret or "")
        token = token or str(existing.verification_token or "")
        encoding_aes_key = encoding_aes_key or str(existing.encrypt_key or "")
        if not wecom_agent_id_text:
            wecom_agent_id_text = str(
                (existing.extra_config or {}).get("wecom_agent_id") or ""
            )
            numeric_wecom_agent_id = normalize_wecom_agent_id(wecom_agent_id_text)

    # Select the explicitly requested mode. Legacy clients without this field
    # retain the old inference rule, but an edit form can no longer be kept in
    # WebSocket mode merely because hidden stale bot fields were submitted.
    has_ws_mode = bool(bot_id and bot_secret)
    has_webhook_mode = bool(corp_id and secret and token and encoding_aes_key)
    connection_mode = requested_mode or (
        "websocket" if has_ws_mode else "webhook" if has_webhook_mode else ""
    )
    if not connection_mode:
        raise HTTPException(
            status_code=422,
            detail="Either bot_id+bot_secret (WebSocket) or corp_id+secret+token+encoding_aes_key (Webhook) required"
        )
    if connection_mode == "websocket" and not has_ws_mode:
        raise HTTPException(
            status_code=422,
            detail="bot_id+bot_secret required for WebSocket mode",
        )
    if connection_mode == "webhook" and not has_webhook_mode:
        raise HTTPException(
            status_code=422,
            detail="corp_id+secret+token+encoding_aes_key required for Webhook mode",
        )
    if (
        connection_mode == "webhook"
        and wecom_agent_id_text
        and numeric_wecom_agent_id is None
    ):
        raise HTTPException(
            status_code=422,
            detail="wecom_agent_id must be a positive ASCII numeric value when provided",
        )
    # Customer Service (KF) callbacks use open_kfid and the dedicated KF send
    # API, so they do not have an application AgentID. Keep AgentID optional for
    # that valid configuration, while rejecting malformed values when supplied.
    # Ordinary application messages still fail closed before LLM/Credits work
    # at runtime when the field is absent.
    # Persist only the active mode. This both clears hidden stale credentials
    # during a mode switch and keeps later runtime inference unambiguous.
    if connection_mode == "websocket":
        corp_id = ""
        secret = ""
        token = ""
        encoding_aes_key = ""
        wecom_agent_id = ""
    else:
        bot_id = ""
        bot_secret = ""
        wecom_agent_id = (
            str(numeric_wecom_agent_id)
            if numeric_wecom_agent_id is not None
            else ""
        )

    extra_config = {
        "wecom_agent_id": wecom_agent_id,
        "bot_id": bot_id,
        "bot_secret": bot_secret,
        "connection_mode": connection_mode,
    }

    if existing:
        existing.app_id = corp_id
        existing.app_secret = secret
        existing.encrypt_key = encoding_aes_key
        existing.verification_token = token
        existing.extra_config = extra_config
        existing.is_configured = True
        existing.is_connected = False
        await db.flush()
        config_out = ChannelConfigOut.model_validate(existing)
    else:
        config = ChannelConfig(
            agent_id=agent_id,
            channel_type="wecom",
            app_id=corp_id,
            app_secret=secret,
            encrypt_key=encoding_aes_key,
            verification_token=token,
            extra_config=extra_config,
            is_configured=True,
            is_connected=False,
        )
        db.add(config)
        await db.flush()
        config_out = ChannelConfigOut.model_validate(config)

    await channel_user_service.provision_provider_for_config(
        db, channel_type="wecom", tenant_id=agent.tenant_id
    )

    try:
        if connection_mode == "websocket":
            asyncio.create_task(
                wecom_stream_manager.start_client(agent_id, bot_id, bot_secret)
            )
            logger.info(f"[WeCom] WebSocket client start triggered for agent {agent_id}")
        else:
            asyncio.create_task(wecom_stream_manager.stop_client(agent_id))
            logger.info(f"[WeCom] WebSocket client stop triggered for agent {agent_id}")
    except Exception as e:
        logger.error(f"[WeCom] Failed to update WebSocket client state: {e}")

    return config_out


@router.get("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut | None)
async def get_wecom_channel(
    agent_id: uuid.UUID,
    missing_ok: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        if missing_ok:
            return None
        raise HTTPException(status_code=404, detail="WeCom not configured")

    config_out = ChannelConfigOut.model_validate(config)
    if (config.extra_config or {}).get("connection_mode") == "websocket":
        config_out.is_connected = wecom_stream_manager.status().get(str(agent_id), False)
    else:
        config_out.is_connected = False
    return config_out


@router.get("/agents/{agent_id}/wecom-channel/webhook-url")
async def get_wecom_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    public_base = await platform_service.get_public_base_url(db, request)
    return {"webhook_url": f"{public_base}/api/channel/wecom/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/wecom-channel", status_code=204)
async def delete_wecom_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")
    await wecom_stream_manager.stop_client(agent_id)
    await db.delete(config)


# ─── Event Webhook ──────────────────────────────────────

_processed_wecom_events: set[str] = set()
_processed_kf_msgids: set[str] = set()



@router.get("/channel/wecom/{agent_id}/webhook")
async def wecom_verify_webhook(
    agent_id: uuid.UUID,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom callback URL verification (GET request)."""
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning("[WeCom] Signature mismatch")
        return Response(status_code=403)

    # Decrypt echostr and return plaintext
    try:
        decrypted, _ = _decrypt_msg(encoding_aes_key, echostr)
        return Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt echostr: {e}")
        return Response(status_code=500)


@router.post("/channel/wecom/{agent_id}/webhook")
async def wecom_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom message callback (POST request with encrypted XML)."""
    body_bytes = await request.body()

    # Get channel config
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""
    # Parse encrypted XML body
    try:
        root = ET.fromstring(body_bytes)
        encrypt_text = root.findtext("Encrypt", "")
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse XML body: {e}")
        return Response(content="success", media_type="text/plain")

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, encrypt_text)
    if expected_sig != msg_signature:
        logger.warning("[WeCom] Signature mismatch on POST")
        return Response(status_code=403)

    # Decrypt message
    try:
        decrypted_xml, recv_corp_id = _decrypt_msg(encoding_aes_key, encrypt_text)
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt message: {e}")
        return Response(content="success", media_type="text/plain")

    logger.info(f"[WeCom] Decrypted event for {agent_id}")

    # Parse decrypted message XML
    try:
        msg_root = ET.fromstring(decrypted_xml)
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse decrypted XML: {e}")
        return Response(content="success", media_type="text/plain")

    msg_type = msg_root.findtext("MsgType", "")
    from_user = msg_root.findtext("FromUserName", "")  # WeCom userid
    msg_id = msg_root.findtext("MsgId", "")
    open_kfid = msg_root.findtext("OpenKfId", "")
    token = msg_root.findtext("Token", "")
    # Group chat ID — present when message comes from a WeCom group
    chat_id = msg_root.findtext("ChatId", "")

    dedup_key = msg_id if msg_id else token
    if dedup_key and dedup_key in _processed_wecom_events:
        return Response(content="success", media_type="text/plain")

    logger.info(
        "[WeCom] Message received type={} group={} message_id_present={}",
        msg_type,
        bool(chat_id),
        bool(msg_id),
    )

    if msg_type == "text":
        user_text = msg_root.findtext("Content", "").strip()
        if not user_text:
            return Response(content="success", media_type="text/plain")

        try:
            await _accept_wecom_text(
                agent_id=agent_id,
                from_user=from_user,
                user_text=user_text,
                chat_id=chat_id,
                external_event_id=dedup_key or None,
            )
        except Exception as exc:
            logger.exception(f"[WeCom] Runtime intake failed for agent {agent_id}: {exc}")
            return Response(status_code=500, content="runtime intake failed")
        if dedup_key:
            _processed_wecom_events.add(dedup_key)
            if len(_processed_wecom_events) > 1000:
                _processed_wecom_events.clear()

    elif msg_type == "event":
        event = msg_root.findtext("Event", "")
        if event == "kf_msg_or_event":
            asyncio.create_task(
                _process_wecom_kf_event(agent_id, config, token, open_kfid)
            )
        else:
            logger.info(f"[WeCom] Received event: {event} (not handled)")

    elif msg_type in ("image", "file"):
        # TODO: Handle image/file messages in future
        logger.info(f"[WeCom] Received {msg_type} message (not yet handled)")

    return Response(content="success", media_type="text/plain")


async def _process_wecom_kf_event(agent_id: uuid.UUID, config_obj: ChannelConfig, token: str, open_kfid: str = None):
    """Sync WeCom Customer Service (KF) messages in background."""
    try:
        # Short transaction: load config only
        async with async_session() as _cfg_db:
            r = await _cfg_db.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "wecom")
            )
            config = r.scalar_one_or_none()
        if not config:
            return
        # config is now detached but app_id/app_secret are loaded

        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken", params={"corpid": config.app_id, "corpsecret": config.app_secret})
            token_data = tok_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                return

            current_cursor = token
            has_more = 1
            current_ts = int(time.time())

            while has_more:
                payload = {"limit": 20}
                if open_kfid:
                    payload["open_kfid"] = open_kfid

                if current_cursor.startswith("ENC"):
                    payload["token"] = current_cursor
                else:
                    payload["cursor"] = current_cursor

                logger.info(
                    "[WeCom KF] Calling sync_msg field_count={} cursor_present={}",
                    len(payload),
                    bool(current_cursor),
                )
                sync_resp = await client.post(f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}", json=payload)
                sync_data = sync_resp.json()
                if sync_data.get("errcode") != 0:
                    logger.error(
                        "[WeCom KF] sync_msg error code={}",
                        sync_data.get("errcode", "unknown"),
                    )
                    break

                has_more = sync_data.get("has_more", 0)
                current_cursor = sync_data.get("next_cursor", "")

                for msg in sync_data.get("msg_list", []):
                    if msg.get("origin") == 3 and msg.get("msgtype") == "text":
                        mid = msg.get("msgid")
                        if mid in _processed_kf_msgids:
                            continue
                        if msg.get("send_time", 0) > 0 and (current_ts - msg.get("send_time", 0) > 86400):
                            continue
                        _processed_kf_msgids.add(mid)
                        text = msg.get("text", {}).get("content", "").strip()
                        if text:
                            logger.info(f"[WeCom KF] Found text message chars={len(text)}")
                            await _accept_wecom_text(
                                agent_id=agent_id,
                                from_user=msg.get("external_userid"),
                                user_text=text,
                                is_kf=True,
                                open_kfid=msg.get("open_kfid"),
                                external_event_id=mid,
                            )
                if not has_more:
                    break
    except Exception as e:
        logger.error(f"[WeCom KF] Error in background task: {e}")


async def _accept_wecom_text(
    *,
    agent_id: uuid.UUID,
    from_user: str,
    user_text: str,
    chat_id: str = "",
    is_kf: bool = False,
    open_kfid: str | None = None,
    external_event_id: str | None = None,
) -> None:
    """Persist one WeCom input and Runtime Command before provider acknowledgement."""
    from app.api.feishu import _load_agent_and_model

    async with async_session() as db:
        agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if not agent_obj:
            raise RuntimeError(f"WeCom Agent {agent_id} not found")

        is_group = bool(chat_id)
        conv_id = f"wecom_group_{chat_id}" if is_group else f"wecom_p2p_{from_user}"
        platform_user = await channel_user_service.resolve_channel_user(
            db=db,
            agent=agent_obj,
            channel_type="wecom",
            external_user_id=from_user,
            extra_info={"unionid": from_user},
        )
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=agent_obj.creator_id if is_group else platform_user.id,
            external_conv_id=conv_id,
            source_channel="wecom",
            first_message_title=user_text,
            is_group=is_group,
            group_name=f"WeCom Group {chat_id[:8]}" if is_group else None,
            created_by_user_id=platform_user.id,
        )
        _, model, _, _ = await _load_agent_and_model(db, agent_id)
        await enqueue_channel_chat_runtime(
            db,
            agent=agent_obj,
            user=platform_user,
            session=session,
            model=model,
            content=user_text,
            source_channel="wecom",
            channel_delivery_target={
                "user_id": from_user,
                "is_kf": is_kf,
                "open_kfid": open_kfid,
            },
            message_id=channel_message_id(
                agent_id,
                "wecom",
                external_event_id,
            ),
        )
        await db.commit()


async def _process_wecom_text(
    agent_id: uuid.UUID,
    config: ChannelConfig,
    from_user: str,
    user_text: str,
    is_kf: bool = False,
    open_kfid: str = None,
    kf_msg_id: str = None,
    chat_id: str = "",
):
    """Compatibility ingress that delegates exactly once to Agent Runtime."""
    if not (is_kf and open_kfid):
        standard_agent_id = normalize_wecom_agent_id(
            (config.extra_config or {}).get("wecom_agent_id")
        )
        if standard_agent_id is None:
            # Fail before creating a session or spending LLM credits.  This
            # also protects legacy rows created before save-time validation.
            logger.error(
                "[WeCom] Reply blocked config_error=invalid_agent_id agent={}",
                agent_id,
            )
            return
    await _accept_wecom_text(
        agent_id=agent_id,
        from_user=from_user,
        user_text=user_text,
        chat_id=chat_id,
        is_kf=is_kf,
        open_kfid=open_kfid,
        external_event_id=kf_msg_id,
    )


# ─── OAuth Callback (SSO) ──────────────────────────────

@router.get("/auth/wecom/callback")
async def wecom_callback(
    code: str,
    request: Request,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Authorize one valid WeCom SSO relay session."""
    parsed_state = parse_sso_scan_state(state, provider_type="wecom")
    if not parsed_state:
        raise HTTPException(status_code=400, detail="SSO session is invalid")
    sid, provider_id = parsed_state
    scan_session = await get_pending_sso_session(db, sid)
    verify_sso_callback_initiator(scan_session, request)
    tenant_id = scan_session.tenant_id

    provider = await get_login_identity_provider_by_id(
        db,
        provider_id=provider_id,
        provider_type="wecom",
        tenant_id=tenant_id,
    )
    if not provider:
        raise HTTPException(status_code=403, detail="WeCom SSO is disabled")
    auth_provider = WeComAuthProvider(provider=provider, config=provider.config or {})
    # Release the preflight connection before calling WeCom.
    await db.commit()

    try:
        token_data = await auth_provider.exchange_code_for_token(code)
        access_token_str = token_data.get("access_token")
        if not access_token_str:
            raise ValueError("WeCom token exchange returned no access token")
            
        user_info = await auth_provider.get_user_info(access_token_str)
        if not user_info.provider_user_id:
            raise ValueError("WeCom user info returned no stable subject")
            
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
                provider_type="wecom",
                tenant_id=tenant_id,
                for_update=True,
            )
            if not current_provider:
                raise HTTPException(status_code=403, detail="WeCom SSO is disabled")
            current_auth_provider = WeComAuthProvider(
                provider=current_provider,
                config=current_provider.config or {},
            )
            user, _is_new = await current_auth_provider.find_or_create_user(
                db,
                user_info,
                tenant_id=tenant_id,
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
                provider_type="wecom",
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
        logger.warning("WeCom login/register failed error_type={}", type(exc).__name__)
        return HTMLResponse("Auth failed: WeCom authentication failed", status_code=400)

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )
