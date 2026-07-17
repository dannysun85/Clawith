"""Disable synthetic local passwords on SSO-origin identities.

Revision ID: sso_password_login
Revises: alert_worker_attribution
Create Date: 2026-07-16
"""

from collections.abc import Sequence
import json
import uuid

from alembic import op
import sqlalchemy as sa

from app.core.identity_provider_secrets import (
    is_identity_provider_config_envelope,
    open_identity_provider_config,
    seal_legacy_identity_provider_config,
)


revision: str = "sso_password_login"
down_revision: str | Sequence[str] | None = "alert_worker_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _identity_provider_config_rows(bind):
    return bind.execute(
        sa.text(
            """
            SELECT id, config
            FROM identity_providers
            ORDER BY id
            """
        )
    ).mappings()


def upgrade() -> None:
    bind = op.get_bind()
    if "password_login_enabled" not in _column_names("identities"):
        op.add_column(
            "identities",
            sa.Column(
                "password_login_enabled",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )
    if "auth_version" not in _column_names("identities"):
        op.add_column(
            "identities",
            sa.Column(
                "auth_version",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )

    if "activation_pending_email_verification" not in _column_names("users"):
        # The column starts fail-closed. Historical inactive rows have no
        # trustworthy provenance and are deliberately not backfilled.
        op.add_column(
            "users",
            sa.Column(
                "activation_pending_email_verification",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )

    if "initiator_nonce_hash" not in _column_names("sso_scan_sessions"):
        op.add_column(
            "sso_scan_sessions",
            sa.Column("initiator_nonce_hash", sa.String(length=64), nullable=True),
        )
    if "ix_sso_scan_sessions_expires_at" not in _index_names("sso_scan_sessions"):
        op.create_index(
            "ix_sso_scan_sessions_expires_at",
            "sso_scan_sessions",
            ["expires_at"],
            unique=False,
        )

    # Keep migration duration bounded and independent from bcrypt work factor.
    # Historical Web registrations retain local-password capability.  Pure SSO
    # identities fail closed, while their existing hash is preserved for an
    # operator audit or a later verified password-reset recovery.  This is one
    # set-based update rather than an unbounded Python bcrypt loop while every
    # application writer is stopped.
    bind.execute(
        sa.text(
            """
            UPDATE identities AS i
            SET password_login_enabled = CASE
                    WHEN i.password_hash IS NOT NULL
                     AND EXISTS (
                        SELECT 1
                        FROM users AS u
                        WHERE u.identity_id = i.id
                          AND u.registration_source = 'web'
                    )
                     AND NOT EXISTS (
                        SELECT 1
                        FROM users AS u
                        WHERE u.identity_id = i.id
                          AND COALESCE(u.registration_source, '') <> 'web'
                    )
                     AND NOT EXISTS (
                        SELECT 1
                        FROM users AS u
                        JOIN org_members AS om ON om.user_id = u.id
                        LEFT JOIN identity_providers AS ip ON ip.id = om.provider_id
                        WHERE u.identity_id = i.id
                          AND om.provider_id IS NOT NULL
                          AND (
                            ip.id IS NULL
                            OR lower(trim(ip.provider_type)) NOT IN ('web', 'platform')
                          )
                    )
                    THEN true
                    ELSE false
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE i.password_login_enabled IS DISTINCT FROM CASE
                    WHEN i.password_hash IS NOT NULL
                     AND EXISTS (
                        SELECT 1
                        FROM users AS u
                        WHERE u.identity_id = i.id
                          AND u.registration_source = 'web'
                    )
                     AND NOT EXISTS (
                        SELECT 1
                        FROM users AS u
                        WHERE u.identity_id = i.id
                          AND COALESCE(u.registration_source, '') <> 'web'
                    )
                     AND NOT EXISTS (
                        SELECT 1
                        FROM users AS u
                        JOIN org_members AS om ON om.user_id = u.id
                        LEFT JOIN identity_providers AS ip ON ip.id = om.provider_id
                        WHERE u.identity_id = i.id
                          AND om.provider_id IS NOT NULL
                          AND (
                            ip.id IS NULL
                            OR lower(trim(ip.provider_type)) NOT IN ('web', 'platform')
                          )
                    )
                    THEN true
                    ELSE false
                END
            """
        )
    )

    duplicate_emails = list(
        bind.execute(
            sa.text(
                """
                SELECT lower(trim(email)) AS canonical_email, count(*) AS duplicate_count
                FROM identities
                WHERE email IS NOT NULL
                GROUP BY lower(trim(email))
                HAVING count(*) > 1
                LIMIT 20
                """
            )
        ).mappings()
    )
    if duplicate_emails:
        raise RuntimeError(
            "Case-insensitive duplicate identity emails require manual merge before "
            f"upgrade: sample_group_count={len(duplicate_emails)}"
        )
    blank_email_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM identities
            WHERE email IS NOT NULL
              AND trim(email) = ''
            """
        )
    ).scalar_one()
    if blank_email_count:
        raise RuntimeError(
            "Blank identity emails require operator cleanup before upgrade: "
            f"count={blank_email_count}"
        )
    bind.execute(
        sa.text(
            """
            UPDATE identities
            SET email = lower(trim(email)),
                updated_at = CURRENT_TIMESTAMP
            WHERE email IS NOT NULL
              AND email <> lower(trim(email))
            """
        )
    )
    if "uq_identities_email_ci" not in _index_names("identities"):
        op.create_index(
            "uq_identities_email_ci",
            "identities",
            [sa.text("lower(email)")],
            unique=True,
            postgresql_where=sa.text("email IS NOT NULL"),
            sqlite_where=sa.text("email IS NOT NULL"),
        )
    constraint_names = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints("identities")
    }
    if "ck_identities_email_canonical" not in constraint_names:
        op.create_check_constraint(
            "ck_identities_email_canonical",
            "identities",
            "email IS NULL OR (email = lower(trim(email)) AND email <> '')",
        )

    duplicate_providers = list(
        bind.execute(
            sa.text(
                """
                SELECT tenant_id, provider_type, count(*) AS duplicate_count
                FROM identity_providers
                GROUP BY tenant_id, provider_type
                HAVING count(*) > 1
                LIMIT 20
                """
            )
        ).mappings()
    )
    if duplicate_providers:
        raise RuntimeError(
            "Duplicate identity provider scopes require manual merge before "
            f"upgrade: sample_group_count={len(duplicate_providers)}"
        )
    provider_member_scope_mismatches = list(
        bind.execute(
            sa.text(
                """
                SELECT om.id AS org_member_id, om.provider_id
                FROM org_members AS om
                JOIN identity_providers AS ip ON ip.id = om.provider_id
                WHERE COALESCE(CAST(om.tenant_id AS VARCHAR), '')
                   <> COALESCE(CAST(ip.tenant_id AS VARCHAR), '')
                LIMIT 20
                """
            )
        ).mappings()
    )
    if provider_member_scope_mismatches:
        raise RuntimeError(
            "Provider/member tenant mismatches require operator repair before "
            f"upgrade: sample_count={len(provider_member_scope_mismatches)}"
        )
    tenant_member_user_scope_mismatches = list(
        bind.execute(
            sa.text(
                """
                SELECT om.id AS org_member_id, om.user_id
                FROM org_members AS om
                JOIN identity_providers AS ip ON ip.id = om.provider_id
                JOIN users AS u ON u.id = om.user_id
                WHERE ip.tenant_id IS NOT NULL
                  AND (
                    COALESCE(CAST(om.tenant_id AS VARCHAR), '')
                    <> COALESCE(CAST(u.tenant_id AS VARCHAR), '')
                    OR COALESCE(CAST(ip.tenant_id AS VARCHAR), '')
                    <> COALESCE(CAST(u.tenant_id AS VARCHAR), '')
                  )
                LIMIT 20
                """
            )
        ).mappings()
    )
    if tenant_member_user_scope_mismatches:
        raise RuntimeError(
            "Tenant provider/member/user scope mismatches require operator repair "
            "before upgrade: "
            f"sample_count={len(tenant_member_user_scope_mismatches)}"
        )
    proactive_channel_scope_mismatches = list(
        bind.execute(
            sa.text(
                """
                SELECT ar.id AS relationship_id, ar.agent_id, ar.member_id
                FROM agent_relationships AS ar
                JOIN agents AS a ON a.id = ar.agent_id
                JOIN org_members AS om ON om.id = ar.member_id
                LEFT JOIN identity_providers AS ip ON ip.id = om.provider_id
                WHERE a.tenant_id IS NULL
                   OR om.tenant_id IS NULL
                   OR ip.id IS NULL
                   OR ip.tenant_id IS NULL
                   OR COALESCE(CAST(a.tenant_id AS VARCHAR), '')
                      <> COALESCE(CAST(om.tenant_id AS VARCHAR), '')
                   OR COALESCE(CAST(a.tenant_id AS VARCHAR), '')
                      <> COALESCE(CAST(ip.tenant_id AS VARCHAR), '')
                LIMIT 20
                """
            )
        ).mappings()
    )
    if proactive_channel_scope_mismatches:
        raise RuntimeError(
            "Agent/member/provider tenant mismatches require operator repair before "
            f"upgrade: sample_count={len(proactive_channel_scope_mismatches)}"
        )
    if "uq_identity_providers_global_type" not in _index_names("identity_providers"):
        op.create_index(
            "uq_identity_providers_global_type",
            "identity_providers",
            ["provider_type"],
            unique=True,
            postgresql_where=sa.text("tenant_id IS NULL"),
            sqlite_where=sa.text("tenant_id IS NULL"),
        )
    if "uq_identity_providers_tenant_type" not in _index_names("identity_providers"):
        op.create_index(
            "uq_identity_providers_tenant_type",
            "identity_providers",
            ["tenant_id", "provider_type"],
            unique=True,
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
            sqlite_where=sa.text("tenant_id IS NOT NULL"),
        )

    for field in ("open_id", "unionid", "external_id"):
        duplicates = list(
            bind.execute(
                sa.text(
                    f"""
                    SELECT provider_id, {field}, count(*) AS duplicate_count
                    FROM org_members
                    WHERE provider_id IS NOT NULL
                      AND {field} IS NOT NULL
                    GROUP BY provider_id, {field}
                    HAVING count(*) > 1
                    LIMIT 20
                    """
                )
            ).mappings()
        )
        if duplicates:
            raise RuntimeError(
                f"Duplicate provider {field} mappings require manual merge before "
                f"upgrade: sample_group_count={len(duplicates)}"
            )
        index_name = f"uq_org_members_provider_{field}"
        if index_name not in _index_names("org_members"):
            predicate = sa.text(f"provider_id IS NOT NULL AND {field} IS NOT NULL")
            op.create_index(
                index_name,
                "org_members",
                ["provider_id", field],
                unique=True,
                postgresql_where=predicate,
                sqlite_where=predicate,
            )

    bind.execute(
        sa.text(
            """
            UPDATE identities
            SET password_login_enabled = false
            WHERE password_hash IS NULL
              AND password_login_enabled IS DISTINCT FROM false
            """
        )
    )

    duplicate_memberships = list(
        bind.execute(
            sa.text(
                """
                SELECT identity_id, tenant_id, count(*) AS duplicate_count
                FROM users
                WHERE identity_id IS NOT NULL
                  AND tenant_id IS NOT NULL
                GROUP BY identity_id, tenant_id
                HAVING count(*) > 1
                LIMIT 20
                """
            )
        ).mappings()
    )
    if duplicate_memberships:
        raise RuntimeError(
            "Duplicate users(identity_id, tenant_id) rows must be audited before "
            f"upgrade: sample_group_count={len(duplicate_memberships)}"
        )
    duplicate_tenantless_memberships = list(
        bind.execute(
            sa.text(
                """
                SELECT identity_id, count(*) AS duplicate_count
                FROM users
                WHERE identity_id IS NOT NULL
                  AND tenant_id IS NULL
                GROUP BY identity_id
                HAVING count(*) > 1
                LIMIT 20
                """
            )
        ).mappings()
    )
    if duplicate_tenantless_memberships:
        raise RuntimeError(
            "Duplicate tenantless users(identity_id) rows must be audited before "
            f"upgrade: sample_group_count={len(duplicate_tenantless_memberships)}"
        )
    if "uq_users_identity_tenant" not in _index_names("users"):
        membership_predicate = sa.text(
            "identity_id IS NOT NULL AND tenant_id IS NOT NULL"
        )
        op.create_index(
            "uq_users_identity_tenant",
            "users",
            ["identity_id", "tenant_id"],
            unique=True,
            postgresql_where=membership_predicate,
            sqlite_where=membership_predicate,
        )
    if "uq_users_identity_tenantless" not in _index_names("users"):
        tenantless_predicate = sa.text(
            "identity_id IS NOT NULL AND tenant_id IS NULL"
        )
        op.create_index(
            "uq_users_identity_tenantless",
            "users",
            ["identity_id"],
            unique=True,
            postgresql_where=tenantless_predicate,
            sqlite_where=tenantless_predicate,
        )

    # Existing configured channels predate explicit provider authorization.
    # Provision exactly one active, non-SSO provider per tenant/channel so the
    # runtime can become fail-closed without breaking already configured bots.
    configured_channels = list(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT a.tenant_id, cc.channel_type
                FROM channel_configs AS cc
                JOIN agents AS a ON a.id = cc.agent_id
                WHERE cc.is_configured = true
                  AND a.tenant_id IS NOT NULL
                """
            )
        ).mappings()
    )
    for channel in configured_channels:
        provider_type = (
            "teams"
            if str(channel["channel_type"]) == "microsoft_teams"
            else str(channel["channel_type"])
        )
        existing_provider = bind.execute(
            sa.text(
                """
                SELECT id
                FROM identity_providers
                WHERE tenant_id = :tenant_id
                  AND provider_type = :provider_type
                LIMIT 1
                """
            ),
            {
                "tenant_id": channel["tenant_id"],
                "provider_type": provider_type,
            },
        ).scalar_one_or_none()
        if existing_provider is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO identity_providers (
                        id, provider_type, name, is_active,
                        sso_login_enabled, config, tenant_id,
                        created_at, updated_at
                    ) VALUES (
                        :id, :provider_type, :name, true,
                        false, '{}', :tenant_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "provider_type": provider_type,
                    "name": provider_type.replace("_", " ").title(),
                    "tenant_id": channel["tenant_id"],
                },
            )
    # Historical inactive rows have no trustworthy provenance explaining why
    # they were disabled. Do not guess that they are merely waiting for email
    # verification: doing so could let a later verification reactivate an
    # administrator-disabled membership. New registrations set the explicit
    # activation marker in application code.

    # Global Google/GitHub providers were historically controlled only by
    # ``is_active``. Preserve that production behavior when introducing the
    # explicit login switch; tenant-scoped providers remain fail-closed and
    # continue to require an administrator choice.
    bind.execute(
        sa.text(
            """
            UPDATE identity_providers
            SET sso_login_enabled = true,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id IS NULL
              AND provider_type IN ('google', 'github')
              AND is_active = true
              AND sso_login_enabled IS DISTINCT FROM true
            """
        )
    )

    # The full provider config is one authenticated envelope.  Encrypting the
    # complete object protects nested service-account private keys and unknown
    # future credentials without relying on a brittle key-name allowlist.
    if bind.dialect.name == "postgresql":
        invalid_provider_config_count = bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM identity_providers
                WHERE config IS NOT NULL
                  AND jsonb_typeof(config::jsonb) IS DISTINCT FROM 'object'
                """
            )
        ).scalar_one()
        if invalid_provider_config_count:
            raise RuntimeError(
                "Non-object identity provider configs require operator cleanup "
                f"before upgrade: count={invalid_provider_config_count}"
            )
        op.alter_column(
            "identity_providers",
            "config",
            type_=sa.Text(),
            postgresql_using="config::text",
            existing_nullable=True,
        )
    for row in list(_identity_provider_config_rows(bind)):
        bind.execute(
            sa.text(
                """
                UPDATE identity_providers
                SET config = :config
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "config": seal_legacy_identity_provider_config(row["config"]),
            },
        )
    op.alter_column(
        "identities",
        "password_login_enabled",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )


