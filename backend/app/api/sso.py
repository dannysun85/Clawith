import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.auth_rate_limit import auth_rate_limit_client_key
from app.core.security import _request_is_secure
from app.database import get_db, transaction
from app.models.identity import SSOScanSession, IdentityProvider
from app.services.external_identity_policy import external_user_can_authenticate
from app.services.sso_scan_session_service import (
    consume_authorized_sso_session,
    get_pending_sso_session,
    hash_sso_initiator_nonce,
    sso_session_is_expired,
    sso_initiator_cookie_name,
    sign_sso_scan_state,
    verify_sso_initiator_nonce,
)

router = APIRouter(tags=["sso"])


_SSO_SESSION_CREATE_RATE_LIMIT_SCRIPT = """
local client_count = tonumber(redis.call('GET', KEYS[1]) or '0')
local tenant_count = tonumber(redis.call('GET', KEYS[2]) or '0')
local global_count = tonumber(redis.call('GET', KEYS[3]) or '0')

if client_count >= tonumber(ARGV[1]) then
    return 1
end
if tenant_count >= tonumber(ARGV[2]) then
    return 2
end
if global_count >= tonumber(ARGV[3]) then
    return 3
end

for index = 1, 3 do
    local count = redis.call('INCR', KEYS[index])
    if count == 1 then
        redis.call('EXPIRE', KEYS[index], ARGV[4])
    end
end
return 0
"""


def _sso_rate_limit_client_key(request: Request) -> str:
    """Return a privacy-safe client bucket for anonymous SSO initiation."""
    return auth_rate_limit_client_key(request)


async def _enforce_sso_session_creation_rate_limit(
    request: Request,
    tenant_id: uuid.UUID | None,
) -> None:
    """Fail closed when anonymous session creation exceeds bounded Redis quotas."""
    from app.core.events import get_redis

    settings = get_settings()
    window = int(datetime.now(timezone.utc).timestamp()) // 60
    client_key = f"sso:create:client:{_sso_rate_limit_client_key(request)}:{window}"
    tenant_key = f"sso:create:tenant:{tenant_id or 'global'}:{window}"
    global_key = f"sso:create:global:{window}"
    try:
        redis = await get_redis()
        rejected_bucket = int(
            await redis.eval(
                _SSO_SESSION_CREATE_RATE_LIMIT_SCRIPT,
                3,
                client_key,
                tenant_key,
                global_key,
                max(1, settings.SSO_SESSION_CREATE_IP_LIMIT_PER_MINUTE),
                max(1, settings.SSO_SESSION_CREATE_TENANT_LIMIT_PER_MINUTE),
                max(1, settings.SSO_SESSION_CREATE_GLOBAL_LIMIT_PER_MINUTE),
                120,
            )
        )
        if rejected_bucket:
            raise HTTPException(status_code=429, detail="Too many SSO login attempts")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="SSO login is temporarily unavailable",
        ) from exc


def _clear_sso_initiator_cookie(
    response: Response,
    request: Request,
    sid: uuid.UUID,
) -> None:
    response.delete_cookie(
        key=sso_initiator_cookie_name(sid),
        path=get_settings().API_PREFIX,
        secure=_request_is_secure(request),
        httponly=True,
        samesite="lax",
    )


async def _require_tenant_sso_available(
    db: AsyncSession,
    tenant_id: uuid.UUID,
):
    """Enforce the same global + tenant SSO decision at every entry point."""

    from app.models.tenant import Tenant
    from app.services.platform_service import platform_service

    if not await platform_service.is_sso_custom_domain_redirect_enabled(db):
        raise HTTPException(status_code=403, detail="Organization SSO is unavailable")
    tenant = await db.get(Tenant, tenant_id)
    if not tenant or not tenant.is_active or not tenant.sso_enabled:
        raise HTTPException(status_code=403, detail="Organization SSO is unavailable")
    return tenant

