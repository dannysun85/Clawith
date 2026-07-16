"""Gateway messages for OpenClaw agent communication."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GatewayMessage(Base):
    """Message queued for delivery to an OpenClaw agent.

    Lifecycle: pending → delivered → completed (or expired).
    """

    __tablename__ = "gateway_messages"
    __table_args__ = (
        Index(
            "ix_gateway_messages_delivery_claim",
            "agent_id",
            "status",
            "delivery_lease_expires_at",
            "created_at",
            postgresql_where=text("status IN ('pending', 'delivered')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Target OpenClaw agent
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    # Sender (one of these may be None)
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # Original directed relationship that authorized this delivery. Replies
    # retain the same source so revocation can be checked after queueing.
    authorization_source_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id"),
    )
    # Chat session tracking for routing responses back
    conversation_id: Mapped[str | None] = mapped_column(String(100))
    # Message content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Status tracking
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending | delivered | completed
    result: Mapped[str | None] = mapped_column(Text)
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    delivery_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
