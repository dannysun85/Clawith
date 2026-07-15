"""Durable state for asynchronous media-generation provider tasks."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MediaGenerationTask(Base):
    """A provider task that must survive Agent turns and process restarts."""

    __tablename__ = "media_generation_tasks"
    __table_args__ = (
        UniqueConstraint("provider", "provider_task_id", name="uq_media_generation_provider_task"),
        UniqueConstraint("reservation_id", name="uq_media_generation_reservation"),
        UniqueConstraint("completion_message_id", name="uq_media_generation_completion_message"),
        CheckConstraint(
            "completion_delivery_status IN ('pending', 'inline', 'persisted', 'not_applicable')",
            name="ck_media_generation_completion_delivery_status",
        ),
        Index("ix_media_generation_due", "status", "next_poll_at"),
        Index(
            "ix_media_generation_completion_outbox",
            "realtime_next_attempt_at",
            "completed_at",
            postgresql_where=text(
                "status = 'succeeded' AND completion_message_id IS NOT NULL "
                "AND realtime_published_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_credentials.id", ondelete="SET NULL"), nullable=True
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_reservations.id", ondelete="SET NULL"), nullable=True
    )
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_sessions.id",
            name="fk_media_generation_origin_session",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    completion_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_messages.id",
            name="fk_media_generation_completion_message",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    modality: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="submitting", index=True)

    metadata_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    output_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    request_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_delivery_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default=text("'pending'")
    )
    realtime_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    realtime_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    realtime_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    realtime_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
