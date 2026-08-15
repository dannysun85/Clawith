"""Tenant (Company) model — multi-tenancy isolation boundary."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Tenant(Base):
    """A company/organization that uses the platform."""

    __tablename__ = "tenants"
    __table_args__ = (
        Index(
            "uq_tenants_creator_idempotency_key",
            "created_by_identity_id",
            "creation_idempotency_key_hash",
            unique=True,
            postgresql_where=text("creation_idempotency_key_hash IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    im_provider: Mapped[str] = mapped_column(
        Enum("feishu", "dingtalk", "wecom", "microsoft_teams", "web_only", name="im_provider_enum"),
        default="web_only",
        nullable=False,
    )
    im_config: Mapped[dict | None] = mapped_column(JSON, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Self-service creation requests are replay-safe per global Identity. The
    # raw Idempotency-Key is never persisted; only its SHA-256 digest is kept.
    created_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    creation_idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Exactly one tenant membership owns the company. Historical tenants that
    # cannot be resolved deterministically are quarantined in an explicit
    # resolution queue instead of silently promoting an arbitrary admin.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    owner_resolution_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    initialization_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    initialized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # First-company setup is intentionally limited to product policy. Provider,
    # model, Tool and Skill choices belong to the operator/admin surfaces.
    company_size: Mapped[str] = mapped_column(
        String(32), default="unspecified", server_default="unspecified", nullable=False
    )
    allow_member_private_agents: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    default_approval_policy: Mapped[str] = mapped_column(
        String(32), default="high_risk", server_default="high_risk", nullable=False
    )

    # Company deletion is recoverable until ``deletion_scheduled_for``. The
    # public API never performs an immediate destructive cascade.
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Default quotas for new users
    default_message_limit: Mapped[int] = mapped_column(Integer, default=50)
    default_message_period: Mapped[str] = mapped_column(String(20), default="permanent")
    default_max_agents: Mapped[int] = mapped_column(Integer, default=2)
    default_agent_ttl_hours: Mapped[int] = mapped_column(Integer, default=0)
    default_max_llm_calls_per_day: Mapped[int] = mapped_column(Integer, default=1000)

    # Heartbeat frequency floor (minutes) — agents cannot heartbeat faster than this
    min_heartbeat_interval_minutes: Mapped[int] = mapped_column(Integer, default=240)

    # Default timezone for all agents in this company (IANA format, e.g. "Asia/Shanghai")
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    # Company country/region code used to derive default timezone and business calendar.
    country_region: Mapped[str] = mapped_column(String(10), default="001")

    # SSO configuration
    sso_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sso_domain: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)

    # Trigger limits — defaults for new agents & floor values
    default_max_triggers: Mapped[int] = mapped_column(Integer, default=20)
    min_poll_interval_floor: Mapped[int] = mapped_column(Integer, default=5)
    max_webhook_rate_ceiling: Mapped[int] = mapped_column(Integer, default=5)

    # A2A async communication (notify / task_delegate)
    # When False, all agent-to-agent messages use synchronous consult mode
    a2a_async_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Company default LLM model. Auto-set to the first enabled model the admin
    # adds; used as the initial primary_model_id for new agents created in this
    # tenant. SET NULL on model delete so the tenant just has no default until
    # an admin picks a new one.
    default_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_models.id", ondelete="SET NULL"), nullable=True,
    )

    @property
    def logo_url(self) -> str | None:
        """Tenant logo URL stored in flexible tenant config."""
        if isinstance(self.im_config, dict):
            value = self.im_config.get("logo_url")
            return value if isinstance(value, str) and value else None
        return None
