"""User and organization models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.associationproxy import association_proxy

from app.database import Base
from app.core.identity_canonicalization import (
    canonicalize_email,
    canonicalize_phone,
    normalize_username,
    username_looks_like_contact,
)



class Identity(Base):
    """
    Physical Identity (Lark ID).
    Represents a natural person globally across all tenants.
    """

    __tablename__ = "identities"
    __table_args__ = (
        CheckConstraint(
            "email IS NULL OR (email = lower(trim(email)) AND email <> '')",
            name="ck_identities_email_canonical",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Global unique identifiers for login
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    
    # Global authentication
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Password authentication is an explicit capability.  SSO/channel-created
    # identities stay disabled until a user proves ownership through the
    # password-reset flow; this prevents provider identifiers from ever acting
    # as implicit local credentials.
    password_login_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    # Incrementing this value revokes every previously issued access token for
    # all tenant memberships belonging to the Identity.
    auth_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    
    # Global status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Verification status
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant_users: Mapped[list["User"]] = relationship(back_populates="identity")

    @validates("email")
    def _canonicalize_email(self, _key: str, value: str | None) -> str | None:
        """Apply the same canonical form enforced by the database schema."""
        return canonicalize_email(value)

    @validates("phone")
    def _canonicalize_phone(self, _key: str, value: str | None) -> str | None:
        return canonicalize_phone(value)

    @validates("username")
    def _validate_username(self, _key: str, value: str | None) -> str | None:
        username = normalize_username(value)
        if username_looks_like_contact(username):
            raise ValueError("Username cannot be an email address or phone number")
        return username


class User(Base):
    """
    Tenant Identity (Member ID).
    Represents a person's role and profile within a specific company.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "preferred_chat_tier IS NULL OR preferred_chat_tier IN ('lite', 'pro', 'ultra')",
            name="ck_users_preferred_chat_tier",
        ),
    )
    # Note: Unique constraints for (tenant_id, username), (tenant_id, email) and (tenant_id, primary_mobile)
    # are handled via partial unique indexes in migration to allow NULL values

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Link to global identity
    identity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("identities.id"), index=True)

    # Tenant context
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))

    # Tenant-specific profile
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(
        Enum("platform_admin", "org_admin", "agent_admin", "member", name="user_role_enum"),
        default="member",
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Distinguish a membership paused only until email ownership is proven
    # from one disabled for an administrative or security reason.  Verification
    # flows may activate only rows carrying this explicit provenance marker.
    activation_pending_email_verification: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    registration_source: Mapped[str | None] = mapped_column(String(50), default="web")

    # The user's latest explicit Lite/Pro/Ultra choice for first-party chats.
    # Agent defaults remain authoritative for automations and background work.
    preferred_chat_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preferred_chat_tier_revision: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Usage quotas (set by admin, defaults from tenant)
    quota_message_limit: Mapped[int] = mapped_column(Integer, default=50)
    quota_message_period: Mapped[str] = mapped_column(String(20), default="permanent")  # permanent|daily|weekly|monthly
    quota_messages_used: Mapped[int] = mapped_column(Integer, default=0)
    quota_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_max_agents: Mapped[int] = mapped_column(Integer, default=2)
    quota_agent_ttl_hours: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    # lazy="selectin" is required because association_proxy fields (email, username,
    # password_hash, email_verified, primary_mobile) delegate to this relationship.
    # Without eager loading, any proxy access in an async context triggers a synchronous
    # IO call inside a greenlet, raising sqlalchemy.exc.MissingGreenlet.
    identity: Mapped["Identity"] = relationship(back_populates="tenant_users", lazy="selectin")

    # Association proxies for backward compatibility
    email = association_proxy("identity", "email", creator=lambda val: Identity(email=val))
    username = association_proxy("identity", "username", creator=lambda val: Identity(username=val))
    password_hash = association_proxy("identity", "password_hash", creator=lambda val: Identity(password_hash=val))
    password_login_enabled = association_proxy(
        "identity",
        "password_login_enabled",
        creator=lambda val: Identity(password_login_enabled=val),
    )
    email_verified = association_proxy("identity", "email_verified", creator=lambda val: Identity(email_verified=val))
    primary_mobile = association_proxy("identity", "phone", creator=lambda val: Identity(phone=val))

    created_agents: Mapped[list["Agent"]] = relationship(back_populates="creator", foreign_keys="Agent.creator_id")


# Forward reference for Agent used in User relationship
from app.models.agent import Agent  # noqa: E402, F401
from app.models.org import OrgMember  # noqa: E402, F401