def downgrade() -> None:
    # No password hashes are destroyed by upgrade.  Older code has no explicit
    # capability flag, so a downgrade must still be an operator-approved step.
    bind = op.get_bind()
    for row in list(_identity_provider_config_rows(bind)):
        value = row["config"]
        plain_config = (
            open_identity_provider_config(value)
            if is_identity_provider_config_envelope(value)
            else open_identity_provider_config(value or "{}")
        )
        bind.execute(
            sa.text(
                """
                UPDATE identity_providers
                SET config = :config
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "config": json.dumps(
                    plain_config,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        )
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "identity_providers",
            "config",
            type_=sa.JSON(),
            postgresql_using="config::json",
            existing_nullable=True,
        )
    for field in ("external_id", "unionid", "open_id"):
        index_name = f"uq_org_members_provider_{field}"
        if index_name in _index_names("org_members"):
            op.drop_index(index_name, table_name="org_members")
    if "uq_identity_providers_tenant_type" in _index_names("identity_providers"):
        op.drop_index("uq_identity_providers_tenant_type", table_name="identity_providers")
    if "uq_identity_providers_global_type" in _index_names("identity_providers"):
        op.drop_index("uq_identity_providers_global_type", table_name="identity_providers")
    if "uq_identities_email_ci" in _index_names("identities"):
        op.drop_index("uq_identities_email_ci", table_name="identities")
    constraint_names = {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_check_constraints("identities")
    }
    if "ck_identities_email_canonical" in constraint_names:
        op.drop_constraint(
            "ck_identities_email_canonical",
            "identities",
            type_="check",
        )
    if "uq_users_identity_tenant" in _index_names("users"):
        op.drop_index("uq_users_identity_tenant", table_name="users")
    if "uq_users_identity_tenantless" in _index_names("users"):
        op.drop_index("uq_users_identity_tenantless", table_name="users")
    if "ix_sso_scan_sessions_expires_at" in _index_names("sso_scan_sessions"):
        op.drop_index("ix_sso_scan_sessions_expires_at", table_name="sso_scan_sessions")
    if "initiator_nonce_hash" in _column_names("sso_scan_sessions"):
        op.drop_column("sso_scan_sessions", "initiator_nonce_hash")
    if "activation_pending_email_verification" in _column_names("users"):
        op.drop_column("users", "activation_pending_email_verification")
    if "password_login_enabled" in _column_names("identities"):
        op.drop_column("identities", "password_login_enabled")
    if "auth_version" in _column_names("identities"):
        op.drop_column("identities", "auth_version")
