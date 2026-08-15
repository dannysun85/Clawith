"""Separate account grants, organization invitations, and tenant ownership.

Revision ID: identity_membership_governance
Revises: add_org_owner_role
Create Date: 2026-08-15 10:30:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "identity_membership_governance"
down_revision: str | Sequence[str] | None = "add_org_owner_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GOVERNANCE_TABLES = {
    "identity_capability_grants",
    "registration_grants",
    "organization_invitations",
    "organization_join_links",
    "platform_support_sessions",
    "tenant_ownership_transfers",
    "tenant_ownership_resolutions",
}
_TENANT_GOVERNANCE_COLUMNS = {
    "created_by_identity_id",
    "creation_idempotency_key_hash",
    "owner_user_id",
    "owner_resolution_required",
    "initialization_completed_at",
    "initialized_by_user_id",
    "deletion_requested_at",
    "deletion_scheduled_for",
    "deletion_requested_by_user_id",
}


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.strip().upper().encode("utf-8")).hexdigest()


def _legacy_status(*, is_active: bool, used_count: int, max_uses: int) -> str:
    if not is_active:
        return "revoked"
    if used_count >= max_uses:
        return "exhausted"
    return "active"


def _governance_schema_preexists() -> bool:
    """Distinguish the metadata-based fresh bootstrap from historical DDL.

    ``initial_schema`` intentionally creates current ORM metadata on an empty
    database. A complete pre-existing governance schema is therefore the fresh
    path. Any partial shape is unsafe and must not be silently repaired here.
    """
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    present_tables = _GOVERNANCE_TABLES & table_names
    tenant_columns = {
        str(column["name"])
        for column in inspector.get_columns("tenants")
    }
    present_columns = _TENANT_GOVERNANCE_COLUMNS & tenant_columns
    if present_tables and present_tables != _GOVERNANCE_TABLES:
        raise RuntimeError(
            "Identity governance migration found partial tables: "
            + ", ".join(sorted(present_tables))
        )
    if present_columns and present_columns != _TENANT_GOVERNANCE_COLUMNS:
        raise RuntimeError(
            "Identity governance migration found partial tenant columns: "
            + ", ".join(sorted(present_columns))
        )
    if bool(present_tables) != bool(present_columns):
        raise RuntimeError(
            "Identity governance migration found mismatched table and tenant-column state"
        )
    return present_tables == _GOVERNANCE_TABLES


def _create_governance_tables() -> None:
    op.create_table(
        "identity_capability_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.Column("granted_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("capability <> ''", name="ck_identity_capability_grants_capability"),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_identity_id"], ["identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by_identity_id"], ["identities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_identity_capability_grants_active",
        "identity_capability_grants",
        ["identity_id", "capability"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_identity_capability_grants_identity_id",
        "identity_capability_grants",
        ["identity_id"],
    )

    op.create_table(
        "registration_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("legacy_invitation_code_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("max_uses > 0", name="ck_registration_grants_max_uses"),
        sa.CheckConstraint(
            "used_count >= 0 AND used_count <= max_uses",
            name="ck_registration_grants_used_count",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'exhausted', 'revoked')",
            name="ck_registration_grants_status",
        ),
        sa.ForeignKeyConstraint(["created_by_identity_id"], ["identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["legacy_invitation_code_id"], ["invitation_codes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_invitation_code_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_registration_grants_token_hash", "registration_grants", ["token_hash"])
    op.create_index("ix_registration_grants_status", "registration_grants", ["status"])

    op.create_table(
        "organization_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_email", sa.String(length=255), nullable=False),
        sa.Column("invited_role", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invited_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("accepted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "invited_role IN ('member', 'org_admin', 'org_owner')",
            name="ck_organization_invitations_role",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked', 'expired')",
            name="ck_organization_invitations_status",
        ),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_organization_invitations_tenant_id", "organization_invitations", ["tenant_id"])
    op.create_index("ix_organization_invitations_target_email", "organization_invitations", ["target_email"])
    op.create_index("ix_organization_invitations_token_hash", "organization_invitations", ["token_hash"])
    op.create_index("ix_organization_invitations_status", "organization_invitations", ["status"])
    op.create_index(
        "uq_organization_invitations_pending_email",
        "organization_invitations",
        ["tenant_id", "target_email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "organization_join_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("legacy_invitation_code_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("max_uses > 0", name="ck_organization_join_links_max_uses"),
        sa.CheckConstraint(
            "used_count >= 0 AND used_count <= max_uses",
            name="ck_organization_join_links_used_count",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'exhausted', 'revoked')",
            name="ck_organization_join_links_status",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["legacy_invitation_code_id"], ["invitation_codes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_invitation_code_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_organization_join_links_tenant_id", "organization_join_links", ["tenant_id"])
    op.create_index("ix_organization_join_links_token_hash", "organization_join_links", ["token_hash"])
    op.create_index("ix_organization_join_links_status", "organization_join_links", ["status"])

    op.create_table(
        "platform_support_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("reason <> ''", name="ck_platform_support_sessions_reason"),
        sa.ForeignKeyConstraint(["platform_identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_platform_support_sessions_platform_identity_id", "platform_support_sessions", ["platform_identity_id"])
    op.create_index("ix_platform_support_sessions_tenant_id", "platform_support_sessions", ["tenant_id"])
    op.create_index("ix_platform_support_sessions_expires_at", "platform_support_sessions", ["expires_at"])

    op.create_table(
        "tenant_ownership_transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposed_owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'cancelled', 'expired')",
            name="ck_tenant_ownership_transfers_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proposed_owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tenant_ownership_transfers_tenant_id",
        "tenant_ownership_transfers",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tenant_ownership_transfers_proposed_owner_user_id",
        "tenant_ownership_transfers",
        ["proposed_owner_user_id"],
    )
    op.create_index(
        "ix_tenant_ownership_transfers_status",
        "tenant_ownership_transfers",
        ["status"],
    )
    op.create_index(
        "ix_tenant_ownership_transfers_expires_at",
        "tenant_ownership_transfers",
        ["expires_at"],
    )
    op.create_index(
        "uq_tenant_ownership_transfers_pending",
        "tenant_ownership_transfers",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "tenant_ownership_resolutions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column("candidate_user_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resolved_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_tenant_ownership_resolutions_status",
        ),
        sa.ForeignKeyConstraint(["resolved_by_identity_id"], ["identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_ownership_resolutions_tenant_id", "tenant_ownership_resolutions", ["tenant_id"])
    op.create_index("ix_tenant_ownership_resolutions_status", "tenant_ownership_resolutions", ["status"])


def _backfill_owners() -> None:
    connection = op.get_bind()
    tenant_ids = [row[0] for row in connection.execute(sa.text("SELECT id FROM tenants ORDER BY created_at, id"))]
    for tenant_id in tenant_ids:
        candidates = list(
            connection.execute(
                sa.text(
                    """
                    SELECT id
                      FROM users
                     WHERE tenant_id = :tenant_id
                       AND is_active IS TRUE
                       AND role::text IN ('org_admin', 'platform_admin')
                     ORDER BY created_at, id
                    """
                ),
                {"tenant_id": tenant_id},
            )
        )
        candidate_ids = [row[0] for row in candidates]
        if len(candidate_ids) == 1:
            owner_user_id = candidate_ids[0]
            connection.execute(
                sa.text(
                    "UPDATE tenants SET owner_user_id = :owner_user_id, "
                    "owner_resolution_required = false WHERE id = :tenant_id"
                ),
                {"owner_user_id": owner_user_id, "tenant_id": tenant_id},
            )
            connection.execute(
                sa.text("UPDATE users SET role = 'org_owner' WHERE id = :owner_user_id"),
                {"owner_user_id": owner_user_id},
            )
            continue

        reason = "missing_candidate" if not candidate_ids else "ambiguous_candidates"
        connection.execute(
            sa.text("UPDATE tenants SET owner_resolution_required = true WHERE id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO tenant_ownership_resolutions (
                    id, tenant_id, reason, candidate_user_ids, status
                ) VALUES (
                    :id, :tenant_id, :reason, CAST(:candidate_user_ids AS jsonb), 'open'
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "reason": reason,
                "candidate_user_ids": json.dumps([str(value) for value in candidate_ids]),
            },
        )


def _migrate_legacy_codes() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                """
                SELECT code.id, code.code, code.tenant_id, code.max_uses,
                       code.used_count, code.is_active, code.created_by,
                       code.created_at, creator.identity_id
                  FROM invitation_codes AS code
                  LEFT JOIN users AS creator ON creator.id = code.created_by
                 ORDER BY code.created_at, code.id
                """
            )
        ).mappings()
    )
    for row in rows:
        normalized = str(row["code"]).strip().upper()
        common = {
            "id": uuid.uuid4(),
            "token_hash": _token_hash(normalized),
            "token_prefix": normalized[:8],
            "max_uses": row["max_uses"],
            "used_count": row["used_count"],
            "status": _legacy_status(
                is_active=row["is_active"],
                used_count=row["used_count"],
                max_uses=row["max_uses"],
            ),
            "legacy_id": row["id"],
            "created_at": row["created_at"] or datetime.now(UTC),
        }
        if row["tenant_id"] is None:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO registration_grants (
                        id, token_hash, token_prefix, max_uses, used_count,
                        status, created_by_identity_id,
                        legacy_invitation_code_id, created_at, updated_at
                    ) VALUES (
                        :id, :token_hash, :token_prefix, :max_uses, :used_count,
                        :status, :creator_identity_id,
                        :legacy_id, :created_at, :created_at
                    )
                    """
                ),
                {**common, "creator_identity_id": row["identity_id"]},
            )
        else:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO organization_join_links (
                        id, tenant_id, token_hash, token_prefix, max_uses,
                        used_count, status, created_by_user_id,
                        legacy_invitation_code_id, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :token_hash, :token_prefix, :max_uses,
                        :used_count, :status, :created_by,
                        :legacy_id, :created_at, :created_at
                    )
                    """
                ),
                {**common, "tenant_id": row["tenant_id"], "created_by": row["created_by"]},
            )


def _backfill_company_create_capability() -> None:
    connection = op.get_bind()
    setting = connection.execute(
        sa.text("SELECT value FROM system_settings WHERE key = 'allow_self_create_company'")
    ).scalar_one_or_none()
    allow_existing = True
    if isinstance(setting, dict):
        allow_existing = bool(setting.get("enabled", True))
    if not allow_existing:
        return
    identity_ids = [
        row[0]
        for row in connection.execute(
            sa.text("SELECT id FROM identities WHERE is_active IS TRUE ORDER BY created_at, id")
        )
    ]
    for identity_id in identity_ids:
        connection.execute(
            sa.text(
                """
                INSERT INTO identity_capability_grants (
                    id, identity_id, capability, granted_at
                ) VALUES (
                    :id, :identity_id, 'company.create', now()
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": uuid.uuid4(), "identity_id": identity_id},
        )