@router.post("/sso/session")
async def create_sso_session(
    request: Request,
    response: Response,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db)
):
    """Create a short-lived same-browser SSO relay session."""
    response.headers["Cache-Control"] = "no-store"
    if tenant_id is not None:
        await _require_tenant_sso_available(db, tenant_id)
    await _enforce_sso_session_creation_rate_limit(request, tenant_id)
    initiator_nonce = secrets.token_urlsafe(32)
    session = SSOScanSession(
        id=uuid.uuid4(),
        status="pending",
        tenant_id=tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        initiator_nonce_hash=hash_sso_initiator_nonce(initiator_nonce),
    )
    db.add(session)
    await db.commit()
    response.set_cookie(
        key=sso_initiator_cookie_name(session.id),
        value=initiator_nonce,
        max_age=300,
        path=get_settings().API_PREFIX,
        secure=_request_is_secure(request),
        httponly=True,
        samesite="lax",
    )
    return {"session_id": str(session.id), "expires_at": session.expires_at}


@router.get("/sso/providers")
async def list_sso_providers(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return public provider metadata without allocating a relay session."""
    await _require_tenant_sso_available(db, tenant_id)

    result = await db.execute(
        select(
            IdentityProvider.provider_type,
            IdentityProvider.name,
        )
        .where(
            IdentityProvider.tenant_id == tenant_id,
            IdentityProvider.is_active.is_(True),
            IdentityProvider.sso_login_enabled.is_(True),
        )
        .order_by(IdentityProvider.name, IdentityProvider.id)
    )
    return [
        {"provider_type": provider_type, "name": name}
        for provider_type, name in result.all()
    ]

@router.get("/sso/session/{sid}/status")
async def get_sso_session_status(
    sid: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Read scan status without consuming or returning credentials."""
    response.headers["Cache-Control"] = "no-store"
    result = await db.execute(
        select(SSOScanSession)
        .where(SSOScanSession.id == sid)
        .execution_options(populate_existing=True)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    verify_sso_initiator_nonce(
        session,
        request.cookies.get(sso_initiator_cookie_name(sid)) or "",
    )
    effective_status = "expired" if sso_session_is_expired(session) else session.status
    if effective_status in {"expired", "completed"}:
        _clear_sso_initiator_cookie(response, request, sid)
    return {
        "status": effective_status,
        "provider_type": session.provider_type,
        "error_msg": session.error_msg,
    }


@router.post("/sso/session/{sid}/consume")
async def consume_sso_session(
    sid: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Atomically consume an authorized token for the initiating browser."""
    from sqlalchemy.orm import selectinload

    from app.models.tenant import Tenant
    from app.models.user import User

    response.headers["Cache-Control"] = "no-store"
    consume_header = request.headers.get("x-astra-sso-session", "")
    if not hmac.compare_digest(consume_header, str(sid)):
        raise HTTPException(status_code=403, detail="Invalid SSO consume request")

    async with transaction(db):
        consumed = await consume_authorized_sso_session(
            db,
            sid,
            initiator_nonce=request.cookies.get(sso_initiator_cookie_name(sid)) or "",
        )
        if consumed:
            session, token = consumed
            user_result = await db.execute(
                select(User)
                .where(User.id == session.user_id)
                .options(selectinload(User.identity))
            )
            user = user_result.scalar_one_or_none()
            tenant = await db.get(Tenant, user.tenant_id) if user and user.tenant_id else None
            _clear_sso_initiator_cookie(response, request, sid)
            if (
                not external_user_can_authenticate(user)
                or (user and user.tenant_id and (not tenant or not tenant.is_active))
            ):
                return {
                    "status": "completed",
                    "provider_type": session.provider_type,
                    "error_msg": "Account is disabled",
                }
            # SSO is an ordinary product login path. Return the same live
            # membership/global-role capability snapshot as password and MFA
            # login; a narrow legacy UserOut would leave available_surfaces
            # empty and incorrectly route a valid JIT member to company setup.
            from app.api.auth import serialize_user_with_access

            serialized_user = await serialize_user_with_access(user)
            return {
                "status": "authorized",
                "provider_type": session.provider_type,
                "error_msg": None,
                "access_token": token,
                "user": serialized_user.model_dump() if serialized_user else None,
            }

        result = await db.execute(
            select(SSOScanSession)
            .where(SSOScanSession.id == sid)
            .execution_options(populate_existing=True)
        )
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        effective_status = "expired" if sso_session_is_expired(session) else session.status
        if effective_status in {"expired", "completed"}:
            _clear_sso_initiator_cookie(response, request, sid)
        return {
            "status": effective_status,
            "provider_type": session.provider_type,
            "error_msg": session.error_msg,
        }

@router.put("/sso/session/{sid}/scan")
async def mark_sso_session_scanned(
    sid: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Optional: Mark session as 'scanned' when the landing page loads on mobile."""
    async with transaction(db):
        session = await get_pending_sso_session(db, sid, for_update=True)
        verify_sso_initiator_nonce(
            session,
            request.cookies.get(sso_initiator_cookie_name(sid)) or "",
        )
        if session.status == "pending":
            session.status = "scanned"
            await db.flush()
    return {"status": "ok"}

@router.get("/sso/config")
async def get_sso_config(sid: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """List active SSO providers with their redirect URLs for the specified session ID."""
    # 1. Resolve session to get tenant context
    session = await get_pending_sso_session(db, sid)
    verify_sso_initiator_nonce(
        session,
        request.cookies.get(sso_initiator_cookie_name(sid)) or "",
    )
        
    # 2. Query IdentityProviders for this tenant (only those that are active AND SSO-enabled)
    query = select(IdentityProvider).where(
        IdentityProvider.is_active.is_(True),
        IdentityProvider.sso_login_enabled.is_(True),
    )
    tenant_obj = None
    if session.tenant_id:
        tenant_obj = await _require_tenant_sso_available(db, session.tenant_id)
        query = query.where(IdentityProvider.tenant_id == session.tenant_id)
    else:
        # Fallback to global/unscoped if session has no tenant_id
        # In a fully isolated system, this might return empty results
        query = query.where(IdentityProvider.tenant_id.is_(None))

    result = await db.execute(query)
    providers = result.scalars().all()
    
    # Determine the base URL for OAuth callbacks using centralized platform service:
    from app.services.platform_service import platform_service
    if session.tenant_id:
        public_base = await platform_service.get_tenant_sso_base_url(
            db,
            tenant_obj,
            request,
            sso_redirect_enabled=True,
        )
    else:
        public_base = await platform_service.get_public_base_url(db, request)
    
    auth_urls = []
    for p in providers:
        signed_state = sign_sso_scan_state(p.provider_type, sid, p.id)
        if p.provider_type == "feishu":
            app_id = p.config.get("app_id")
            if app_id:
                redir = f"{public_base}/api/auth/feishu/callback"
                url = (
                    "https://open.feishu.cn/open-apis/authen/v1/index"
                    f"?app_id={app_id}&redirect_uri={quote(redir)}&state={quote(signed_state)}"
                )
                auth_urls.append({"provider_type": "feishu", "name": p.name, "url": url})
        
        elif p.provider_type == "dingtalk":
            from app.services.auth_provider import DingTalkAuthProvider

            auth_provider = DingTalkAuthProvider(provider=p, config=p.config or {})
            redir = f"{public_base}/api/auth/dingtalk/callback"
            url = await auth_provider.get_authorization_url(redir, signed_state)
            auth_urls.append({"provider_type": "dingtalk", "name": p.name, "url": url})
                
        elif p.provider_type == "wecom":
            corp_id = p.config.get("corp_id")
            agent_id = p.config.get("agent_id")
            if corp_id and agent_id:
                # Callback implemented in app/api/wecom.py
                redir = f"{public_base}/api/auth/wecom/callback"
                url = (
                    "https://open.work.weixin.qq.com/wwopen/sso/qrConnect"
                    f"?appid={corp_id}&agentid={agent_id}&redirect_uri={quote(redir)}"
                    f"&state={quote(signed_state)}"
                )
                auth_urls.append({"provider_type": "wecom", "name": p.name, "url": url})
        elif p.provider_type == "google_workspace":
            from app.services.auth_provider import GoogleWorkspaceAuthProvider
            from app.services.google_workspace_oauth import (
                create_google_sso_state,
                get_google_redirect_uri,
            )
            auth_provider = GoogleWorkspaceAuthProvider(provider=p, config=p.config or {})
            redir = await get_google_redirect_uri(db, p, request)
            authorization = await create_google_sso_state(
                session_id=sid,
                provider=p,
                redirect_uri=redir,
            )
            url = await auth_provider.get_sso_authorization_url(
                redir,
                authorization.state,
                code_challenge=authorization.code_challenge,
                nonce=authorization.nonce,
            )
            auth_urls.append({"provider_type": "google_workspace", "name": p.name, "url": url})

    return auth_urls
