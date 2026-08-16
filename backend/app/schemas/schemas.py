"""Pydantic schemas for request/response validation."""

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.core.identity_canonicalization import normalize_username, username_looks_like_contact


def _validated_username(value: str | None) -> str | None:
    if value is None:
        return None
    username = normalize_username(value)
    if not username:
        raise ValueError("Username is required")
    if username_looks_like_contact(username):
        raise ValueError("Username cannot be an email address or phone number")
    return username


# ─── Auth ───────────────────────────────────────────────

class UserRegister(BaseModel):
    """Legacy combined registration - kept for backward compatibility."""
    username: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = None
    invitation_code: str | None = None
    # SSO registration fields
    provider: str | None = Field(None, description="Provider type for SSO registration (feishu, dingtalk, etc.)")
    provider_code: str | None = Field(None, description="OAuth code for SSO registration")

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return _validated_username(value) or ""


class RegisterInitRequest(BaseModel):
    """Step 1: Initialize registration with account credentials."""
    username: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = None
    invitation_code: str | None = None
    target_tenant_id: uuid.UUID | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return _validated_username(value) or ""


class RegisterInitResponse(BaseModel):
    """Response after step 1 - user created, needs email verification."""
    user_id: uuid.UUID
    email: str
    access_token: str
    message: str = "Registration initiated. Please verify your email."
    user: "UserOut" # Include full user info
    needs_company_setup: bool = True
    target_tenant_id: uuid.UUID | None = None


class RegisterCompleteRequest(BaseModel):
    """Step 3: Complete registration after email verification."""
    token: str = Field(min_length=6, max_length=512, description="Email verification token")


class RegisterCompleteResponse(BaseModel):
    """Response after successful registration completion."""
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"
    needs_company_setup: bool = False


class SSORegisterRequest(BaseModel):
    """Legacy payload retained only for the retired /register/sso contract."""
    provider: str = Field(description="Legacy public social provider type")
    code: str = Field(description="Legacy OAuth authorization code")
    invitation_code: str | None = None


class UserLogin(BaseModel):
    login_identifier: str = Field(description="Email address for login")
    password: str
    tenant_id: uuid.UUID | None = None  # Optional: when set, restrict login to users of this tenant


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    new_password: str = Field(min_length=6, max_length=128)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=6, max_length=512)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class NeedsVerificationResponse(BaseModel):
    """Response when user needs to verify email before continuing."""
    needs_verification: bool = True
    email: str
    message: str = "Email already registered but not verified. Please enter the verification token."


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"
    identity: "IdentityOut | None" = None
    needs_company_setup: bool = False
    tenant_name: str | None = None


class TenantChoice(BaseModel):
    """Multi-tenant login: tenant selection info."""
    tenant_id: uuid.UUID | None
    tenant_name: str
    tenant_slug: str
    logo_url: str | None = None
    membership_role: str | None = None


class MultiTenantResponse(BaseModel):
    """Response when multiple tenants match the same login identifier."""
    requires_tenant_selection: bool = True
    login_identifier: str
    tenants: list[TenantChoice]
    # Opaque short-lived token used by OAuth flows (no password available for re-auth).
    # When present, the client must POST to /auth/select-oauth-tenant instead of re-calling /auth/login.
    pending_token: str | None = None



class TenantSwitchRequest(BaseModel):
    tenant_id: uuid.UUID


class TenantSwitchResponse(BaseModel):
    access_token: str
    target_tenant_id: uuid.UUID
    token_type: str = "bearer"
    redirect_url: str | None = None
    message: str | None = None


