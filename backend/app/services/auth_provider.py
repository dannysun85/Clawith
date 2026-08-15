"""Generic OAuth/SSO authentication provider framework.

This module provides a base class for all identity providers (Feishu, DingTalk, WeCom, etc.)
and concrete implementations for each supported provider.
"""

import hmac
import uuid
from urllib.parse import quote, urlencode

import httpx
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from jose import JWTError, jwt

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import IdentityProvider
from app.models.user import User
from app.services.external_identity_policy import (
    acquire_external_subject_lock,
    create_isolated_external_identity,
    external_user_can_authenticate,
    require_stable_external_subject,
)
from app.services.google_workspace_oauth import (
    GOOGLE_HTTP_PROXY,
    local_oidc_emulator_base_url,
)
from app.services.identity_provider_lookup import get_preferred_identity_provider
from loguru import logger


_MEMBERSHIP_TENANT_UNSET = object()


@dataclass
class ExternalUserInfo:
    """Standardized user info from external identity providers."""

    provider_type: str
    provider_union_id: str | None = None
    provider_user_id: str | None = None
    name: str = ""
    email: str = ""
    # Provider-attested email ownership metadata.  This flag is intentionally
    # not sufficient for implicit account linking; the authenticated bind flow
    # is the only automatic merge boundary.
    email_verified: bool = False
    avatar_url: str = ""
    mobile: str = ""
    raw_data: dict = None

    def __post_init__(self):
        if self.raw_data is None:
            self.raw_data = {}


