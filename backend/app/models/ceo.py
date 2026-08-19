"""CEO orchestrator (P1 observer) per-tenant settings model.

One row per tenant, created only on explicit opt-in. The CEO Agent itself is a
regular ``Agent`` row with ``is_system=True`` and the ``ceo`` role template;
this table is the governance anchor (enablement, cadence switches, budget caps,
meeting group binding). Disable never deletes the Agent or any history.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CeoOrchestratorSettings(Base):
    """Per-tenant CEO orchestrator configuration. Always at most one row per tenant."""

    __tablename__ = "ceo_orchestrator_settings"
    __table_args__ = (
        UniqueConstraint("ceo_agent_id", name="uq_ceo_orchestrator_settings_ceo_agent"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # The per-tenant CEO Agent (is_system=True, template role_key="ceo").
    ceo_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Tenant opt-in master switch. All automation gates on this.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cadence switches (both default off; require the master switch too).
    briefing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    morning_meeting_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Lazily created on the first meeting; never auto-created at enable time.
    meeting_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="SET NULL"),
        nullable=True,
    )

    # CEO-only automation Credits budget caps (0 = unlimited, not recommended).
    daily_credit_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    monthly_credit_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    # Employee Agents selected at enable time (meeting members /定向询问对象).
    meeting_member_agent_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