class IdentityOut(BaseModel):
    """Global identity information."""
    id: uuid.UUID
    email: str | None = None
    phone: str | None = None
    username: str | None = None
    is_active: bool
    is_platform_admin: bool
    email_verified: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserOut(BaseModel):
    id: uuid.UUID
    identity_id: uuid.UUID | None = None
    # ``id`` remains the legacy membership identifier.  New clients should
    # consume the explicit identity/membership/access contract below.
    membership_id: uuid.UUID | None = None
    username: str | None = None
    email: str | None = None
    display_name: str
    avatar_url: str | None = None
    role: str
    is_platform_admin: bool = False
    membership_role: Literal["member", "org_admin", "org_owner"] | None = None
    global_roles: list[str] = Field(default_factory=list)
    effective_capabilities: list[str] = Field(default_factory=list)
    available_surfaces: list[Literal["work", "company_admin", "platform_admin"]] = Field(
        default_factory=list
    )
    pending_invitation_count: int = 0
    current_support_session: dict[str, Any] | None = None
    tenant_id: uuid.UUID | None = None
    title: str | None = None
    timezone: str | None = None
    work_hours_start: str | None = None
    work_hours_end: str | None = None
    primary_mobile: str | None = None
    registration_source: str | None = None
    preferred_chat_tier: Literal["lite", "pro", "ultra"] | None = None
    preferred_chat_tier_revision: int = 0
    is_active: bool
    email_verified: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class IdentityProviderOut(BaseModel):
    id: uuid.UUID
    provider_type: str
    name: str
    is_active: bool
    sso_login_enabled: bool = False
    config: dict | None = None
    tenant_id: uuid.UUID | None = None
    updated_at: datetime | None = None
    created_at: datetime
    sso_domain: str | None = None
    readiness: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class OAuthAuthorizeResponse(BaseModel):
    authorization_url: str


class OAuthCallbackRequest(BaseModel):
    code: str | None = None          # Step 1: initial OAuth code exchange
    state: str
    redirect_uri: str | None = None
    # Step 2: tenant selection (no code needed)
    tenant_id: str | None = None
    pending_token: str | None = None


class IdentityBindRequest(BaseModel):
    provider_type: str
    code: str  # OAuth code for binding
    current_password: str | None = Field(default=None, min_length=1, max_length=128)


class IdentityUnbindRequest(BaseModel):
    provider_type: str
    current_password: str | None = Field(default=None, min_length=1, max_length=128)


class UserUpdate(BaseModel):
    username: str | None = None
    email: EmailStr | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    title: str | None = None
    primary_mobile: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        return _validated_username(value)


class SelfUserUpdate(UserUpdate):
    """Profile update plus password proof for global recovery/login fields."""

    current_password: str | None = Field(default=None, min_length=1, max_length=128)


# ─── Agent ──────────────────────────────────────────────

