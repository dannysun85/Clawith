"""LLM model pool configuration."""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LLMModel(Base):
    """LLM model in the platform model pool."""

    __tablename__ = "llm_models"
    __table_args__ = (
        CheckConstraint(
            "context_window_tokens IS NULL OR context_window_tokens > 0",
            name="ck_llm_models_context_window_tokens_positive",
        ),
        CheckConstraint(
            "context_window_tokens_override IS NULL OR context_window_tokens_override > 0",
            name="ck_llm_models_context_window_tokens_override_positive",
        ),
        CheckConstraint(
            "max_input_tokens IS NULL OR max_input_tokens > 0",
            name="ck_llm_models_max_input_tokens_positive",
        ),
        CheckConstraint(
            "max_input_tokens_override IS NULL OR max_input_tokens_override > 0",
            name="ck_llm_models_max_input_tokens_override_positive",
        ),
        CheckConstraint(
            "capability_source IS NULL OR capability_source IN "
            "('manual', 'provider_api', 'builtin_registry', 'runtime_config')",
            name="ck_llm_models_capability_source",
        ),
        CheckConstraint(
            "tool_calling_capability_source IS NULL OR "
            "tool_calling_capability_source IN ('probe', 'builtin_registry')",
            name="ck_llm_models_tool_calling_capability_source",
        ),
        Index(
            "ix_llm_models_active_tenant_created_at",
            "tenant_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # anthropic, openai, deepseek, etc.
    model: Mapped[str] = mapped_column(String(100), nullable=False)  # claude-opus-4-6, gpt-4o, etc.
    api_key_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500))
    label: Mapped[str] = mapped_column(String(200), nullable=False)  # Display name
    max_tokens_per_day: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_timeout: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Request timeout in seconds, default 120
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Per-model output token limit override
    # Runtime capability data is control-plane metadata. It selects a platform
    # model for planning/tool use without reintroducing per-tenant model-object
    # authorization.
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window_tokens_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capability_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supports_tool_calling: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tool_calling_capability_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tool_calling_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tool_calling_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Subscription model routing (模块四 7.2)
    modality: Mapped[str] = mapped_column(String(20), nullable=False, default="text")  # text/vision/audio/music/video/multimodal
    modalities: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ["text","vision"]
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")  # premium/standard/basic
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {stream, tool_call, ...}
    verification_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LLMCredential(Base):
    """API-key account pool entry (账号池, provider-scoped).

    One account serves multiple models/modalities of a provider (e.g. a MiniMax
    code-plan account can call text/voice/image/video). Load-balanced across the
    pool per provider+modality. tenant_id=null = platform pool (shared by all
    tenants); set = tenant-owned key (future).
    """

    __tablename__ = "llm_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # minimax/openai/...
    label: Mapped[str] = mapped_column(String(200), nullable=False)  # "MiniMax code plan A"
    api_key_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500))  # override provider default
    # Subscription tier belongs to the provider account, not Astra's
    # Lite/Pro/Ultra product tier. It is currently required for Agent Plan so
    # a Small/Medium key is never routed to a Seedance 2.0 request it cannot
    # serve.
    plan_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ["text","voice","image","video"]
    # Provider quota circuits live outside credential authentication health.
    # MiniMax's current plan allowance is shared under ``plan``; named model
    # rows from provider evidence can additionally open exact media circuits.
    modality_status: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)  # per-account daily cap
    used_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unverified")
    # unverified / healthy / degraded / quota_exceeded / disabled
    # A healthy status is only an operational circuit state.  These two
    # fields preserve the last explicit, read-only provider authentication
    # probe so control-plane readiness never has to infer verification from a
    # configured key or a successful call that happened under older config.
    last_verification_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_receipt: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # weighted round-robin
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # higher = used first
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Client-side rate limiting (proactive protection against 429s / provider bans).
    # NULL means "no limit enforced" (provider default or unlimited).
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # max requests per minute
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # max tokens per minute
    # Legacy manual threshold retained for schema compatibility. Current
    # MiniMax Token Plan usage is price-weighted across modalities and cannot
    # be reproduced safely from a local raw-token counter.
    window_5h_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )  # null = platform pool / set = tenant-owned key
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaProviderDailyAllowanceClaim(Base):
    """Transactional claim against a provider's modality-specific daily allowance.

    This is intentionally separate from ``LLMCredential.used_today``: that
    legacy counter spans every modality, whereas MiniMax Plan currently grants
    a small video-only daily allowance.  Rows are retained as an audit trail;
    only ``claimed`` and ``accepted`` rows consume the day's allowance.
    """

    __tablename__ = "media_provider_daily_allowance_claims"
    __table_args__ = (
        CheckConstraint(
            "status IN ('claimed', 'accepted', 'released')",
            name="ck_media_provider_daily_allowance_claim_status",
        ),
        Index(
            "ix_media_provider_daily_allowance_active",
            "credential_id",
            "modality",
            "allowance_date",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_credentials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    modality: Mapped[str] = mapped_column(String(20), nullable=False)
    allowance_date: Mapped[date] = mapped_column(Date, nullable=False)
    quota_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="claimed", server_default=text("'claimed'")
    )
    task_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
