"""Pydantic schemas for SaaS admin APIs (phase 1)."""

import uuid
from datetime import datetime
from typing import Literal

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
    route_purpose: str = "input_understanding"

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


class MediaProviderReadinessOut(BaseModel):
    """One provider's evidence levels for a media modality."""

    provider: str
    configured: bool
    account_verified: bool
    generation_observed: bool
    plan_tiers: list[str]
    account_receipt: dict[str, object] | None = None
    generation_receipt: dict[str, object] | None = None


class MediaExecutionStrategyRouteOut(BaseModel):
    """Account-pool readiness for one provider-neutral execution strategy."""

    strategy: str
    provider_order: list[str]
    available_providers: list[str]
    preferred_provider: str
    alternate_provider: str
    preferred_ready: bool
    executable_without_alternate_confirmation: bool
    alternate_confirmation_required: bool


class MediaRouteOut(BaseModel):
    """Platform account readiness; actual execution lives only in task receipts."""

    modality: str
    tier: str
    route_purpose: str = "media_generation"
    provider: str
    routing_mode: str
    route_semantics: str
    provider_order: list[str]
    available_providers: list[str]
    execution_strategies: list[MediaExecutionStrategyRouteOut]
    primary_provider: str
    degraded_providers: list[str]
    capability_status: str
    reason_code: str | None = None
    recommended_action: str
    evaluation_source: str
    readiness_status: str
    quality_evidence_status: str
    provider_readiness: list[MediaProviderReadinessOut]
    fallback_provider: str
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
    volcengine_profile: dict[str, str] | None = None
    minimax_allowance: dict[str, object] | None = None
    provider_quotes: dict[str, dict[str, object]] = Field(default_factory=dict)
    pricing_version: str | None = None


class MediaRouteUpdateIn(BaseModel):
    """Update one tier-specific MiniMax fallback profile."""

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


class LLMSystemCostReceiptOut(BaseModel):
    """Secret-free platform-cost receipt for one system-owned LLM call."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    group_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    call_index: int
    operation: str
    model_id: uuid.UUID
    credential_id: uuid.UUID | None = None
    provider: str
    model: str
    provider_service_tier: str
    status: str
    provider_outcome: str
    usage_source: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    estimated_tokens: int
    budget_reservation_credits: int
    request_input_token_upper_bound: int
    request_max_output_tokens: int
    system_cost_credits: int | None = None
    cost_status: str
    reconciliation_error_code: str | None = None
    provider_accepted_at: datetime | None = None
    finalized_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LLMSystemCostSummaryOut(BaseModel):
    """Bounded aggregate for platform operations and margin reconciliation."""

    receipt_count: int
    finalized_count: int
    reconciling_count: int
    provider_inflight_count: int
    reconciled_count: int
    voided_count: int
    unpriced_count: int
    total_tokens: int
    estimated_tokens: int
    system_cost_credits: int
    active_budget_credits: int


class LLMSystemCostResolutionOut(BaseModel):
    """Append-only, secret-free receipt for a Planning cost transition."""

    id: uuid.UUID
    receipt_id: uuid.UUID
    tenant_id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    action: str
    source: str
    evidence_ref: str
    reason: str
    previous_status: str
    resulting_status: str
    previous_provider_outcome: str
    resulting_provider_outcome: str
    reported_system_cost_credits: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PlanningCostResolutionIn(BaseModel):
    """Evidence-backed terminal disposition for one ambiguous receipt."""

    expected_tenant_id: uuid.UUID
    expected_status: Literal["reconciling"]
    expected_provider_outcome: Literal["acceptance_unknown"]
    disposition: Literal["confirm_not_accepted", "settle_accepted"]
    evidence_ref: str = Field(min_length=8, max_length=500)
    reason: str = Field(min_length=8, max_length=500)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    system_cost_credits: int | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid")

    @field_validator("evidence_ref", "reason")
    @classmethod
    def normalize_evidence_text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("value must contain at least 8 non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def validate_resolution_shape(self):
        usage_values = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
            self.system_cost_credits,
        )
        if self.disposition == "confirm_not_accepted":
            if any(value is not None for value in usage_values):
                raise ValueError(
                    "confirm_not_accepted must not include usage or system cost"
                )
            return self
        if any(value is None for value in usage_values):
            raise ValueError(
                "settle_accepted requires complete usage and system cost"
            )
        assert self.total_tokens is not None
        assert self.input_tokens is not None
        assert self.output_tokens is not None
        if self.total_tokens <= 0 or self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError(
                "total_tokens must be positive and cover input_tokens plus output_tokens"
            )
        return self


class PlanningCostResolutionResultOut(BaseModel):
    receipt: LLMSystemCostReceiptOut
    resolution: LLMSystemCostResolutionOut
    replayed: bool


class PlanningCostStaleScanIn(BaseModel):
    """Preview/apply the server-configured stale inflight transition."""

    apply: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    evidence_ref: str = Field(min_length=8, max_length=500)
    reason: str = Field(min_length=8, max_length=500)

    model_config = ConfigDict(extra="forbid")

    _normalize_evidence_ref = field_validator("evidence_ref")(
        PlanningCostResolutionIn.normalize_evidence_text.__func__
    )
    _normalize_reason = field_validator("reason")(
        PlanningCostResolutionIn.normalize_evidence_text.__func__
    )


class PlanningCostStaleScanOut(BaseModel):
    cutoff: datetime
    candidate_receipt_ids: list[uuid.UUID]
    candidate_count: int
    applied_count: int


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


class ManualOrderDecisionIn(BaseModel):
    """Exact, evidence-backed disposition for one manual payment order."""

    expected_tenant_id: uuid.UUID
    expected_status: Literal["pending", "canceled"]
    disposition: Literal[
        "keep_pending",
        "mark_paid",
        "cancel_expired",
        "cancel_test",
        "cancel_invalid",
        "restore_pending",
    ]
    evidence_ref: str = Field(min_length=8, max_length=500)
    reason: str = Field(min_length=8, max_length=500)
    rollback_of_decision_id: uuid.UUID | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("evidence_ref", "reason")
    @classmethod
    def normalize_operator_text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("value must contain at least 8 non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def validate_rollback_shape(self):
        if self.disposition == "restore_pending":
            if self.rollback_of_decision_id is None:
                raise ValueError("restore_pending requires rollback_of_decision_id")
            if self.expected_status != "canceled":
                raise ValueError("restore_pending requires expected_status=canceled")
        elif self.rollback_of_decision_id is not None:
            raise ValueError("rollback_of_decision_id is only valid for restore_pending")
        elif self.expected_status != "pending":
            raise ValueError(f"{self.disposition} requires expected_status=pending")
        return self


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