class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100, description="Agent name, 2-100 characters")
    agent_type: str = "native"  # native | openclaw
    role_description: str = Field(default="", max_length=500, description="Role description, max 500 characters")
    bio: str | None = None
    welcome_message: str | None = None
    avatar_url: str | None = None
    # Soul
    personality: str = ""
    boundaries: str = ""
    # Model
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    # SaaS tier selection (Lite/Pro/Ultra); preferred over legacy model IDs
    preferred_tier: str | None = None
    preferred_modality: str | None = "text"
    # Permissions
    permission_scope_type: str = "company"  # company | user | custom
    permission_scope_ids: list[uuid.UUID] = []
    permission_access_level: str = "use"  # use | manage
    # Target tenant (admin-only override; otherwise ignored)
    tenant_id: uuid.UUID | None = None
    # Template
    template_id: uuid.UUID | None = None
    # Autonomy
    autonomy_policy: dict | None = None
    # Token limits
    max_tokens_per_day: int | None = None
    max_tokens_per_month: int | None = None
    # Skills to copy into agent workspace
    skill_ids: list[uuid.UUID] = []


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    avatar_url: str | None = None
    role_description: str
    bio: str | None = None
    welcome_message: str | None = None
    status: str
    creator_id: uuid.UUID
    creator_username: str | None = None  # Populated by API layer; not in ORM model directly
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    preferred_tier: str | None = None
    preferred_modality: str | None = None
    autonomy_policy: dict
    tokens_used_today: int
    tokens_used_month: int
    tokens_used_total: int = 0
    cache_read_tokens_today: int = 0
    cache_read_tokens_month: int = 0
    cache_read_tokens_total: int = 0
    cache_creation_tokens_today: int = 0
    cache_creation_tokens_month: int = 0
    cache_creation_tokens_total: int = 0
    max_tokens_per_day: int | None = None
    max_tokens_per_month: int | None = None
    context_window_size: int = 100
    max_tool_rounds: int = 50
    max_triggers: int = 20
    min_poll_interval_min: int = 5
    webhook_rate_limit: int = 5
    heartbeat_enabled: bool = False
    heartbeat_interval_minutes: int = 240
    heartbeat_active_hours: str = "09:00-18:00"
    last_heartbeat_at: datetime | None = None
    timezone: str | None = None
    expires_at: datetime | None = None
    is_expired: bool = False
    is_system: bool = False
    # Viewer-specific roster role. Agent list responses derive this from
    # onboarding and reviewed template identity; clients must not infer it
    # from names. Other Agent responses may leave it unset.
    product_role: Literal[
        "personal_assistant",
        "legacy_personal_assistant",
        "agent_employee",
    ] | None = None
    legacy_assistant_disposition: Literal[
        "active",
        "archived",
        "converted",
    ] | None = None
    access_mode: str = "company"
    company_access_level: str = "use"
    llm_calls_today: int = 0
    max_llm_calls_per_day: int = 1000
    agent_type: str = "native"
    template_revision_applied: int | None = None
    template_sync_status: str = "current"
    template_sync_details: dict = Field(default_factory=dict)
    template_synced_at: datetime | None = None
    openclaw_last_seen: datetime | None = None
    deletion_requested_at: datetime | None = None
    deletion_state: str = "active"
    unread_count: int = 0
    has_api_key: bool = False
    api_key_hash: str | None = None
    # True when the current viewer already has an onboarding row for this
    # agent. Computed per-request by the API layer from the junction table;
    # not an ORM attribute, so callers must set it explicitly. Defaults to
    # True so list endpoints that don't care about onboarding don't leak
    # stale "needs onboarding" UI to users they shouldn't prompt.
    onboarded_for_me: bool = True
    # Release capabilities are server-authored and let the UI distinguish an
    # intentionally paused platform lane from an authorization error.
    automation_execution_enabled: bool = False
    approval_execution_enabled: bool = False
    execution_capabilities: dict[str, bool] = Field(default_factory=dict)
    created_at: datetime
    last_active_at: datetime | None = None
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class AgentUpdate(BaseModel):
    name: str | None = None
    role_description: str | None = None
    bio: str | None = None
    welcome_message: str | None = None
    avatar_url: str | None = None
    autonomy_policy: dict | None = None
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    preferred_tier: str | None = None
    preferred_modality: str | None = None
    context_window_size: int | None = Field(default=None, ge=1, le=500)
    max_tokens_per_day: int | None = None
    max_tokens_per_month: int | None = None
    max_tool_rounds: int | None = None
    max_triggers: int | None = None
    min_poll_interval_min: int | None = None
    webhook_rate_limit: int | None = None
    heartbeat_enabled: bool | None = None
    heartbeat_interval_minutes: int | None = None
    heartbeat_active_hours: str | None = None
    timezone: str | None = None
    expires_at: datetime | None = None  # Admin only — extend agent expiry


class LegacyAssistantDispositionUpdate(BaseModel):
    action: Literal["archive", "convert_to_employee", "restore_history"]
    expected_disposition: Literal["active", "archived", "converted"]


class AgentStatusOut(BaseModel):
    """Agent status from state.json."""
    agent_id: uuid.UUID
    name: str
    status: str
    current_task: str | None = None
    last_active: datetime | None = None
    channel_status: dict = {}
    stats: dict = {}


# ─── Task ───────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    type: str = "todo"  # todo | supervision
    priority: str = "medium"
    due_date: datetime | None = None
    # Supervision fields
    supervision_target_name: str | None = None
    supervision_channel: str | None = None
    remind_schedule: str | None = None


class TaskOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    agent_id: uuid.UUID
    title: str
    description: str | None = None
    intent: str | None = None
    origin_type: str = "legacy_agent_task"
    executor_kind: str = "agent_employee"
    executor_snapshot: dict = Field(default_factory=dict)
    work_type: str = "general"
    work_statement: dict = Field(default_factory=dict)
    confirmation_fingerprint: str | None = None
    confirmed_at: datetime | None = None
    group_id: uuid.UUID | None = None
    client_request_id: uuid.UUID | None = None
    type: str
    status: str
    priority: str
    assignee: str
    created_by: uuid.UUID
    creator_username: str | None = None
    due_date: datetime | None = None
    supervision_target_name: str | None = None
    supervision_channel: str | None = None
    remind_schedule: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: datetime | None = None
    supervision_target_name: str | None = None
    remind_schedule: str | None = None


class TaskLogCreate(BaseModel):
    content: str


class TaskLogOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── LLM ────────────────────────────────────────────────

class LLMModelCreate(BaseModel):
    provider: str
    model: str
    api_key: str = ""  # empty for platform models (key lives in the credential pool)
    base_url: str | None = None
    label: str
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens_per_day: int | None = None
    enabled: bool = True
    supports_vision: bool = False
    max_output_tokens: int | None = Field(None, ge=1, le=1_000_000)
    request_timeout: int | None = None
    modality: str = "text"
    tier: str = "standard"

class LLMModelUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    label: str | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens_per_day: int | None = None
    enabled: bool | None = None
    supports_vision: bool | None = None
    max_output_tokens: int | None = Field(None, ge=1, le=1_000_000)
    request_timeout: int | None = None
    modality: str | None = None
    tier: str | None = None


class LLMModelOut(BaseModel):
    id: uuid.UUID
    provider: str
    model: str
    base_url: str | None = None
    label: str
    temperature: float | None = None
    api_key_masked: str = ""
    max_tokens_per_day: int | None = None
    enabled: bool
    supports_vision: bool = False
    verification_status: str | None = None
    last_verified_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    supports_tool_calling: bool | None = None
    tool_calling_capability_source: str | None = None
    tool_calling_checked_at: datetime | None = None
    tool_calling_error: str | None = None
    max_output_tokens: int | None = None
    request_timeout: int | None = None
    modality: str = "text"
    tier: str = "standard"
    created_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


# ─── Channel Config ─────────────────────────────────────

class ChannelConfigCreate(BaseModel):
    channel_type: str = "feishu"
    app_id: str
    app_secret: str
    encrypt_key: str | None = None
    verification_token: str | None = None
    extra_config: dict | None = None


class ChannelConfigOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    channel_type: str
    app_id: str | None = None
    app_secret_configured: bool = False
    encrypt_key_configured: bool = False
    verification_token_configured: bool = False
    configured_secret_fields: list[str] = Field(default_factory=list)
    is_configured: bool
    is_connected: bool
    last_tested_at: datetime | None = None
    extra_config: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def redact_channel_credentials(cls, value):
        """Convert a ChannelConfig into a status-only public representation."""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            getter = value.get
        else:
            def getter(key, default=None):
                return getattr(value, key, default)

        raw_extra = getter("extra_config", {}) or {}
        configured_secret_fields: list[str] = []
        private_extra_keys = {
            "access_token",
            "api_key",
            "app_secret",
            "bot_secret",
            "bot_token",
            "client_secret",
            "conversation_id",
            "encrypt_key",
            "password",
            "private_key",
            "secret",
            "service_url",
            "signing_secret",
            "token",
            "verification_token",
            "verify_token",
        }
        compact_private_keys = {
            re.sub(r"[^a-z0-9]", "", key.lower()) for key in private_extra_keys
        }

        def is_private_key(key: object) -> bool:
            compact = re.sub(r"[^a-z0-9]", "", str(key).strip().lower())
            return compact in compact_private_keys or compact.endswith(
                ("secret", "token", "password", "privatekey")
            )

        def scrub_extra(item, path: str = ""):
            if isinstance(item, dict):
                public: dict = {}
                for key, nested in item.items():
                    field_path = f"{path}.{key}" if path else str(key)
                    if is_private_key(key):
                        if nested:
                            configured_secret_fields.append(field_path)
                        continue
                    public[key] = scrub_extra(nested, field_path)
                return public
            if isinstance(item, list):
                return [scrub_extra(nested, path) for nested in item]
            return item

        extra_config = scrub_extra(raw_extra) if isinstance(raw_extra, dict) else {}

        return {
            "id": getter("id"),
            "agent_id": getter("agent_id"),
            "channel_type": getter("channel_type"),
            "app_id": getter("app_id"),
            "app_secret_configured": bool(
                getter("app_secret_configured", False) or getter("app_secret")
            ),
            "encrypt_key_configured": bool(
                getter("encrypt_key_configured", False) or getter("encrypt_key")
            ),
            "verification_token_configured": bool(
                getter("verification_token_configured", False)
                or getter("verification_token")
            ),
            "configured_secret_fields": sorted(
                set(configured_secret_fields)
                | set(getter("configured_secret_fields", []) or [])
            ),
            "is_configured": bool(getter("is_configured", False)),
            "is_connected": bool(getter("is_connected", False)),
            "last_tested_at": getter("last_tested_at"),
            "extra_config": extra_config,
            "created_at": getter("created_at"),
        }


