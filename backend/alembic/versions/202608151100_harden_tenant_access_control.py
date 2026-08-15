"""Harden tenant data scope and Agent object grants.

Revision ID: harden_tenant_access_control
Revises: identity_membership_governance
Create Date: 2026-08-15 11:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "harden_tenant_access_control"
down_revision: str | Sequence[str] | None = "identity_membership_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _check_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        str(constraint["name"])
        for constraint in inspector.get_check_constraints(table_name)
        if constraint.get("name")
    }


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        str(index["name"])
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }


def _foreign_key_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        str(constraint["name"])
        for constraint in inspector.get_foreign_keys(table_name)
        if constraint.get("name")
    }


def _add_tenant_scope_columns() -> None:
    audit_columns = _column_names("audit_logs")
    if "tenant_id" not in audit_columns:
        op.add_column(
            "audit_logs",
            sa.Column("tenant_id", sa.Uuid(), nullable=True),
        )
    if "fk_audit_logs_tenant_id_tenants" not in _foreign_key_names("audit_logs"):
        op.create_foreign_key(
            "fk_audit_logs_tenant_id_tenants",
            "audit_logs",
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_audit_logs_tenant_id" not in _index_names("audit_logs"):
        op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])

    enterprise_columns = _column_names("enterprise_info")
    if "tenant_id" not in enterprise_columns:
        op.add_column(
            "enterprise_info",
            sa.Column("tenant_id", sa.Uuid(), nullable=True),
        )
    if "fk_enterprise_info_tenant_id_tenants" not in _foreign_key_names("enterprise_info"):
        op.create_foreign_key(
            "fk_enterprise_info_tenant_id_tenants",
            "enterprise_info",
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "ix_enterprise_info_tenant_id" not in _index_names("enterprise_info"):
        op.create_index("ix_enterprise_info_tenant_id", "enterprise_info", ["tenant_id"])


def _backfill_tenant_scope() -> None:
    op.execute(
        sa.text(
            """
            UPDATE audit_logs AS audit
               SET tenant_id = agent.tenant_id
              FROM agents AS agent
             WHERE audit.tenant_id IS NULL
               AND audit.agent_id = agent.id
               AND agent.tenant_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE audit_logs AS audit
               SET tenant_id = member.tenant_id
              FROM users AS member
             WHERE audit.tenant_id IS NULL
               AND audit.user_id = member.id
               AND member.tenant_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE enterprise_info AS info
               SET tenant_id = member.tenant_id
              FROM users AS member
             WHERE info.tenant_id IS NULL
               AND info.updated_by = member.id
               AND member.tenant_id IS NOT NULL
            """
        )
    )


def _replace_enterprise_info_uniqueness() -> None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("enterprise_info"):
        if constraint.get("column_names") == ["info_type"] and constraint.get("name"):
            op.drop_constraint(
                str(constraint["name"]),
                "enterprise_info",
                type_="unique",
            )
    if "uq_enterprise_info_tenant_type" not in _index_names("enterprise_info"):
        op.create_index(
            "uq_enterprise_info_tenant_type",
            "enterprise_info",
            ["tenant_id", "info_type"],
            unique=True,
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
        )


def _normalize_agent_permissions() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agent_permissions
               SET access_level = 'use'
             WHERE access_level NOT IN ('use', 'manage')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE agent_permissions
               SET scope_id = NULL
             WHERE scope_type = 'company'
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM agent_permissions
             WHERE scope_type <> 'company'
               AND scope_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM agent_permissions AS permission
             USING (
                 SELECT id
                   FROM (
                       SELECT id,
                              row_number() OVER (
                                  PARTITION BY agent_id, scope_type, scope_id
                                  ORDER BY (access_level = 'manage') DESC, id
                              ) AS duplicate_number
                         FROM agent_permissions
                   ) AS ranked
                  WHERE ranked.duplicate_number > 1
             ) AS duplicate
             WHERE permission.id = duplicate.id
            """
        )
    )


def _add_agent_permission_constraints() -> None:
    checks = _check_names("agent_permissions")
    if "ck_agent_permissions_access_level" not in checks:
        op.create_check_constraint(
            "ck_agent_permissions_access_level",
            "agent_permissions",
            "access_level IN ('use', 'manage')",
        )
    if "ck_agent_permissions_scope_id" not in checks:
        op.create_check_constraint(
            "ck_agent_permissions_scope_id",
            "agent_permissions",
            "(scope_type = 'company' AND scope_id IS NULL) OR "
            "(scope_type IN ('department', 'user') AND scope_id IS NOT NULL)",
        )

    indexes = _index_names("agent_permissions")
    if "uq_agent_permissions_company" not in indexes:
        op.create_index(
            "uq_agent_permissions_company",
            "agent_permissions",
            ["agent_id"],
            unique=True,
            postgresql_where=sa.text("scope_type = 'company'"),
        )
    if "uq_agent_permissions_scoped" not in indexes:
        op.create_index(
            "uq_agent_permissions_scoped",
            "agent_permissions",
            ["agent_id", "scope_type", "scope_id"],
            unique=True,
            postgresql_where=sa.text("scope_type <> 'company'"),
        )


def upgrade() -> None:
    _add_tenant_scope_columns()
    _backfill_tenant_scope()
    _replace_enterprise_info_uniqueness()
    _normalize_agent_permissions()
    _add_agent_permission_constraints()


def downgrade() -> None:
    op.drop_index("uq_agent_permissions_scoped", table_name="agent_permissions")
    op.drop_index("uq_agent_permissions_company", table_name="agent_permissions")
    op.drop_constraint(
        "ck_agent_permissions_scope_id",
        "agent_permissions",
        type_="check",
    )
    op.drop_constraint(
        "ck_agent_permissions_access_level",
        "agent_permissions",
        type_="check",
    )

    op.drop_index("uq_enterprise_info_tenant_type", table_name="enterprise_info")
    op.execute(
        sa.text(
            """
            DELETE FROM enterprise_info AS info
             USING (
                 SELECT id
                   FROM (
                       SELECT id,
                              row_number() OVER (
                                  PARTITION BY info_type
                                  ORDER BY updated_at DESC, id
                              ) AS duplicate_number
                         FROM enterprise_info
                   ) AS ranked
                  WHERE ranked.duplicate_number > 1
             ) AS duplicate
             WHERE info.id = duplicate.id
            """
        )
    )
    op.create_unique_constraint(
        "enterprise_info_info_type_key",
        "enterprise_info",
        ["info_type"],
    )
    op.drop_index("ix_enterprise_info_tenant_id", table_name="enterprise_info")
    op.drop_constraint(
        "fk_enterprise_info_tenant_id_tenants",
        "enterprise_info",
        type_="foreignkey",
    )
    op.drop_column("enterprise_info", "tenant_id")

    op.drop_index("ix_audit_logs_tenant_id", table_name="audit_logs")
    op.drop_constraint(
        "fk_audit_logs_tenant_id_tenants",
        "audit_logs",
        type_="foreignkey",
    )
    op.drop_column("audit_logs", "tenant_id")
