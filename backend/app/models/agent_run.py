"""Product-owned immutable registry and delivery facts for durable Agent runs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentRun(Base):
    """Product-owned identity and delivery facts; execution state stays in checkpoints."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_runs"),
        CheckConstraint(
            "source_type IN ('chat', 'trigger', 'task', 'a2a', 'heartbeat')",
            name="ck_agent_runs_source_type",
        ),
        CheckConstraint(
            "run_kind IN ('foreground', 'background', 'delegated', 'orchestration')",
            name="ck_agent_runs_run_kind",
        ),
        CheckConstraint(
            "runtime_type IN ('legacy', 'langgraph')",
            name="ck_agent_runs_runtime_type",
        ),
        CheckConstraint(
            "delivery_status IN ('not_required', 'pending', 'delivered', 'failed')",
            name="ck_agent_runs_delivery_status",
        ),
        CheckConstraint(
            "runtime_type <> 'langgraph' OR model_id IS NOT NULL",
            name="ck_agent_runs_langgraph_model",
        ),
        CheckConstraint(
            "lane_held = false OR scheduling_lane_key IS NOT NULL",
            name="ck_agent_runs_lane_holder_key",
        ),
        CheckConstraint(
            "(scheduling_lane_key IS NULL AND scheduling_position_created_at IS NULL "
            "AND scheduling_position_id IS NULL) OR "
            "(scheduling_lane_key IS NOT NULL AND scheduling_position_created_at IS NOT NULL "
            "AND scheduling_position_id IS NOT NULL)",
            name="ck_agent_runs_lane_position",
        ),
        CheckConstraint(
            "(run_kind = 'orchestration' AND agent_id IS NULL "
            "AND system_role = 'group_planning' AND model_id IS NOT NULL) OR "
            "(run_kind <> 'orchestration' AND agent_id IS NOT NULL AND system_role IS NULL)",
            name="ck_agent_runs_orchestration_identity",
        ),
        CheckConstraint(
            "(run_kind = 'orchestration' AND model_turn_limit IS NULL) OR "
            "(run_kind <> 'orchestration' AND model_turn_limit > 0)",
            name="ck_agent_runs_model_turn_limit",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.id"],
            name="fk_agent_runs_tenant_session_chat_sessions",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_runs_tenant_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", name="fk_agent_runs_tenant_id_tenants", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", name="fk_agent_runs_agent_id_agents", ondelete="CASCADE"),
        nullable=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_sessions.id",
            name="fk_agent_runs_session_id_chat_sessions",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_execution_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_agent_runs_origin_user_id_users", ondelete="SET NULL"),
        nullable=True,
    )
    origin_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", name="fk_agent_runs_origin_agent_id_agents", ondelete="SET NULL"),
        nullable=True,
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", name="fk_agent_runs_parent_run_id_agent_runs", ondelete="SET NULL"),
        nullable=True,
    )
    root_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", name="fk_agent_runs_root_run_id_agent_runs", ondelete="SET NULL"),
        nullable=True,
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    run_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    system_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_models.id", name="fk_agent_runs_model_id_llm_models", ondelete="RESTRICT"),
        nullable=True,
    )
    model_turn_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_type: Mapped[str] = mapped_column(String(24), nullable=False)
    runtime_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    graph_name: Mapped[str] = mapped_column(String(100), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduling_lane_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scheduling_position_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scheduling_position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lane_held: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    lane_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_context_applied_checkpoint_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    delivery_status: Mapped[str] = mapped_column(String(24), nullable=False)
    delivery_target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


Index(
    "ix_agent_runs_tenant_thread_created_at",
    AgentRun.tenant_id,
    AgentRun.runtime_thread_id,
    AgentRun.created_at,
    AgentRun.id,
)
Index("ix_agent_runs_session_created_at", AgentRun.session_id, AgentRun.created_at.desc())
Index("ix_agent_runs_parent_run_id", AgentRun.parent_run_id)
Index("ix_agent_runs_root_run_id", AgentRun.root_run_id)
Index("ix_agent_runs_source", AgentRun.source_type, AgentRun.source_id)
Index(
    "uq_agent_runs_source_execution",
    AgentRun.source_type,
    AgentRun.source_execution_id,
    unique=True,
    postgresql_where=AgentRun.source_execution_id.is_not(None),
)
Index(
    "uq_agent_runs_active_lane",
    AgentRun.scheduling_lane_key,
    unique=True,
    postgresql_where=(AgentRun.scheduling_lane_key.is_not(None) & AgentRun.lane_held.is_(True)),
)
Index(
    "ix_agent_runs_lane_candidate_order",
    AgentRun.scheduling_lane_key,
    AgentRun.scheduling_position_created_at,
    AgentRun.scheduling_position_id,
    AgentRun.created_at,
    AgentRun.id,
    postgresql_where=AgentRun.scheduling_lane_key.is_not(None),
)


class LLMSystemCostReceipt(Base):
    """Durable platform-cost and replay receipt for a system-owned LLM call.

    Group Planning has no employee ``agent_id`` and therefore must not be
    forced through the customer/Agent Credits ledger.  This receipt records
    provider debt separately while retaining enough normalized response state
    to replay a finalized call after a worker interruption without calling the
    Provider again.
    """

    __tablename__ = "llm_system_cost_receipts"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_llm_system_cost_receipts"),
        CheckConstraint(
            "operation IN ('group_planning')",
            name="ck_llm_system_cost_receipts_operation",
        ),
        CheckConstraint(
            "status IN ('provider_inflight', 'reconciling', 'finalized', "
            "'reconciled', 'voided')",
            name="ck_llm_system_cost_receipts_status",
        ),
        CheckConstraint(
            "provider_outcome IN ('pending', 'acceptance_unknown', 'accepted', "
            "'not_accepted')",
            name="ck_llm_system_cost_receipts_provider_outcome",
        ),
        CheckConstraint(
            "usage_source IN ('pending', 'provider_reported', 'estimated', "
            "'operator_reported', 'unknown')",
            name="ck_llm_system_cost_receipts_usage_source",
        ),
        CheckConstraint(
            "cost_status IN ('pending', 'priced', 'unpriced', 'not_applicable')",
            name="ck_llm_system_cost_receipts_cost_status",
        ),
        CheckConstraint(
            "call_index > 0",
            name="ck_llm_system_cost_receipts_call_index_positive",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0 "
            "AND cache_read_tokens >= 0 AND cache_creation_tokens >= 0 "
            "AND estimated_tokens >= 0",
            name="ck_llm_system_cost_receipts_tokens_nonnegative",
        ),
        CheckConstraint(
            "system_cost_credits IS NULL OR system_cost_credits >= 0",
            name="ck_llm_system_cost_receipts_cost_nonnegative",
        ),
        CheckConstraint(
            "budget_reservation_credits > 0 AND request_input_token_upper_bound > 0 "
            "AND request_max_output_tokens > 0",
            name="ck_llm_system_cost_receipts_budget_positive",
        ),
        CheckConstraint(
            "status <> 'finalized' OR (provider_outcome = 'accepted' "
            "AND usage_source <> 'pending' AND cost_status <> 'pending' "
            "AND finalized_at IS NOT NULL)",
            name="ck_llm_system_cost_receipts_finalized_shape",
        ),
        CheckConstraint(
            "status <> 'reconciled' OR (provider_outcome = 'accepted' "
            "AND usage_source = 'operator_reported' AND cost_status = 'priced' "
            "AND system_cost_credits IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_llm_system_cost_receipts_reconciled_shape",
        ),
        CheckConstraint(
            "status <> 'voided' OR (provider_outcome = 'not_accepted' "
            "AND usage_source = 'unknown' AND cost_status = 'not_applicable' "
            "AND system_cost_credits = 0 AND finalized_at IS NOT NULL)",
            name="ck_llm_system_cost_receipts_voided_shape",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.id"],
            name="fk_llm_system_cost_receipts_tenant_run_agent_runs",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.id"],
            name="fk_llm_system_cost_receipts_tenant_session_chat_sessions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            name="fk_llm_system_cost_receipts_tenant_group_groups",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "call_index",
            name="uq_llm_system_cost_receipts_run_call",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_llm_system_cost_receipts_tenant_id_id",
        ),
        Index(
            "ix_llm_system_cost_receipts_tenant_created",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_llm_system_cost_receipts_group_created",
            "group_id",
            "created_at",
        ),
        Index(
            "ix_llm_system_cost_receipts_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(
        String(32), nullable=False, default="group_planning", server_default="group_planning"
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "llm_models.id",
            name="fk_llm_system_cost_receipts_model_id_llm_models",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    # Keep the immutable UUID snapshot even if an administrator later rotates
    # or deletes the credential row; no key material or label is stored here.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_service_tier: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="provider_inflight", server_default="provider_inflight"
    )
    provider_outcome: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    usage_source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    estimated_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    budget_reservation_credits: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    request_input_token_upper_bound: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    request_max_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    system_cost_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LLMSystemCostResolution(Base):
    """Append-only operator or daemon receipt for one cost-state transition."""

    __tablename__ = "llm_system_cost_resolutions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_llm_system_cost_resolutions"),
        CheckConstraint(
            "action IN ('mark_stale_unknown', 'confirm_not_accepted', "
            "'settle_accepted')",
            name="ck_llm_system_cost_resolutions_action",
        ),
        CheckConstraint(
            "source IN ('operator', 'daemon')",
            name="ck_llm_system_cost_resolutions_source",
        ),
        CheckConstraint(
            "previous_status IN ('provider_inflight', 'reconciling') AND "
            "resulting_status IN ('reconciling', 'reconciled', 'voided')",
            name="ck_llm_system_cost_resolutions_statuses",
        ),
        CheckConstraint(
            "previous_provider_outcome IN ('pending', 'acceptance_unknown') AND "
            "resulting_provider_outcome IN ('acceptance_unknown', 'accepted', "
            "'not_accepted')",
            name="ck_llm_system_cost_resolutions_outcomes",
        ),
        CheckConstraint(
            "reported_system_cost_credits IS NULL OR "
            "reported_system_cost_credits >= 0",
            name="ck_llm_system_cost_resolutions_cost_nonnegative",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "receipt_id"],
            ["llm_system_cost_receipts.tenant_id", "llm_system_cost_receipts.id"],
            name="fk_llm_system_cost_resolutions_tenant_receipt",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "receipt_id",
            "idempotency_key_hash",
            name="uq_llm_system_cost_resolutions_idempotency",
        ),
        Index(
            "ix_llm_system_cost_resolutions_tenant_created",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_llm_system_cost_resolutions_receipt_created",
            "receipt_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="fk_llm_system_cost_resolutions_actor_user_id_users",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_status: Mapped[str] = mapped_column(String(24), nullable=False)
    resulting_status: Mapped[str] = mapped_column(String(24), nullable=False)
    previous_provider_outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    resulting_provider_outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    reported_system_cost_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