# ─── Approval ───────────────────────────────────────────

class ApprovalRequestOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    action_type: str
    details: dict
    status: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    execution_status: str | None = None
    execution_claimed_at: datetime | None = None
    execution_finished_at: datetime | None = None
    execution_attempts: int = 0
    execution_result_summary: dict = Field(default_factory=dict)
    execution_error_code: str | None = None
    execution_available: bool = False
    execution_paused_reason: str | None = None

    model_config = {"from_attributes": True}


class ApprovalAction(BaseModel):
    action: Literal["approve", "reject"]


# ─── Enterprise Info ────────────────────────────────────

class UserInviteRequest(BaseModel):
    emails: list[EmailStr] = Field(..., description="List of emails to invite")

class EnterpriseInfoUpdate(BaseModel):
    content: dict
    visible_roles: list[str] = []


class EnterpriseInfoOut(BaseModel):
    id: uuid.UUID
    info_type: str
    content: dict
    version: int
    visible_roles: list
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Chat ───────────────────────────────────────────────

class ChatMessageOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    content: str
    thinking: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSend(BaseModel):
    content: str = Field(min_length=1)


# ─── Audit Log ──────────────────────────────────────────

class AuditLogOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    action: str
    details: dict
    ip_address: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Generic ────────────────────────────────────────────

class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int = 1
    page_size: int = 20


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


# ─── Gateway (OpenClaw) ─────────────────────────────────

class GatewayHistoryItem(BaseModel):
    role: str  # "user" or "assistant"
    content: str
    sender_name: str | None = None
    created_at: datetime


class GatewayRelationshipItem(BaseModel):
    name: str
    type: str  # "human" or "agent"
    role: str | None = None  # e.g. "collaborator", "supervisor"
    description: str | None = None
    channels: list[str] = []  # e.g. ["feishu"], ["agent"]


class GatewayMessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: str | None = None
    sender_agent_name: str | None = None
    sender_user_name: str | None = None
    sender_user_id: str | None = None
    content: str
    created_at: datetime
    delivery_attempt: int = 1
    history: list[GatewayHistoryItem] = []



class GatewayPollResponse(BaseModel):
    messages: list[GatewayMessageOut] = []
    relationships: list[GatewayRelationshipItem] = []


class GatewayReportRequest(BaseModel):
    message_id: uuid.UUID
    # Compatibility for already-installed OpenClaw skills: omission is valid
    # only for the first, still-live lease. Reclaimed work must always carry
    # the exact generation returned by poll.
    delivery_attempt: int | None = Field(default=None, ge=1)
    result: str = Field(min_length=1)


class GatewaySendMessageRequest(BaseModel):
    target: str  # Name of target person or agent
    content: str = Field(min_length=1)
    channel: str | None = None  # Optional: "feishu", "agent", etc. Auto-detected if omitted.
    message_id: uuid.UUID | None = None  # Durable Runtime idempotency key.
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