class BaseAuthProvider(ABC):
    """Abstract base class for all authentication providers."""

    provider_type: str = ""

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        """Initialize provider with optional config from database.

        Args:
            provider: IdentityProvider model instance from database
            config: Configuration dict (fallback if no provider record)
        """
        self.provider = provider
        # Provider configuration is ORM-managed mutable state.  Authentication
        # requests may add request-local values such as redirect URIs, so every
        # provider receives an isolated snapshot instead of a live reference
        # that could dirty (and later overwrite) encrypted credentials.
        source_config = provider.config if provider and provider.config else config
        self.config = dict(source_config or {})

    @abstractmethod
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Generate OAuth authorization URL.

        Args:
            redirect_uri: Callback URL after authorization
            state: CSRF state parameter

        Returns:
            Authorization URL to redirect user to
        """
        pass

    @abstractmethod
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Dict containing access_token and optionally refresh_token
        """
        pass

    @abstractmethod
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Fetch user profile from provider API.

        Args:
            access_token: Valid access token

        Returns:
            ExternalUserInfo instance with user data
        """
        pass

    async def find_or_create_user(
        self,
        db: AsyncSession,
        user_info: ExternalUserInfo,
        tenant_id: str | None = None,
        membership_tenant_id: str | None | object = _MEMBERSHIP_TENANT_UNSET,
    ) -> tuple[User, bool]:
        """Find existing user or create new one via Identity/OrgMember.

        Args:
            db: Database session
            user_info: User info from provider
            tenant_id: Optional tenant ID for association
        """
        from app.services.sso_service import (
            ExternalIdentityProvisioningDeniedError,
            sso_service,
        )

        effective_membership_tenant_id = (
            tenant_id
            if membership_tenant_id is _MEMBERSHIP_TENANT_UNSET
            else membership_tenant_id
        )

        # 1. Resolve only through a provider-scoped stable subject.  Email and
        # mobile are profile claims, not account-link credentials.
        provider_user_id = require_stable_external_subject(
            self.provider_type,
            user_info.provider_user_id or user_info.provider_union_id,
        )
        await acquire_external_subject_lock(
            db,
            provider_type=self.provider_type,
            tenant_id=tenant_id,
            provider_subject=provider_user_id,
        )

        # Lock before provider creation so concurrent first login cannot create
        # duplicate provider or member shells.
        provider = await self._ensure_provider(db, tenant_id)
        identity_data = self._identity_payload(user_info)
        user = await sso_service.resolve_user_identity(
            db,
            provider_user_id,
            self.provider_type,
            tenant_id=tenant_id,
            identity_data=identity_data,
            provider_id=provider.id,
        )

        # Tenant SSO is an authentication path, not an implicit invitation.
        # A subject must already exist in the exact provider's synchronized
        # directory unless an explicit, provider-verifiable JIT policy allows
        # creation.  Global OAuth registration remains governed by its signup
        # code and therefore does not pass a tenant scope here.
        if user is None and tenant_id is not None:
            provisioned_member = await sso_service.find_identity_member(
                db,
                provider.id,
                self.provider_type,
                provider_user_id,
                identity_data,
                active_only=False,
            )
            if provisioned_member is not None and provisioned_member.status != "active":
                raise ExternalIdentityProvisioningDeniedError(
                    "The directory member is disabled or deleted"
                )
            if provisioned_member is None and not self._tenant_jit_provisioning_allowed(user_info):
                raise ExternalIdentityProvisioningDeniedError(
                    "The external subject is not provisioned for this tenant"
                )

        is_new = False
        if user:
            # Disabled global identities and memberships remain authoritative;
            # do not mutate or relink them before the callback rejects login.
            if not getattr(user, "is_active", False):
                return user, False
            if user.identity_id and not external_user_can_authenticate(user):
                return user, False

            # Update user info and ensure identity is loaded
            if not user.identity_id:
                identity = await create_isolated_external_identity(
                    db,
                    provider_type=self.provider_type,
                    provider_subject=provider_user_id,
                )
                user.identity_id = identity.id
                # Keep the relationship and foreign key in sync before any
                # association-proxy reads/writes below.  Otherwise assigning
                # ``user.email`` can synthesize a second Identity object.
                user.identity = identity
            
            await self._update_existing_user(db, user, user_info)
        else:
            # 3. Create new user (and Identity if needed)
            user = await self._create_new_user(
                db,
                user_info,
                effective_membership_tenant_id,
            )
            is_new = True
            
        # Ensure OrgMember linkage
        await sso_service.link_identity(
            db,
            str(user.id),
            self.provider_type,
            provider_user_id,
            identity_data,
            tenant_id=tenant_id,
            provider_id=provider.id,
        )

        # SSO users should also appear as Web members for tenant-side user management.
        from app.services.registration_service import registration_service
        await registration_service.ensure_web_org_member(user)

        return user, is_new

    def _tenant_jit_provisioning_allowed(self, user_info: ExternalUserInfo) -> bool:
        """Return whether this exact provider proves safe tenant-side JIT.

        The default is deliberately false.  Provider subclasses may opt in
        only when an administrator enabled the policy and the callback proves
        organization ownership from provider-bound data.
        """
        return False

    def _identity_payload(self, user_info: ExternalUserInfo) -> dict[str, Any]:
        """Build provider metadata without promoting contacts to Identity."""
        return {
            "name": user_info.name,
            "email": user_info.email,
            "email_verified": user_info.email_verified,
            "mobile": user_info.mobile,
            "avatar": user_info.avatar_url,
            "raw_data": user_info.raw_data,
        }

    async def _ensure_provider(self, db: AsyncSession, tenant_id: str | None = None) -> IdentityProvider:
        """Get or create IdentityProvider record."""
        if self.provider:
            return self.provider

        provider = await get_preferred_identity_provider(
            db,
            self.provider_type,
            tenant_id,
        )

        if not provider:
            provider = IdentityProvider(
                provider_type=self.provider_type,
                name=self.provider_type.capitalize(),
                is_active=True,
                config=self.config,
                tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
            )
            db.add(provider)
            await db.flush()

        self.provider = provider
        return provider

    async def _find_user_by_legacy_fields(self, db: AsyncSession, user_info: ExternalUserInfo) -> User | None:
        """Find user by legacy provider-specific fields (if any)."""
        return None  # Override in subclasses for backward compatibility

    async def _update_existing_user(
        self, db: AsyncSession, user: User, user_info: ExternalUserInfo
    ):
        """Update existing user with new info from provider."""
        if user_info.name and not user.display_name:
            user.display_name = user_info.name
        if user_info.avatar_url and not user.avatar_url:
            user.avatar_url = user_info.avatar_url
        # Update legacy fields if applicable
        await self._update_legacy_user_fields(user, user_info)

    async def _create_new_user(
        self,
        db: AsyncSession,
        user_info: ExternalUserInfo,
        tenant_id: str | None,
    ) -> User:
        """Create new user from external identity."""
        provider_subject = require_stable_external_subject(
            self.provider_type,
            user_info.provider_user_id or user_info.provider_union_id,
        )
        identity = await create_isolated_external_identity(
            db,
            provider_type=self.provider_type,
            provider_subject=provider_subject,
        )

        # 2. Create a tenant membership.  Successful provider authentication
        # activates the membership, while the global Identity remains
        # passwordless and carries no unproven contact ownership.
        user = User(
            identity_id=identity.id,
            display_name=user_info.name or identity.username,
            avatar_url=user_info.avatar_url,
            registration_source=self.provider_type,
            # Keep the in-memory ORM value type aligned with PostgreSQL UUID.
            # The identity linker compares this value before a refresh; leaving
            # the caller's string here makes a legitimate same-tenant JIT flow
            # look like a cross-tenant link attempt.
            tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
            role="member",
            is_active=True,
            activation_pending_email_verification=False,
        )


        # Set legacy fields if needed
        await self._set_legacy_user_fields(user, user_info)

        db.add(user)
        await db.flush()
        
        # Preload identity
        user.identity = identity
        return user

    async def _update_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """Override in subclass to update provider-specific legacy fields."""
        pass

    async def _set_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """Override in subclass to set provider-specific legacy fields on new user."""
        pass


class FeishuAuthProvider(BaseAuthProvider):
    """Feishu (Lark) OAuth provider implementation."""

    provider_type = "feishu"

    FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
    FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
    FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.app_id = self.config.get("app_id")
        self.app_secret = self.config.get("app_secret")
        self._app_access_token: str | None = None

    def _tenant_jit_provisioning_allowed(self, user_info: ExternalUserInfo) -> bool:
        return bool(
            self.config.get("jit_provisioning_enabled") is True
            and self.app_id
            and self.app_secret
            and user_info.provider_union_id
        )

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_id or ""
        base_url = "https://open.feishu.cn/open-apis/authen/v1/authorize"
        params = f"app_id={app_id}&redirect_uri={redirect_uri}&state={state}"
        return f"{base_url}?{params}"

    async def get_app_access_token(self) -> str:
        if self._app_access_token:
            return self._app_access_token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.FEISHU_APP_TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            self._app_access_token = data.get("app_access_token", "")
            return self._app_access_token

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                self.FEISHU_TOKEN_URL,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            token_data = token_resp.json()
            return token_data.get("data", {})

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            info_resp = await client.get(
                self.FEISHU_USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            info_data = info_resp.json().get("data", {})
            logger.info(f"Feishu user info fetched field_count={len(info_data)}")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_union_id=info_data.get("union_id"),
                name=info_data.get("name", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatar_url", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )

    async def _find_user_by_legacy_fields(self, db: AsyncSession, user_info: ExternalUserInfo) -> User | None:
        """Feishu legacy lookup removed (open_id/union_id no longer stored on User)."""
        return None

    async def _update_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return

    async def _set_legacy_user_fields(self, user: User, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return


class DingTalkAuthProvider(BaseAuthProvider):
    """DingTalk OAuth provider implementation."""

    provider_type = "dingtalk"

    DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
    DINGTALK_USER_INFO_URL = "https://api.dingtalk.com/v1.0/contact/users/me"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.app_key = self.config.get("app_key")
        self.app_secret = self.config.get("app_secret")
        self.corp_id = self.config.get("corp_id")

    def _tenant_jit_provisioning_allowed(self, user_info: ExternalUserInfo) -> bool:
        returned_corp_id = (
            user_info.raw_data.get("corpId")
            or user_info.raw_data.get("corp_id")
            or user_info.raw_data.get("corpid")
        )
        return bool(
            self.config.get("jit_provisioning_enabled") is True
            and self.corp_id
            and returned_corp_id
            and hmac.compare_digest(str(self.corp_id), str(returned_corp_id))
        )

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_key or ""
        base_url = "https://login.dingtalk.com/oauth2/auth"
        # Contact.User.Read is required for GET /v1.0/contact/users/me (user info on callback)
        # contact.user.mobile requires the fieldMobile permission in DingTalk console
        # fieldEmail requires the fieldEmail permission in DingTalk console
        scope = "openid corpid Contact.User.Read fieldEmail contact.user.mobile"
        params = (
            f"client_id={app_id}&redirect_uri={quote(redirect_uri)}&"
            f"state={state}&response_type=code&scope={quote(scope)}&prompt=consent"
        )
        # corp_id is optional: restricts the login page to a specific enterprise.
        # If not configured, DingTalk shows a company picker (still works for SSO).
        if self.corp_id:
            params = f"corpId={self.corp_id}&" + params
        return f"{base_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.DINGTALK_TOKEN_URL,
                json={
                    "clientId": self.app_key,
                    "clientSecret": self.app_secret,
                    "code": code,
                    "grantType": "authorization_code",
                },
            )
            resp_data = resp.json()
            if not isinstance(resp_data, dict):
                logger.error(
                    "DingTalk token exchange returned invalid payload type={}",
                    type(resp_data).__name__,
                )
                return {}
            if resp.status_code != 200:
                logger.error(
                    "DingTalk token exchange failed status={} error_code={}",
                    resp.status_code,
                    resp_data.get("code") or resp_data.get("errcode") or "unknown",
                )
                return {}

            # New DingTalk OAuth2 returns flat JSON with camelCase fields
            return {
                "access_token": resp_data.get("accessToken"),
                "refresh_token": resp_data.get("refreshToken"),
                "expires_in": resp_data.get("expireIn"),
            }

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            headers = {"x-acs-dingtalk-access-token": access_token}
            info_resp = await client.get(self.DINGTALK_USER_INFO_URL, headers=headers)
            info_data = info_resp.json()
            if not isinstance(info_data, dict):
                logger.error(
                    "DingTalk user info returned invalid payload type={}",
                    type(info_data).__name__,
                )
                raise Exception("Failed to fetch user info: invalid response payload")
            if info_resp.status_code != 200:
                # Common error: errCode=403 means Contact.User.Read scope not granted.
                # Ensure 'Contact.User.Read' is included in the OAuth scope AND
                # that the app has been authorized by the employee in the login flow.
                err_msg = info_data.get('message') or info_data.get('errmsg') or str(info_data)
                logger.error(
                    "DingTalk user info fetch failed status={} error_code={}",
                    info_resp.status_code,
                    info_data.get("code") or info_data.get("errCode") or "unknown",
                )
                raise Exception(f"Failed to fetch user info: {err_msg}")

            # DingTalk new OAuth2 returns openId, unionId, nick, avatarUrl, mobile, email
            logger.info(f"DingTalk user info fetched field_count={len(info_data)}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_union_id=info_data.get("unionId"),
                name=info_data.get("nick", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatarUrl", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )


class WeComAuthProvider(BaseAuthProvider):
    """WeCom (Enterprise WeChat) OAuth provider implementation.

    Authentication flow:
    1. gettoken (corp_id + secret) -> access_token
    2. auth/getuserinfo (access_token + OAuth code) -> userid + user_ticket
    3. auth/getuserdetail (access_token + user_ticket) -> avatar, email, mobile
    4. user/get (access_token + userid) -> name, position (non-sensitive fields)

    Note: Steps 3 and 4 require the calling server IP to be whitelisted in the
    WeCom self-built app settings. This is a one-time setup per tenant.
    (Contrast with getuserinfo in step 2, which only requires trusted domain,
    not IP whitelist.)
    """

    provider_type = "wecom"

    # All WeCom self-built app API calls go to qyapi.weixin.qq.com
    # The old api.weixin.qq.com endpoints are legacy WeCom Public Account APIs
    # and no longer work for self-built apps.
    WECOM_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    WECOM_USER_INFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo"
    WECOM_USER_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserdetail"
    WECOM_USER_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        # corp_id and agent_id are used for the OAuth redirect URL
        self.corp_id = self.config.get("corp_id") or self.config.get("app_id")
        # secret is the self-built app's AgentSecret (not the contact-sync secret)
        self.secret = self.config.get("secret") or self.config.get("app_secret")
        self.agent_id = self.config.get("agent_id")

    def _tenant_jit_provisioning_allowed(self, user_info: ExternalUserInfo) -> bool:
        returned_user_id = str(user_info.raw_data.get("userid") or "").strip()
        return bool(
            self.config.get("jit_provisioning_enabled") is True
            and self.corp_id
            and self.secret
            and self.agent_id
            and returned_user_id
            and hmac.compare_digest(
                returned_user_id,
                str(user_info.provider_user_id or "").strip(),
            )
        )

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Construct the WeCom web-login SSO redirect URL.

        Uses the 'Scan QR Code to Login' flow (CorpPinCorp), which redirects users
        to authenticate with their WeCom account then returns them to redirect_uri
        with a code parameter.
        """
        base_url = "https://open.work.weixin.qq.com/wwlogin/sso/login"
        params = (
            f"loginType=CorpPinCorp"
            f"&appid={self.corp_id}"
            f"&agentid={self.agent_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&state={state}"
        )
        return f"{base_url}?{params}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        """Exchange OAuth code for a packed token string containing all user data.

        Three sequential API calls:
          1. gettoken -> access_token
          2. auth/getuserinfo (code) -> userid + user_ticket
          3a. auth/getuserdetail (user_ticket) -> avatar, email, mobile [sensitive]
          3b. user/get (userid) -> name, position [non-sensitive, best-effort]

        Returns a packed JSON dict disguised as the access_token field so
        the existing BaseAuthProvider interface (get_user_info) can consume it.
        """
        import json

        async with httpx.AsyncClient(timeout=10) as client:
            # Step 1: Get app-level access token using corp credentials
            token_resp = await client.get(
                self.WECOM_TOKEN_URL,
                params={"corpid": self.corp_id, "corpsecret": self.secret},
            )
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                logger.error(
                    "[WeCom SSO] gettoken failed error_code={}",
                    token_data.get("errcode", "unknown"),
                )
                return {}

            # Step 2: Exchange OAuth code for userid + user_ticket
            # auth/getuserinfo returns userid (lowercase 'u') for internal employees.
            # user_ticket is a temporary credential (valid ~1800s) representing
            # the employee's own OAuth authorization, required for sensitive fields.
            info_resp = await client.get(
                self.WECOM_USER_INFO_URL,
                params={"access_token": access_token, "code": code},
            )
            info_data = info_resp.json()
            # The key is lowercase 'userid' in the new auth endpoint (not 'UserId')
            userid = info_data.get("userid") or info_data.get("UserId", "")
            user_ticket = info_data.get("user_ticket", "")
            if not userid:
                logger.error(
                    "[WeCom SSO] getuserinfo missing userid error_code={}",
                    info_data.get("errcode", "unknown"),
                )
                return {}

            # Step 3a: Fetch sensitive profile fields using user_ticket.
            # Since June 2022, new self-built apps cannot get avatar/email/mobile
            # from user/get directly. The user_ticket (from OAuth consent) unlocks them.
            # Returns: userid, gender, avatar, qr_code, mobile, email, biz_mail, address
            sensitive_data: dict = {}
            if user_ticket:
                try:
                    detail_resp = await client.post(
                        self.WECOM_USER_DETAIL_URL,
                        params={"access_token": access_token},
                        json={"user_ticket": user_ticket},
                    )
                    detail_json = detail_resp.json()
                    if detail_json.get("errcode") == 0:
                        sensitive_data = detail_json
                        logger.info("[WeCom SSO] getuserdetail succeeded")
                    else:
                        logger.warning(
                            "[WeCom SSO] getuserdetail failed error_code={}",
                            detail_json.get("errcode", "unknown"),
                        )
                except Exception as exc:
                    logger.warning(
                        "[WeCom SSO] getuserdetail error_type={}",
                        type(exc).__name__,
                    )
            else:
                logger.info(
                    "[WeCom SSO] No user_ticket; "
                    "sensitive fields (avatar/email/mobile) will be unavailable. "
                    "Ensure the WeCom app has 'snsapi_privateinfo' scope."
                )

            # Step 3b: Fetch non-sensitive profile fields from user/get (name, position).
            # These fields are NOT restricted by the June 2022 policy and are available
            # via the standard app access token (IP whitelist required).
            basic_data: dict = {}
            try:
                get_resp = await client.get(
                    self.WECOM_USER_GET_URL,
                    params={"access_token": access_token, "userid": userid},
                )
                get_json = get_resp.json()
                if get_json.get("errcode") == 0:
                    basic_data = get_json
                    logger.info("[WeCom SSO] user/get succeeded")
                else:
                    logger.warning(
                        "[WeCom SSO] user/get failed error_code={}",
                        get_json.get("errcode", "unknown"),
                    )
            except Exception as exc:
                logger.warning(
                    "[WeCom SSO] user/get error_type={}",
                    type(exc).__name__,
                )

            # Pack all data for get_user_info() to consume
            packed_token = json.dumps({
                "userid": userid,
                "sensitive": sensitive_data,  # from getuserdetail (avatar, email, mobile)
                "basic": basic_data,           # from user/get (name, position)
            })
            return {"access_token": packed_token}

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Parse the packed token into a standardized ExternalUserInfo.

        Priority for each field:
          - email: sensitive_data (getuserdetail) > biz_mail > basic_data (user/get)
          - avatar: sensitive_data > basic_data
          - mobile: sensitive_data only (restricted post-2022 in user/get)
          - name: basic_data (non-sensitive, from user/get)
        """
        import json
        try:
            data = json.loads(access_token)
            userid = data.get("userid", "")
            sensitive = data.get("sensitive", {})
            basic = data.get("basic", {})

            # Name from user/get (non-sensitive, always available when IP is whitelisted)
            name = basic.get("name") or f"WeCom {userid}"

            # Email: prefer personal email from getuserdetail, fall back to biz_mail
            email = (
                sensitive.get("email")
                or sensitive.get("biz_mail")
                or basic.get("email")
                or basic.get("biz_mail")
                or ""
            )

            # Avatar from getuserdetail (restricted post-2022 in user/get)
            avatar_url = sensitive.get("avatar") or basic.get("avatar") or ""

            # Mobile only from getuserdetail (restricted post-2022 in user/get)
            mobile = sensitive.get("mobile") or ""

            # Merge raw_data so OrgMember has full context
            raw = {**basic, **sensitive, "userid": userid}

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=userid,
                name=name,
                email=email,
                avatar_url=avatar_url,
                mobile=mobile,
                raw_data=raw,
            )
        except Exception as exc:
            logger.error(
                "[WeCom SSO] get_user_info parse error_type={}",
                type(exc).__name__,
            )
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id="",
                name="",
                raw_data={"error_type": type(exc).__name__},
            )


