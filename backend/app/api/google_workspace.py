"""Google Workspace OAuth callback routes."""

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import (
    _request_is_secure,
    create_access_token,
    encrypt_data,
    get_current_admin,
    identity_auth_version,
)
from app.database import get_db, transaction
from app.models.user import User
from app.services.auth_provider import GoogleWorkspaceAuthProvider
from app.services.google_workspace_oauth import (
    GOOGLE_CALLBACK_PATH,
    GOOGLE_SSO_STATE_KIND,
    GOOGLE_SYNC_BROWSER_NONCE_COOKIE,
    GOOGLE_SYNC_STATE_TTL_SECONDS,
    consume_google_sync_state,
    create_google_sync_state,
    get_google_provider,
    get_google_redirect_uri,
    parse_google_oauth_state,
    probe_google_directory,
)
from app.services.external_identity_policy import external_user_can_authenticate
from app.services.identity_provider_lookup import get_login_identity_provider_by_id
from app.services.sso_service import ExternalIdentityProvisioningDeniedError
from app.services.sso_scan_session_service import (
    authorize_sso_session,
    get_pending_sso_session,
    verify_sso_callback_initiator,
)

router = APIRouter(tags=["google_workspace"])
settings = get_settings()


@router.get("/enterprise/identity-providers/{provider_id}/google-workspace-sync/authorize-url")
async def get_google_workspace_sync_authorize_url(
    provider_id: uuid.UUID,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    provider = await get_google_provider(db, provider_id)
    is_platform_admin = current_user.role == "platform_admin" or bool(
        getattr(getattr(current_user, "identity", None), "is_platform_admin", False)
    )
    if not provider.is_active:
        raise HTTPException(status_code=403, detail="Google Workspace provider is disabled")
    if not is_platform_admin and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to manage this provider")

    config = provider.config or {}
    auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=config)
    if not auth_provider.client_id or not auth_provider.client_secret:
        raise HTTPException(status_code=400, detail="Please save Client ID and Client Secret first")

    redirect_uri = await get_google_redirect_uri(db, provider, request)
    browser_nonce = secrets.token_urlsafe(32)
    state = await create_google_sync_state(
        provider_id=provider.id,
        admin_user_id=current_user.id,
        tenant_id=provider.tenant_id,
        redirect_uri=redirect_uri,
        browser_nonce=browser_nonce,
    )
    url = await auth_provider.get_admin_authorization_url(redirect_uri, state)
    response.set_cookie(
        key=GOOGLE_SYNC_BROWSER_NONCE_COOKIE,
        value=browser_nonce,
        max_age=GOOGLE_SYNC_STATE_TTL_SECONDS,
        path="/",
        secure=_request_is_secure(request),
        httponly=True,
        samesite="lax",
    )
    return {"authorization_url": url}


async def _handle_google_sso_callback(
    code: str,
    sid: uuid.UUID,
    provider_id: uuid.UUID,
    request: Request | None,
    db: AsyncSession,
):
    scan_session = await get_pending_sso_session(db, sid)
    verify_sso_callback_initiator(scan_session, request)
    tenant_id = scan_session.tenant_id

    provider = await get_login_identity_provider_by_id(
        db,
        provider_id=provider_id,
        provider_type="google_workspace",
        tenant_id=tenant_id,
    )
    if not provider:
        raise HTTPException(status_code=403, detail="Google Workspace SSO is disabled")
    auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=provider.config or {})

    redirect_uri = await get_google_redirect_uri(db, provider, request)
    # End the preflight transaction before Google network I/O.
    await db.commit()

    try:
        token_data = await auth_provider.exchange_code_for_token(
            code,
            redirect_uri=redirect_uri,
        )
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(
                "Google Workspace token exchange failed error_code={}",
                token_data.get("error", "unknown"),
            )
            return HTMLResponse("Auth failed: Token exchange error")

        user_info = await auth_provider.get_user_info(access_token)
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
                provider_type="google_workspace",
                tenant_id=tenant_id,
                for_update=True,
            )
            if not current_provider:
                raise HTTPException(status_code=403, detail="Google Workspace SSO is disabled")
            current_auth_provider = GoogleWorkspaceAuthProvider(
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
                provider_type="google_workspace",
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
        logger.warning("Google Workspace login failed error_type={}", type(exc).__name__)
        return HTMLResponse("Auth failed: Google Workspace authentication failed", status_code=400)

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )


