"""Subscription, billing, and credit models.

Implements the subscription core (模块 0 地基):
- Plan: global subscription plan definitions (套餐)
- Subscription: tenant subscription state (订阅)
- CreditBalance / CreditTransaction: credit accounting (积分余额/流水)
- BillingProfile: tenant invoice profile (发票资料)
- PaymentOrder: payment order (支付订单)
- TenantUsage: tenant-level daily usage counter (tenant 共享配额, 3.7)

See SUBSCRIPTION_IMPLEMENTATION_DESIGN.md §3 for design.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Plan(Base):
    """Subscription plan definition (global, not tenant-scoped)."""

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # free / pro / enterprise
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # rank for ordering
    period: Mapped[str] = mapped_column(String(20), nullable=False, default="monthly")  # monthly / yearly / permanent
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")

    # Quota fields — read by entitlements to drive quota_guard (3.6/3.7)
    max_agents: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_llm_calls_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    message_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    message_period: Mapped[str] = mapped_column(String(20), nullable=False, default="permanent")
    max_triggers: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    credits_per_period: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Plan-scoped model access (模块四 7.4)
    allowed_modalities: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ["text","vision"]
    allowed_tiers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ["standard"]
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # extensible feature flags

    stripe_price_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Subscription(Base):
    """Tenant subscription. One active/trialing row per tenant (partial unique index)."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # active / trialing / canceled / expired / past_due  (3.6 state machine)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # entitlement cutoff
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    seats: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stripe_sub_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditBalance(Base):
    """Tenant credit balance (one row per tenant)."""

    __tablename__ = "credit_balances"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # pre-hold for in-flight ops
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditTransaction(Base):
    """Credit transaction log (audit)."""

    __tablename__ = "credit_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)  # +credit / -consume
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)  # snapshot for audit
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    # subscribe / topup / consume / refund / refund_clawback / adjust
    ref_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # agent / message / order
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Audit fields for usage breakdown
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    modality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class CreditReservation(Base):
    """Held credits for asynchronous provider tasks."""

    __tablename__ = "credit_reservations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    modality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved")
    # reserved / provider_inflight / settlement_ready / finalized / released / expired
    ref_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BillingProfile(Base):
    """Tenant billing/invoice profile (one row per tenant)."""

    __tablename__ = "billing_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaymentOrder(Base):
    """Payment order (one per checkout attempt)."""

    __tablename__ = "payment_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # subscribe / topup
    plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=True)
    credits: Mapped[int | None] = mapped_column(Integer, nullable=True)  # topup amount
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    # stripe / alipay / wechat / manual
    provider_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending / paid / failed / canceled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BillingWebhookEvent(Base):
    """Idempotency log for signed billing provider webhook events."""

    __tablename__ = "billing_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantUsage(Base):
    """Tenant daily usage counter (tenant-level shared quota, 3.7).

    Primary key (tenant_id, period_date) — new row per tenant-local day = natural reset.
    """

    __tablename__ = "tenant_usage"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True
    )
    period_date: Mapped[date] = mapped_column(Date, primary_key=True)  # tenant-local date
    llm_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # weighted by tier
    llm_calls_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # derived from plan
    messages_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # for stats/cost
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BillingRule(Base):
    """Fixed credit cost rule per action/modality/tier."""

    __tablename__ = "billing_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    modality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="call")
    credit_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditPack(Base):
    """Boost credit pack product."""

    __tablename__ = "credit_packs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    applicable_plan_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelRoute(Base):
    """SaaS tier + modality -> real LLMModel routing."""

    __tablename__ = "model_routes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saas_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    modality: Mapped[str] = mapped_column(String(20), nullable=False)
    llm_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_models.id"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_route_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_routes.id"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
