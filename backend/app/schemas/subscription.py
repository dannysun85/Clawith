"""Pydantic schemas for subscription APIs (阶段0)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PlanOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    tier: int
    period: str
    price_cents: int
    currency: str
    max_agents: int
    max_llm_calls_per_day: int
    message_limit: int
    message_period: str
    max_triggers: int
    credits_per_period: int
    allowed_modalities: list | None = None
    allowed_tiers: list | None = None
    features: dict | None = None
    is_active: bool
    sort_order: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PlanCreateIn(BaseModel):
    """Admin creates a plan (code is immutable after creation)."""

    code: str
    name: str
    tier: int = 0
    period: str = "monthly"
    price_cents: int = 0
    currency: str = "CNY"
    max_agents: int = 2
    max_llm_calls_per_day: int = 1000
    message_limit: int = 50
    message_period: str = "permanent"
    max_triggers: int = 20
    credits_per_period: int = 0
    allowed_modalities: list | None = None
    allowed_tiers: list | None = None
    features: dict | None = None
    sort_order: int = 0


class PlanUpdateIn(BaseModel):
    """Admin updates a plan with a required optimistic-lock precondition."""

    name: str | None = None
    tier: int | None = None
    period: str | None = None
    price_cents: int | None = None
    currency: str | None = None
    max_agents: int | None = None
    max_llm_calls_per_day: int | None = None
    message_limit: int | None = None
    message_period: str | None = None
    max_triggers: int | None = None
    credits_per_period: int | None = None
    allowed_modalities: list | None = None
    allowed_tiers: list | None = None
    features: dict | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    expected_updated_at: datetime


class EntitlementsOut(BaseModel):
    """Current tenant entitlements + subscription state (for frontend subscription tab)."""

    plan_id: uuid.UUID | None = None
    plan_code: str | None = None
    max_agents: int = 0
    max_llm_calls_per_day: int = 0
    message_limit: int = 0
    message_period: str = "permanent"
    max_triggers: int = 0
    credits_per_period: int = 0
    allowed_modalities: list = []
    allowed_tiers: list = []
    generation_modalities: list = []
    generation_tiers: list = []
    subscription_status: str | None = None  # active/trialing/canceled/expired/past_due
    period_end: datetime | None = None


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str | None = None
    status: str
    period_start: datetime
    period_end: datetime | None = None
    auto_renew: bool
    seats: int
    cancel_at_period_end: bool

    model_config = ConfigDict(from_attributes=True)


class AssignPlanIn(BaseModel):
    """Admin assigns a plan to a tenant (阶段0, no payment)."""

    tenant_id: uuid.UUID
    plan_id: uuid.UUID
    period_days: int | None = None  # None = permanent (e.g. free)


class UsageOut(BaseModel):
    """Current tenant usage today (for subscription tab display)."""

    period_date: str
    llm_calls_used: int
    llm_calls_limit: int
    messages_used: int
    messages_limit: int
    tokens_used: int
    credits_balance: int = 0


class PersonalUsageOut(BaseModel):
    """Only usage that can be attributed to the current membership.

    The tenant quota tables are company aggregates, so they are deliberately
    excluded.  Credit ledger rows with an exact ``user_id`` provide the only
    safe P0 attribution; the status remains partial until calls/messages/token
    counters gain membership-level accounting.
    """

    attribution_status: str = "partial"
    attribution_note: str
    consumed_credits: int = 0
    attributed_transactions: int = 0
    llm_calls_limit: int = 0
    message_limit: int = 0
    max_triggers: int = 0


class CreditBalanceOut(BaseModel):
    """Tenant credit balance."""

    balance: int
    reserved: int = 0


class CreditTransactionOut(BaseModel):
    """Single credit transaction row for the ledger."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    delta: int
    balance_after: int
    reason: str
    ref_type: str | None = None
    ref_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    action: str | None = None
    modality: str | None = None
    tier: str | None = None
    provider: str | None = None
    model: str | None = None
    consumer_label: str | None = None
    actor_label: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CreditPackOut(BaseModel):
    """Boost credit pack product."""

    id: uuid.UUID
    code: str
    name: str
    credits: int
    price_cents: int
    currency: str
    applicable_plan_ids: list | None = None
    is_active: bool
    sort_order: int

    model_config = ConfigDict(from_attributes=True)


class PaymentOrderOut(BaseModel):
    """Payment order with optional provider checkout session."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    type: str
    plan_id: uuid.UUID | None = None
    credits: int | None = None
    amount_cents: int
    currency: str
    provider: str
    provider_session_id: str | None = None
    provider_payment_id: str | None = None
    session_url: str | None = None
    status: str
    created_at: datetime
    paid_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionSummaryOut(BaseModel):
    """Server-side subscription usage and billing summary."""

    plan_id: uuid.UUID | None = None
    plan_code: str | None = None
    subscription_status: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    period_grant: int = 0
    topup_grants: int = 0
    consumed_credits: int = 0
    refunded_credits: int = 0
    total_granted: int = 0
    balance: int = 0
    reserved: int = 0
    available_balance: int = 0
    seats_used: int = 0
    seats_total: int = 0
    llm_calls_limit: int = 0
    message_limit: int = 0
    max_triggers: int = 0


class BillingProfileOut(BaseModel):
    """Tenant billing/invoice profile."""

    tenant_id: uuid.UUID
    company_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    email: str | None = None
    phone: str | None = None

    model_config = ConfigDict(from_attributes=True)


class BillingProfileIn(BaseModel):
    """Update tenant billing profile."""

    company_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    email: str | None = None
    phone: str | None = None


class BillingConfigOut(BaseModel):
    """Frontend billing display config (payment provider + FX rate)."""

    provider: str
    usd_cny_rate: float


class CheckoutSubscribeIn(BaseModel):
    """Create a subscription order."""

    plan_id: uuid.UUID
    period: str = "monthly"  # monthly / yearly
    seats: int = 1


class CheckoutTopupIn(BaseModel):
    """Create a credit top-up order."""

    credit_pack_id: uuid.UUID


class SeatUsageOut(BaseModel):
    """Tenant seat usage."""

    seats_total: int
    seats_used: int
    pending_invites: int