def upgrade() -> None:
    schema_preexists = _governance_schema_preexists()
    if not schema_preexists:
        op.add_column(
            "tenants",
            sa.Column("created_by_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.add_column(
            "tenants",
            sa.Column("creation_idempotency_key_hash", sa.String(length=64), nullable=True),
        )
        op.add_column("tenants", sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.add_column(
            "tenants",
            sa.Column("owner_resolution_required", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
        op.add_column("tenants", sa.Column("initialization_completed_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("tenants", sa.Column("initialized_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.add_column("tenants", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("tenants", sa.Column("deletion_scheduled_for", sa.DateTime(timezone=True), nullable=True))
        op.add_column(
            "tenants",
            sa.Column("deletion_requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )

        _create_governance_tables()
    _backfill_owners()
    _migrate_legacy_codes()
    _backfill_company_create_capability()

    if not schema_preexists:
        op.create_index(
            "uq_tenants_creator_idempotency_key",
            "tenants",
            ["created_by_identity_id", "creation_idempotency_key_hash"],
            unique=True,
            postgresql_where=sa.text("creation_idempotency_key_hash IS NOT NULL"),
        )
        op.create_index(
            "uq_users_one_org_owner_per_tenant",
            "users",
            ["tenant_id"],
            unique=True,
            postgresql_where=sa.text("role = 'org_owner'"),
        )
        op.create_unique_constraint("uq_tenants_owner_user_id", "tenants", ["owner_user_id"])
        op.create_foreign_key(
            "fk_tenants_created_by_identity_id_identities",
            "tenants",
            "identities",
            ["created_by_identity_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_tenants_owner_user_id_users",
            "tenants",
            "users",
            ["owner_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_tenants_initialized_by_user_id_users",
            "tenants",
            "users",
            ["initialized_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_tenants_deletion_requested_by_user_id_users",
            "tenants",
            "users",
            ["deletion_requested_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    governance_fk_columns = {
        ("created_by_identity_id",),
        ("deletion_requested_by_user_id",),
        ("initialized_by_user_id",),
        ("owner_user_id",),
    }
    for foreign_key in inspector.get_foreign_keys("tenants"):
        columns = tuple(foreign_key.get("constrained_columns") or [])
        name = foreign_key.get("name")
        if columns in governance_fk_columns and name:
            op.drop_constraint(str(name), "tenants", type_="foreignkey")
    for constraint in inspector.get_unique_constraints("tenants"):
        columns = tuple(constraint.get("column_names") or [])
        name = constraint.get("name")
        if columns == ("owner_user_id",) and name:
            op.drop_constraint(str(name), "tenants", type_="unique")

    user_index_names = {str(index["name"]) for index in inspector.get_indexes("users")}
    if "uq_users_one_org_owner_per_tenant" in user_index_names:
        op.drop_index("uq_users_one_org_owner_per_tenant", table_name="users")
    tenant_index_names = {str(index["name"]) for index in inspector.get_indexes("tenants")}
    if "uq_tenants_creator_idempotency_key" in tenant_index_names:
        op.drop_index("uq_tenants_creator_idempotency_key", table_name="tenants")

    op.execute("UPDATE users SET role = 'org_admin' WHERE role::text = 'org_owner'")

    for table_name in (
        "tenant_ownership_resolutions",
        "tenant_ownership_transfers",
        "platform_support_sessions",
        "organization_join_links",
        "organization_invitations",
        "registration_grants",
        "identity_capability_grants",
    ):
        op.drop_table(table_name)

    op.drop_column("tenants", "deletion_requested_by_user_id")
    op.drop_column("tenants", "deletion_scheduled_for")
    op.drop_column("tenants", "deletion_requested_at")
    op.drop_column("tenants", "initialized_by_user_id")
    op.drop_column("tenants", "initialization_completed_at")
    op.drop_column("tenants", "owner_resolution_required")
    op.drop_column("tenants", "owner_user_id")
    op.drop_column("tenants", "creation_idempotency_key_hash")
    op.drop_column("tenants", "created_by_identity_id")
