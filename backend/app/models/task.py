"""Task models for digital employees."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    """Task assigned to or managed by a digital employee."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "origin_type IN ('workbench', 'agent_page', 'agent_chat', 'group', "
            "'trigger', 'api', 'legacy_agent_task')",
            name="ck_tasks_origin_type",
        ),
        CheckConstraint(
            "executor_kind IN ('personal_assistant', 'agent_employee', "
            "'temporary_expert', 'group')",
            name="ck_tasks_executor_kind",
        ),
        CheckConstraint(
            "(client_request_id IS NULL AND request_fingerprint IS NULL) OR "
            "(client_request_id IS NOT NULL AND request_fingerprint IS NOT NULL)",
            name="ck_tasks_client_fingerprint",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
        Index(
            "uq_tasks_workbench_client_identity",
            "tenant_id",
            "created_by",
            "client_request_id",
            unique=True,
            postgresql_where=text("client_request_id IS NOT NULL"),
        ),
        Index("ix_tasks_tenant_creator_updated", "tenant_id", "created_by", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", name="fk_tasks_tenant_id_tenants", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    origin_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy_agent_task", server_default=text("'legacy_agent_task'")
    )
    executor_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="agent_employee", server_default=text("'agent_employee'")
    )
    executor_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    work_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="general", server_default=text("'general'")
    )
    work_statement: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    confirmation_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", name="fk_tasks_group_id_groups", ondelete="SET NULL"),
        nullable=True,
    )
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(
        Enum("todo", "supervision", name="task_type_enum", create_constraint=False),
        default="todo",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "doing", "done", "failed", name="task_status_enum"),
        default="pending",
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        Enum("low", "medium", "high", "urgent", name="task_priority_enum"),
        default="medium",
        nullable=False,
    )
    assignee: Mapped[str] = mapped_column(String(50), default="self")  # "self" or user_id
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Supervision specific fields
    supervision_target_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    supervision_target_name: Mapped[str | None] = mapped_column(String(100))
    supervision_channel: Mapped[str | None] = mapped_column(String(50))
    remind_schedule: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    agent: Mapped["Agent"] = relationship(back_populates="tasks")
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    logs: Mapped[list["TaskLog"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskLog(Base):
    """Progress log entry for a task."""

    __tablename__ = "task_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="logs")


class TaskResultReviewReceipt(Base):
    """Immutable owner decision for one exact Work Runtime attempt."""

    __tablename__ = "task_result_review_receipts"
    __table_args__ = (
        CheckConstraint(
            "action IN ('approve', 'request_changes')",
            name="ck_task_result_review_receipts_action",
        ),
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "run_id",
            name="uq_task_result_review_receipts_attempt",
        ),
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "client_request_id",
            name="uq_task_result_review_receipts_request",
        ),
        Index(
            "ix_task_result_review_receipts_task_created",
            "task_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    client_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Resolve forward refs
from app.models.agent import Agent  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
