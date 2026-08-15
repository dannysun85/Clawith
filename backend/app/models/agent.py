"""Digital Employee (Agent) models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Default context window size — used as the fallback when
# agent.context_window_size is None or 0 across all channels.
# Centralizing this constant prevents inconsistent fallback values
# (see: https://github.com/dataelement/Clawith/issues/238).
DEFAULT_CONTEXT_WINDOW_SIZE = 100


class Agent(Base):
    """Digital employee (Agent) instance.

    agent_type: 'native' (platform-hosted) or 'openclaw' (remote OpenClaw bot).
    """

    __tablename__ = "agents"
    __table_args__ = (
        Index(
            "ix_agents_active_tenant_created_at",
            "tenant_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role_description: Mapped[str] = mapped_column(String(500), default="")
    bio: Mapped[str | None] = mapped_column(Text)
    welcome_message: Mapped[str | None] = mapped_column(Text, default=None)

    # Ownership
    creator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))

    # Agent type: 'native' (platform-hosted LLM) or 'openclaw' (remote OpenClaw bot)
    agent_type: Mapped[str] = mapped_column(String(20), default="native", nullable=False)
    # API key hash for OpenClaw gateway authentication
    api_key_hash: Mapped[str | None] = mapped_column(String(128))
    # Last time OpenClaw polled the gateway (online status indicator)
    openclaw_last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Durable deletion fence. Once set, start/recover and all execution lanes
    # must remain fail-closed until provider cleanup and final deletion finish.
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Runtime
    status: Mapped[str] = mapped_column(
        Enum("creating", "running", "idle", "stopped", "error", name="agent_status_enum", create_constraint=False),
        default="creating",
        nullable=False,
    )
    container_id: Mapped[str | None] = mapped_column(String(100))
    container_port: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # LLM config
    primary_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))
    fallback_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))

    # SaaS tier selection (Lite/Pro/Ultra) — preferred over legacy primary_model_id
    preferred_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preferred_modality: Mapped[str | None] = mapped_column(String(20), nullable=True, default="text")

    # Autonomy policy (L1/L2/L3)
    autonomy_policy: Mapped[dict] = mapped_column(
        JSON,
        default={
            "read_files": "L1",
            "write_workspace_files": "L2",
            "send_feishu_message": "L2",
            "send_external_message": "L3",
            "modify_soul": "L3",
            "access_business_system_read": "L2",
            "access_business_system_write": "L3",
            "delete_files": "L3",
            "create_calendar_event": "L2",
            "financial_operations": "L3",
        },
    )

    # Token usage control
    max_tokens_per_day: Mapped[int | None] = mapped_column(Integer)
    max_tokens_per_month: Mapped[int | None] = mapped_column(Integer)
    tokens_used_today: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_month: Mapped[int] = mapped_column(Integer, default=0)
    last_daily_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_monthly_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_used_total: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens_today: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens_month: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens_total: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens_today: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens_month: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens_total: Mapped[int] = mapped_column(Integer, default=0)
    context_window_size: Mapped[int] = mapped_column(Integer, default=100)
    # Historical field name: this is the maximum number of model-decision turns
    # allowed for one Agent Run, not the number of tools executed.
    max_tool_rounds: Mapped[int] = mapped_column(Integer, default=50)

    # Trigger limits (per-agent, configurable from Settings UI)
    max_triggers: Mapped[int] = mapped_column(Integer, default=20)
    min_poll_interval_min: Mapped[int] = mapped_column(Integer, default=5)
    webhook_rate_limit: Mapped[int] = mapped_column(Integer, default=5)

    # Expiry control
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_expired: Mapped[bool] = mapped_column(Boolean, default=False)

    # System agent flag — system agents (e.g. OKR Agent) cannot be deleted by users
    # and their system triggers are protected from user deletion.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Access model:
    # - company: all platform users and non-private tenant agents can access; Plaza is enabled.
    # - private: only the creator can use/manage; hidden from Plaza.
    # - custom: creator/company governors plus explicitly permitted users can
    #   use it; user rows grant use or management, and Plaza is disabled.
    access_mode: Mapped[str] = mapped_column(String(20), default="company", nullable=False)
    # Legacy/default UI field. Runtime access is determined by access_mode and
    # AgentPermission; this field is retained only for response compatibility.
    company_access_level: Mapped[str] = mapped_column(String(20), default="use", nullable=False)

    # Daily LLM call limit
    llm_calls_today: Mapped[int] = mapped_column(Integer, default=0)
    max_llm_calls_per_day: Mapped[int] = mapped_column(Integer, default=1000)
    llm_calls_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Template
    template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_templates.id"))
    template_revision_applied: Mapped[int | None] = mapped_column(Integer)
    template_sync_status: Mapped[str] = mapped_column(
        String(20), default="current", server_default="current", nullable=False
    )
    template_sync_details: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'::json"),
    )
    template_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Heartbeat (proactive agent awareness)
    heartbeat_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    heartbeat_interval_minutes: Mapped[int] = mapped_column(Integer, default=240)
    heartbeat_active_hours: Mapped[str] = mapped_column(String(20), default="09:00-18:00")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Timezone (IANA format, e.g. "Asia/Shanghai"). None = inherit from tenant.
    timezone: Mapped[str | None] = mapped_column(String(50), default=None, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    creator: Mapped["User"] = relationship("User", back_populates="created_agents", foreign_keys=[creator_id])

    @property
    def has_api_key(self) -> bool:
        """Whether this agent has an API key configured."""
        return bool(self.api_key_hash)

    permissions: Mapped[list["AgentPermission"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    channel_config: Mapped["ChannelConfig | None"] = relationship(back_populates="agent", uselist=False)
    primary_model: Mapped["LLMModel | None"] = relationship(foreign_keys=[primary_model_id])
    fallback_model: Mapped["LLMModel | None"] = relationship(foreign_keys=[fallback_model_id])


class AgentPermission(Base):
    """Access permission for a digital employee.

    Database checks and partial unique indexes are migration-owned because the
    historical smoke path builds current metadata at ``initial_schema`` before
    replaying legacy duplicate rows that the head migration must normalize.
    """

    __tablename__ = "agent_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(
        Enum("company", "department", "user", name="permission_scope_enum"),
        nullable=False,
    )
    # scope_id: null for company, user_id for user scope
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # access_level: 'use' = task/chat/tool/skill/workspace only, 'manage' = full access
    access_level: Mapped[str] = mapped_column(String(20), default="use", nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="permissions")


class AgentTemplate(Base):
    """Digital employee template for quick creation."""

    __tablename__ = "agent_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="🤖")
    category: Mapped[str] = mapped_column(String(50), default="general")
    soul_template: Mapped[str] = mapped_column(Text, default="")
    default_skills: Mapped[list] = mapped_column(JSON, default=list)
    # Explicit executable capabilities granted to agents created from this
    # role. Skills remain instructions; they never imply tool permission.
    default_tools: Mapped[list] = mapped_column(JSON, default=list)
    # Smithery server IDs (e.g. "shibui/finance") to auto-import + bind when
    # an agent is created from this template. The new-agent handler in
    # api.agents.create_agent calls import_mcp_from_smithery for each, using
    # the system-level Smithery key, then assigns the resulting Tool(s) via
    # AgentTool. Idempotent: existing Tool with same mcp_server_url is reused.
    default_mcp_servers: Mapped[list] = mapped_column(JSON, default=list)
    default_autonomy_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # Talent Market card: 2-4 short capability bullets shown under the role
    capability_bullets: Mapped[list] = mapped_column(JSON, default=list)
    # Versioned role identity and operating boundaries.  These are persisted
    # rather than re-read from repository files so every API/runtime process
    # observes the same reviewed contract.
    role_key: Mapped[str | None] = mapped_column(String(100), index=True)
    role_revision: Mapped[int] = mapped_column(
        default=1,
        server_default="1",
        nullable=False,
    )
    responsibilities: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
    )
    non_responsibilities: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
    )
    limitations: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
    )
    workflows: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
    )
    deliverables: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
    )
    evaluation_criteria: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
    )
    source_provenance: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'::json"),
    )
    # Only ``enabled`` templates may appear in the Talent Market or be used to
    # create an Agent.  Candidate/conditional records remain inspectable by an
    # admin without becoming executable employees by accident.
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), default="enabled", server_default="enabled", nullable=False, index=True
    )
    activation_gate: Mapped[str | None] = mapped_column(Text)
    workforce_source_role_id: Mapped[str | None] = mapped_column(String(100), index=True)
    workforce_decision: Mapped[str | None] = mapped_column(String(32), index=True)
    workforce_pack: Mapped[str | None] = mapped_column(String(100), index=True)
    is_builtin: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentTemplateEvaluation(Base):
    """Immutable A/B evidence used to gate a role-template activation."""

    __tablename__ = "agent_template_evaluations"
    __table_args__ = (
        Index(
            "ix_agent_template_evaluations_template_revision_created",
            "template_id",
            "role_revision",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(50), nullable=False)
    fixture_set_version: Mapped[str] = mapped_column(String(50), nullable=False)
    role_family: Mapped[str] = mapped_column(String(40), nullable=False)
    baseline_metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    candidate_metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    fixture_results: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
        nullable=False,
    )
    safety_pass: Mapped[bool] = mapped_column(Boolean, nullable=False)
    capability_pass: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gate_status: Mapped[str] = mapped_column(String(20), nullable=False)
    gate_reasons: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentUserOnboarding(Base):
    """Tracks the per-(agent, user) onboarding ritual.

    Row presence means the greeting has fired, so the frontend should not
    auto-trigger another empty-session greeting. The ``phase`` column lets the
    backend continue with a second, real user reply that calibrates the agent
    and writes durable working notes before marking onboarding complete.
    """

    __tablename__ = "agent_user_onboardings"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    onboarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)


# Import for relationship resolution
from app.models.task import Task  # noqa: E402, F401
from app.models.channel_config import ChannelConfig  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
from app.models.llm import LLMModel  # noqa: E402, F401
