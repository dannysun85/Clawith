"""Audit log, approval request, chat message, and enterprise info models."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """Audit trail for all operations."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default={})
    ip_address: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ApprovalRequest(Base):
    """Approval request for L3 autonomy operations."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "execution_status IS NULL OR execution_status IN "
            "('legacy', 'invalid', 'pending', 'executing', 'succeeded', "
            "'failed', 'ambiguous', 'not_required')",
            name="ck_approval_execution_status",
        ),
        CheckConstraint(
            "execution_attempts >= 0 AND execution_attempts <= 1",
            name="ck_approval_execution_single_attempt",
        ),
        CheckConstraint(
            "(status = 'pending' AND (execution_status IS NULL OR execution_status = 'invalid') "
            "AND execution_attempts = 0 AND execution_claim_token IS NULL "
            "AND execution_claimed_at IS NULL AND execution_finished_at IS NULL) OR "
            "(status = 'rejected' AND execution_status IN ('legacy', 'not_required') "
            "AND execution_attempts = 0 AND execution_claim_token IS NULL "
            "AND execution_claimed_at IS NULL AND execution_finished_at IS NULL) OR "
            "(status = 'approved' AND ((execution_status = 'legacy' AND execution_attempts = 0 "
            "AND execution_claim_token IS NULL AND execution_claimed_at IS NULL "
            "AND execution_finished_at IS NULL) OR (execution_status = 'pending' "
            "AND execution_attempts = 0 AND execution_claim_token IS NULL "
            "AND execution_claimed_at IS NULL AND execution_finished_at IS NULL) OR "
            "(execution_status = 'executing' AND execution_attempts = 1 "
            "AND execution_claim_token IS NOT NULL AND execution_claimed_at IS NOT NULL "
            "AND execution_finished_at IS NULL) OR (execution_status IN "
            "('succeeded', 'failed', 'ambiguous') AND execution_attempts = 1 "
            "AND execution_claim_token IS NOT NULL AND execution_claimed_at IS NOT NULL "
            "AND execution_finished_at IS NOT NULL)))",
            name="ck_approval_execution_state_consistency",
        ),
        Index(
            "uq_active_approval_request_fingerprint",
            "agent_id",
            "request_fingerprint",
            unique=True,
            postgresql_where=text(
                "request_fingerprint IS NOT NULL AND (status = 'pending' OR "
                "(status = 'approved' AND execution_status IN ('pending', 'executing')))"
            ),
            sqlite_where=text(
                "request_fingerprint IS NOT NULL AND (status = 'pending' OR "
                "(status = 'approved' AND execution_status IN ('pending', 'executing')))"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default={})
    status: Mapped[str] = mapped_column(
        Enum("pending", "approved", "rejected", name="approval_status_enum"),
        default="pending",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    # Approval resolution and action execution are deliberately separate
    # durable states.  An approved row is committed before a worker claims the
    # external side effect, so a failed commit can never make an action
    # replayable.  A process crash while ``executing`` is reconciled to
    # ``ambiguous`` and is never retried automatically.
    execution_status: Mapped[str | None] = mapped_column(String(32), index=True)
    execution_claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    execution_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    execution_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    execution_result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_error_code: Mapped[str | None] = mapped_column(String(100))


class ChatMessage(Base):
    """Message on the unified chat substrate."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(
        Enum("user", "assistant", "system", "tool_call", name="chat_role_enum"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(200), default="web", nullable=False, index=True)
    # Participant identity (unified User/Agent identity)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("participants.id"), nullable=True)
    # Model thinking process
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    mentions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class EnterpriseInfo(Base):
    """Centralized enterprise information with versioning for sync."""

    __tablename__ = "enterprise_info"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    info_type: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)  # org_structure, company_profile, etc.
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    visible_roles: Mapped[list] = mapped_column(JSON, default=[])  # Which agent roles can see this
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
