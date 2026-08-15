"""Identity-level MFA challenges and one-time recovery codes."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IdentityMfaRecoveryCode(Base):
    """One HMAC-digested recovery code which can be consumed once."""

    __tablename__ = "identity_mfa_recovery_codes"
    __table_args__ = (
        Index(
            "uq_identity_mfa_recovery_codes_active_hash",
            "identity_id",
            "code_hash",
            unique=True,
            postgresql_where=text("used_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdentityMfaChallenge(Base):
    """Short-lived, database-fenced MFA login or setup ceremony."""

    __tablename__ = "identity_mfa_challenges"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('login', 'bootstrap', 'setup')",
            name="ck_identity_mfa_challenges_purpose",
        ),
        CheckConstraint(
            "failed_attempts >= 0 AND failed_attempts <= 8",
            name="ck_identity_mfa_challenges_failed_attempts",
        ),
        Index(
            "ix_identity_mfa_challenges_active",
            "identity_id",
            "expires_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Setup ceremonies keep the pending seed encrypted here until the user
    # proves a matching TOTP. Login challenges never populate this field.
    secret_envelope: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["IdentityMfaChallenge", "IdentityMfaRecoveryCode"]