class GoogleWorkspaceAuthProvider(BaseAuthProvider):
    """Google Workspace OAuth provider implementation for SSO login."""

    provider_type = "google_workspace"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
    GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
    GOOGLE_SSO_SCOPE = "openid email profile"
    GOOGLE_ADMIN_SCOPES = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
        "https://www.googleapis.com/auth/admin.directory.orgunit.readonly",
    ]

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("sso_client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("sso_client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("sso_scope") or self.config.get("scope") or self.GOOGLE_SSO_SCOPE
        self.local_emulator_base_url = local_oidc_emulator_base_url(self.config)

    @staticmethod
    def _normalized_jit_domains(value: object) -> set[str]:
        if isinstance(value, str):
            candidates = value.split(",")
        elif isinstance(value, list):
            candidates = value
        else:
            return set()
        return {
            str(candidate).strip().casefold().rstrip(".")
            for candidate in candidates
            if str(candidate).strip()
        }

    def _tenant_jit_provisioning_allowed(self, user_info: ExternalUserInfo) -> bool:
        """Allow JIT only from an exact, provider-attested Workspace domain."""
        allowed_domains = self._normalized_jit_domains(
            self.config.get("jit_allowed_domains")
        )
        email = str(user_info.email or "").strip().casefold()
        email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        hosted_domain = str(user_info.raw_data.get("hd") or "").strip().casefold().rstrip(".")
        return bool(
            self.config.get("jit_provisioning_enabled") is True
            and allowed_domains
            and user_info.email_verified is True
            and user_info.provider_user_id
            and hosted_domain
            and hosted_domain == email_domain
            and hosted_domain in allowed_domains
        )

    def _build_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        *,
        scopes: str | list[str] | None = None,
        access_type: str = "online",
        prompt: str = "select_account",
        authorization_url: str | None = None,
        code_challenge: str | None = None,
        nonce: str | None = None,
    ) -> str:

        scope_value = scopes or self.scope
        if isinstance(scope_value, list):
            scope_value = " ".join(scope_value)

        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope_value,
            "state": state or "",
            "access_type": access_type,
            "include_granted_scopes": "true",
            "prompt": prompt,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        if nonce:
            params["nonce"] = nonce
        return f"{authorization_url or self.GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.scope,
            access_type="online",
            prompt="select_account",
        )

    async def get_sso_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        *,
        code_challenge: str,
        nonce: str,
    ) -> str:
        authorization_url = (
            f"{self.local_emulator_base_url}/authorize"
            if self.local_emulator_base_url
            else self.GOOGLE_AUTHORIZE_URL
        )
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.scope,
            access_type="online",
            prompt="select_account",
            authorization_url=authorization_url,
            code_challenge=code_challenge,
            nonce=nonce,
        )

    async def get_admin_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.GOOGLE_ADMIN_SCOPES,
            access_type="offline",
            prompt="consent",
        )

    async def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str | None = None,
        *,
        code_verifier: str | None = None,
    ) -> dict:
        token_url = (
            f"{self.local_emulator_base_url}/token"
            if code_verifier and self.local_emulator_base_url
            else self.GOOGLE_TOKEN_URL
        )
        form = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri or self.config.get("redirect_uri"),
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def verify_sso_id_token(self, id_token: str, *, expected_nonce: str) -> dict:
        """Verify signature, audience, issuer, expiry and the request nonce."""
        if not id_token or not self.client_id or not expected_nonce:
            raise ValueError("OIDC token verification inputs are incomplete")
        jwks_url = (
            f"{self.local_emulator_base_url}/jwks"
            if self.local_emulator_base_url
            else self.GOOGLE_JWKS_URL
        )
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            jwks = response.json()
        try:
            claims = jwt.decode(
                id_token,
                jwks,
                algorithms=["RS256"],
                audience=self.client_id,
                options={"verify_iss": False},
            )
        except JWTError as exc:
            raise ValueError("OIDC ID token signature or claims are invalid") from exc

        allowed_issuers = (
            {self.local_emulator_base_url}
            if self.local_emulator_base_url
            else self.GOOGLE_ISSUERS
        )
        issuer = str(claims.get("iss") or "")
        nonce = str(claims.get("nonce") or "")
        subject = str(claims.get("sub") or "")
        if issuer not in allowed_issuers:
            raise ValueError("OIDC ID token issuer is invalid")
        if not nonce or not hmac.compare_digest(nonce, expected_nonce):
            raise ValueError("OIDC ID token nonce is invalid")
        if not subject:
            raise ValueError("OIDC ID token subject is missing")
        return claims

    async def get_sso_user_info(
        self,
        access_token: str,
        *,
        id_token_claims: dict,
    ) -> ExternalUserInfo:
        user_info_url = (
            f"{self.local_emulator_base_url}/userinfo"
            if self.local_emulator_base_url
            else self.GOOGLE_USER_INFO_URL
        )
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            response = await client.get(
                user_info_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            info = response.json()
        subject = str(info.get("sub") or "")
        expected_subject = str(id_token_claims.get("sub") or "")
        if not subject or not hmac.compare_digest(subject, expected_subject):
            raise ValueError("OIDC userinfo subject does not match the ID token")
        merged = dict(info)
        for claim in ("hd", "email", "email_verified", "name", "picture"):
            if claim not in merged and claim in id_token_claims:
                merged[claim] = id_token_claims[claim]
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=subject,
            name=merged.get("name", "") or merged.get("email", ""),
            email=merged.get("email", ""),
            email_verified=merged.get("email_verified") is True,
            avatar_url=merged.get("picture", ""),
            raw_data=merged,
        )

    async def refresh_access_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_openid_profile(self, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        info = await self.fetch_openid_profile(access_token)
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=info.get("sub", ""),
            name=info.get("name", "") or info.get("email", ""),
            email=info.get("email", ""),
            email_verified=bool(info.get("email_verified")),
            avatar_url=info.get("picture", ""),
            raw_data=info,
        )


