"""Durable ownership ledger for AgentBay provider sessions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


AGENTBAY_PROVIDER_COLLISION_STATUS = "provider_identity_collision"
AGENTBAY_UNRESOLVED_STATUSES = frozenset(
    {"active", "cleanup_required", AGENTBAY_PROVIDER_COLLISION_STATUS}
)
AGENTBAY_PROVIDER_UNIQUE_STATUSES = frozenset(
    {"active", "cleanup_required", AGENTBAY_PROVIDER_COLLISION_STATUS}
)


class AgentBaySessionLedger(Base):
    """Bind one provider sandbox to one tenant/user/chat-session lane."""

    __tablename__ = "agentbay_session_ledger"
    __table_args__ = (
        Index(
            "uq_agentbay_active_user_chat_image",
            "agent_id",
            "user_id",
            "chat_session_id",
            "image_type",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND agent_id IS NOT NULL "
                "AND user_id IS NOT NULL AND chat_session_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'active' AND agent_id IS NOT NULL "
                "AND user_id IS NOT NULL AND chat_session_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_agentbay_live_provider_session_id",
            "provider_session_id",
            unique=True,
            postgresql_where=text(
                "provider_session_id IS NOT NULL "
                "AND status IN "
                "('active', 'cleanup_required', 'provider_identity_collision')"
            ),
            sqlite_where=text(
                "provider_session_id IS NOT NULL "
                "AND status IN "
                "('active', 'cleanup_required', 'provider_identity_collision')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    chat_session_id: Mapped[str | None] = mapped_column(String(160), index=True)
    provider_session_id: Mapped[str | None] = mapped_column(String(200), index=True)
    image_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    platform: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    close_reason: Mapped[str | None] = mapped_column(String(100), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
