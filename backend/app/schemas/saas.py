"""Pydantic schemas for SaaS admin APIs (phase 1)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ALLOWED_SAAS_TIERS = {"lite", "pro", "ultra"}
ALLOWED_MODEL_ROUTE_MODALITIES = {"text", "image", "video"}
ALLOWED_BILLING_ACTIONS = {"chat", "heartbeat", "image", "audio", "music", "video", "tool", "browser", "search", "trigger"}
ALLOWED_BILLING_MODALITIES = {"text", "image", "audio", "music", "video", "multimodal"}


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    return value or None


class ModelRouteOut(BaseModel):
    """Model route: SaaS tier + modality -> LLMModel."""

    id: uuid.UUID
    saas_tier: str
    modality: str
    llm_model_id: uuid.UUID
    priority: int
    fallback_route_id: uuid.UUID | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ModelRouteCreateIn(BaseModel):
    """Create a model route."""

    saas_tier: str
    modality: str
    llm_model_id: uuid.UUID
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    fallback_route_id: uuid.UUID | None = None
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")

    @field_validator("saas_tier")
    @classmethod
    def validate_saas_tier(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_SAAS_TIERS:
            raise ValueError("saas_tier must be lite, pro, or ultra")
        return normalized

    @field_validator("modality")
    @classmethod
    def validate_modality(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_MODEL_ROUTE_MODALITIES:
            raise ValueError("modality must be text, image, or video")
        return normalized


class ModelRouteUpdateIn(BaseModel):
    """Update a model route."""

    saas_tier: str | None = None
    modality: str | None = None
    llm_model_id: uuid.UUID | None = None
    priority: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    fallback_route_id: uuid.UUID | None = None
    enabled: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value):
        if isinstance(value, dict):
            nullable_only = {"fallback_route_id"}
            invalid = sorted(
                key
                for key, item in value.items()
                if item is None and key not in nullable_only
            )
            if invalid:
                raise ValueError(
                    f"explicit null is not allowed for: {', '.join(invalid)}"
                )
        return value

    @field_validator("saas_tier")
    @classmethod
    def validate_saas_tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in ALLOWED_SAAS_TIERS:
            raise ValueError("saas_tier must be lite, pro, or ultra")
        return normalized

    @field_validator("modality")
    @classmethod
    def validate_modality(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in ALLOWED_MODEL_ROUTE_MODALITIES:
            raise ValueError("modality must be text, image, or video")
        return normalized


class MediaRouteOut(BaseModel):
    """Effective platform-owned MiniMax generation route."""

    modality: str
    tier: str
    provider: str
    tool_name: str
    model: str
    settings: dict[str, str | int | bool]
    valid_models: list[str]
    enabled: bool
    tool_enabled: bool
    pool_available: bool
    available: bool
    source: str
    billing_mode: str
    estimated_credits: int | None = None
    billing_unit: str


class MediaRouteUpdateIn(BaseModel):
    """Update one tier-specific media route without exposing credentials."""

    model: str | None = None
    sample_rate: int | None = Field(default=None, ge=8000, le=48000)
    bitrate: int | None = Field(default=None, ge=32000, le=320000)
    duration: int | None = Field(default=None, ge=1, le=30)
    resolution: str | None = None
    enabled: bool | None = None
    reset_to_default: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model is required")
        return normalized

    @field_validator("resolution")
    @classmethod
    def normalize_resolution(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class BillingRuleOut(BaseModel):
    """Fixed credit cost rule."""

    id: uuid.UUID
    action: str
    modality: str | None = None
    tier: str | None = None
    unit: str
    credit_cost: int
    enabled: bool
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BillingRuleCreateIn(BaseModel):
    """Create a billing rule."""

    action: str
    modality: str | None = None
    tier: str | None = None
    unit: str = "call"
    credit_cost: int = Field(ge=0)
    enabled: bool = True
    priority: int = 0

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_BILLING_ACTIONS:
            raise ValueError(f"unsupported billing action: {value}")
        return normalized

    @field_validator("modality")
    @classmethod
    def validate_modality(cls, value: str | None) -> str | None:
        normalized = _normalize_optional(value)
        if normalized and normalized not in ALLOWED_BILLING_MODALITIES:
            raise ValueError(f"unsupported billing modality: {value}")
        return normalized

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        normalized = _normalize_optional(value)
        if normalized and normalized not in ALLOWED_SAAS_TIERS:
            raise ValueError(f"unsupported SaaS tier: {value}")
        return normalized


class BillingRuleUpdateIn(BaseModel):
    """Update a billing rule."""

    action: str | None = None
    modality: str | None = None
    tier: str | None = None
    unit: str | None = None
    credit_cost: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    priority: int | None = None

    _validate_action = field_validator("action")(BillingRuleCreateIn.validate_action.__func__)
    _validate_modality = field_validator("modality")(BillingRuleCreateIn.validate_modality.__func__)
    _validate_tier = field_validator("tier")(BillingRuleCreateIn.validate_tier.__func__)


class CreditPackCreateIn(BaseModel):
    """Create a credit pack."""

    code: str
    name: str
    credits: int = Field(gt=0)
    price_cents: int = Field(ge=0)
    currency: str = "CNY"
    applicable_plan_ids: list[uuid.UUID] | None = None
    is_active: bool = True
    sort_order: int = 0

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return normalized

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("code is required")
        return normalized


class CreditPackUpdateIn(BaseModel):
    """Update a credit pack."""

    code: str | None = None
    name: str | None = None
    credits: int | None = Field(default=None, gt=0)
    price_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    applicable_plan_ids: list[uuid.UUID] | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    _validate_currency = field_validator("currency")(CreditPackCreateIn.validate_currency.__func__)
    _validate_code = field_validator("code")(CreditPackCreateIn.validate_code.__func__)


class SaasTenantOut(BaseModel):
    """Tenant summary for SaaS admin tenant list."""

    tenant_id: uuid.UUID
    tenant_name: str | None = None
    plan_code: str | None = None
    subscription_status: str | None = None
    period_end: datetime | None = None
    seats_total: int = 0
    seats_used: int = 0
    credits_balance: int = 0


class AssignSubscriptionIn(BaseModel):
    """Platform admin assigns a plan to tenants."""

    tenant_ids: list[uuid.UUID]
    plan_id: uuid.UUID
    period_days: int | None = None
    confirm: bool = False
    audit_reason: str | None = None


class InitializeFreeSubscriptionsIn(BaseModel):
    """Initialize Free subscriptions for tenants that do not have an active subscription."""

    tenant_ids: list[uuid.UUID] | None = None
    include_inactive: bool = False
    confirm: bool = False
    audit_reason: str | None = None


class InitializeFreeSubscriptionsOut(BaseModel):
    """Bulk Free initialization result."""

    total_candidates: int
    created: int
    skipped_existing: int
    tenant_ids: list[uuid.UUID]


class GrantCreditsIn(BaseModel):
    """Platform admin grants credits to tenants."""

    tenant_ids: list[uuid.UUID]
    amount: int = Field(gt=0)
    reason: str
    confirm: bool = False
    audit_reason: str | None = None


class MediaFailureRemediationIn(BaseModel):
    """Dry-run or apply an exact refundable media-task remediation."""

    task_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    expected_tenant_id: uuid.UUID
    incident_key: str = Field(min_length=1, max_length=200)
    apply: bool = False


class MediaProviderDebtResolutionIn(BaseModel):
    """Evidence-backed resolution for ambiguous or non-refundable media debt."""

    task_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    expected_tenant_id: uuid.UUID
    incident_key: str = Field(min_length=1, max_length=200)
    evidence_ref: str = Field(min_length=1, max_length=500)
    resolution: str
    apply: bool = False

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {
            "provider_rejected",
            "provider_accepted",
            "close_asset_loss",
        }:
            raise ValueError(
                "resolution must be provider_rejected, provider_accepted, or close_asset_loss"
            )
        return normalized


class LLMCreditHoldResolutionIn(BaseModel):
    """Evidence-backed resolution for ambiguous LLM provider holds."""

    reservation_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    expected_tenant_id: uuid.UUID
    incident_key: str = Field(min_length=1, max_length=200)
    evidence_ref: str = Field(min_length=1, max_length=500)
    resolution: str
    settlement_amount: int | None = Field(default=None, ge=0)
    apply: bool = False

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"provider_completed", "provider_not_accepted"}:
            raise ValueError(
                "resolution must be provider_completed or provider_not_accepted"
            )
        return normalized


class MarkOrderPaidIn(BaseModel):
    """Platform admin marks an order as paid (mock payment in phase 1)."""

    order_id: uuid.UUID