class MicrosoftTeamsAuthProvider(BaseAuthProvider):
    """Microsoft Teams OAuth provider implementation."""

    provider_type = "microsoft_teams"

    # Will be implemented when needed
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")


class GoogleAuthProvider(BaseAuthProvider):
    """Google OAuth provider implementation."""

    provider_type = "google"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("scope") or "openid profile email"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"{self.GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri or "",
                    "grant_type": "authorization_code",
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(
                    "Google token exchange failed status={} error_code={}",
                    resp.status_code,
                    data.get("error", "unknown"),
                )
                return {}
            return data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            if resp.status_code != 200:
                raise Exception(data.get("error_description") or data.get("error") or "Failed to fetch Google user info")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=data.get("sub", ""),
                name=data.get("name", ""),
                email=data.get("email", ""),
                email_verified=bool(data.get("email_verified")),
                avatar_url=data.get("picture", ""),
                raw_data=data,
            )


class GitHubAuthProvider(BaseAuthProvider):
    """GitHub OAuth provider implementation."""

    provider_type = "github"

    GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
    GITHUB_USER_INFO_URL = "https://api.github.com/user"
    GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

    def __init__(self, provider: IdentityProvider | None = None, config: dict | None = None):
        super().__init__(provider, config)
        self.client_id = self.config.get("client_id") or self.config.get("app_id")
        self.client_secret = self.config.get("client_secret") or self.config.get("app_secret")
        self.scope = self.config.get("scope") or "read:user user:email"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "scope": self.scope,
        }
        if state:
            params["state"] = state
        return f"{self.GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(
                    "GitHub token exchange failed status={} error_code={}",
                    resp.status_code,
                    data.get("error", "unknown"),
                )
                return {}
            return data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            user_resp = await client.get(self.GITHUB_USER_INFO_URL, headers=headers)
            user_data = user_resp.json()
            if user_resp.status_code != 200:
                raise Exception(user_data.get("message") or "Failed to fetch GitHub user info")

            # ``GET /user`` does not attest that its public email is verified.
            # Only the authenticated email endpoint's verified entries may be
            # exposed as an ownership-verified provider claim.
            email = ""
            emails_resp = await client.get(self.GITHUB_EMAILS_URL, headers=headers)
            emails_data = emails_resp.json()
            if emails_resp.status_code == 200 and isinstance(emails_data, list):
                verified_emails = [
                    item
                    for item in emails_data
                    if item.get("verified") and item.get("email")
                ]
                preferred = next(
                    (item for item in verified_emails if item.get("primary")),
                    None,
                )
                email = str((preferred or (verified_emails[0] if verified_emails else {})).get("email", ""))

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=str(user_data.get("id", "")),
                name=user_data.get("name") or user_data.get("login") or "",
                email=email,
                email_verified=bool(email),
                avatar_url=user_data.get("avatar_url", ""),
                raw_data=user_data,
            )


# Provider class mapping
PROVIDER_CLASSES = {
    "feishu": FeishuAuthProvider,
    "dingtalk": DingTalkAuthProvider,
    "wecom": WeComAuthProvider,
    "google_workspace": GoogleWorkspaceAuthProvider,
    "microsoft_teams": MicrosoftTeamsAuthProvider,
    "google": GoogleAuthProvider,
    "github": GitHubAuthProvider,
}