async def _handle_google_admin_sync_callback(
    code: str,
    provider_id: uuid.UUID,
    admin_user_id: uuid.UUID,
    expected_tenant_id: uuid.UUID | None,
    redirect_uri: str,
    db: AsyncSession,
):
    provider = await get_google_provider(db, provider_id)
    admin_result = await db.execute(
        select(User)
        .where(User.id == admin_user_id)
        .options(selectinload(User.identity))
    )
    admin_user = admin_result.scalar_one_or_none()
    is_platform_admin = bool(
        admin_user
        and (
            admin_user.role == "platform_admin"
            or getattr(getattr(admin_user, "identity", None), "is_platform_admin", False)
        )
    )
    if (
        not external_user_can_authenticate(admin_user)
        or not provider.is_active
        or provider.tenant_id != expected_tenant_id
        or (not is_platform_admin and admin_user.tenant_id != provider.tenant_id)
    ):
        raise HTTPException(status_code=403, detail="Google Workspace authorization is no longer allowed")
    if provider.tenant_id:
        from app.models.tenant import Tenant

        tenant = await db.get(Tenant, provider.tenant_id)
        if not tenant or not tenant.is_active:
            raise HTTPException(status_code=403, detail="Organization is unavailable")
    config = provider.config or {}
    customer_id = config.get("customer_id") or "my_customer"
    auth_provider = GoogleWorkspaceAuthProvider(provider=provider, config=config)
    # Release the request-scoped connection before exchanging tokens or
    # probing the external Directory API.
    await db.commit()

    try:
        token_data = await auth_provider.exchange_code_for_token(code, redirect_uri=redirect_uri)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        if not access_token or not refresh_token:
            raise RuntimeError("Google did not return a refresh token. Re-authorize with consent.")

        profile = await auth_provider.fetch_openid_profile(access_token)
        await probe_google_directory(access_token, customer_id)

        async with transaction(db):
            provider_result = await db.execute(
                select(type(provider))
                .where(type(provider).id == provider_id)
                .with_for_update()
            )
            current_provider = provider_result.scalar_one_or_none()
            current_admin_result = await db.execute(
                select(User)
                .where(User.id == admin_user_id)
                .options(selectinload(User.identity))
            )
            current_admin = current_admin_result.scalar_one_or_none()
            current_is_platform_admin = bool(
                current_admin
                and (
                    current_admin.role == "platform_admin"
                    or getattr(
                        getattr(current_admin, "identity", None),
                        "is_platform_admin",
                        False,
                    )
                )
            )
            if (
                not current_provider
                or not current_provider.is_active
                or current_provider.tenant_id != expected_tenant_id
                or not external_user_can_authenticate(current_admin)
                or (
                    not current_is_platform_admin
                    and current_admin.tenant_id != current_provider.tenant_id
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Google Workspace authorization is no longer allowed",
                )
            if current_provider.tenant_id:
                from app.models.tenant import Tenant

                current_tenant = await db.get(Tenant, current_provider.tenant_id)
                if not current_tenant or not current_tenant.is_active:
                    raise HTTPException(
                        status_code=403,
                        detail="Organization is unavailable",
                    )
            new_config = dict(current_provider.config or {})
            new_config["google_admin_refresh_token_encrypted"] = encrypt_data(
                refresh_token,
                settings.SECRET_KEY,
            )
            new_config["google_admin_authorized_email"] = profile.get("email", "")
            new_config["google_admin_authorized_at"] = datetime.now(timezone.utc).isoformat()
            current_provider.config = new_config
            await db.flush()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Google Workspace admin sync authorization failed error_type={}",
            type(exc).__name__,
        )
        await db.rollback()
        return HTMLResponse(
            """<html><head><meta charset="utf-8" /></head>
            <body style="font-family: sans-serif; padding: 24px;">
                <div>Google Workspace admin authorization failed. Please try again.</div>
            </body></html>""",
            status_code=400,
        )
    return HTMLResponse(
        """<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>Google Workspace admin authorization successful. You can close this window.</div>
            <script>
              if (window.opener) {
                window.opener.postMessage({ type: "google-workspace-sync-authorized" }, "*");
                window.close();
              }
            </script>
        </body></html>"""
    )


@router.get(GOOGLE_CALLBACK_PATH)
async def google_workspace_callback(
    code: str,
    state: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Unified callback for Google Workspace SSO login and admin authorization."""
    # SSO state is self-contained and signed with SECRET_KEY.  Resolve it
    # before touching Redis so a transient cache outage cannot break login.
    parsed_state = parse_google_oauth_state(state) if state else None
    if parsed_state:
        state_kind, state_value = parsed_state
        if state_kind == GOOGLE_SSO_STATE_KIND:
            if len(state_value) != 2:
                raise HTTPException(status_code=400, detail="Authorization failed: invalid state")
            sid, provider_id = state_value
            return await _handle_google_sso_callback(code, sid, provider_id, request, db)

    browser_nonce = (
        request.cookies.get(GOOGLE_SYNC_BROWSER_NONCE_COOKIE) or ""
        if request is not None
        else ""
    )
    try:
        sync_state = (
            await consume_google_sync_state(state, browser_nonce)
            if state and browser_nonce
            else None
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Authorization is temporarily unavailable",
        ) from exc
    if sync_state:
        try:
            provider_id = uuid.UUID(str(sync_state["provider_id"]))
            admin_user_id = uuid.UUID(str(sync_state["admin_user_id"]))
            tenant_raw = sync_state.get("tenant_id")
            expected_tenant_id = uuid.UUID(str(tenant_raw)) if tenant_raw else None
            redirect_uri = str(sync_state["redirect_uri"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Authorization failed: invalid state") from exc
        return await _handle_google_admin_sync_callback(
            code,
            provider_id,
            admin_user_id,
            expected_tenant_id,
            redirect_uri,
            db,
        )

    raise HTTPException(status_code=400, detail="Authorization failed: invalid state")
