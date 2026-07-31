"""Make onboarding-linked private assistants owner-only.

Revision ID: private_assistant_access
Revises: deliverable_execution_shadow
Create Date: 2026-08-01 17:00:00

Earlier releases could adopt a legacy Private Assistant into onboarding while
leaving its old company-wide access policy in place.  That made another
member's companion appear in the Agent employee list.  The onboarding relation
is the product identity boundary, so every linked assistant is reconciled to a
private Agent with one owner-level permission.

The migration snapshots both the Agent policy and every permission row.  A
downgrade restores that exact state, but fails closed if an administrator has
changed the reconciled policy in the meantime.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "private_assistant_access"
down_revision: str | Sequence[str] | None = "deliverable_execution_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AGENT_STATE_TABLE = "__private_assistant_access_state"
_PERMISSION_STATE_TABLE = "__private_assistant_permission_state"


def _assert_source_schema() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    required = {
        "agents": {
            "id",
            "creator_id",
            "tenant_id",
            "deleted_at",
            "access_mode",
            "company_access_level",
        },
        "agent_permissions": {
            "id",
            "agent_id",
            "scope_type",
            "scope_id",
            "access_level",
        },
        "user_tenant_onboardings": {
            "id",
            "user_id",
            "tenant_id",
            "personal_assistant_agent_id",
        },
    }
    missing_tables = sorted(set(required) - table_names)
    if missing_tables:
        raise RuntimeError(
            "Private assistant access migration is missing source tables: "
            + ", ".join(missing_tables)
        )
    for table_name, expected_columns in required.items():
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing_columns = sorted(expected_columns - columns)
        if missing_columns:
            raise RuntimeError(
                f"Private assistant access migration found incomplete {table_name}: "
                + ", ".join(missing_columns)
            )


def _assert_links_are_owned() -> None:
    connection = op.get_bind()
    invalid_links = connection.execute(
        sa.text(
            """
            SELECT count(*)
              FROM user_tenant_onboardings AS onboarding
              LEFT JOIN agents AS agent
                ON agent.id = onboarding.personal_assistant_agent_id
             WHERE onboarding.personal_assistant_agent_id IS NOT NULL
               AND (
                    agent.id IS NULL
                    OR agent.creator_id IS DISTINCT FROM onboarding.user_id
                    OR agent.tenant_id IS DISTINCT FROM onboarding.tenant_id
                    OR agent.deleted_at IS NOT NULL
               )
            """
        )
    ).scalar_one()
    if invalid_links:
        raise RuntimeError(
            "Private assistant access migration found an unowned, cross-tenant, or deleted onboarding link"
        )

    duplicate_links = connection.execute(
        sa.text(
            """
            SELECT count(*)
              FROM (
                    SELECT personal_assistant_agent_id
                      FROM user_tenant_onboardings
                     WHERE personal_assistant_agent_id IS NOT NULL
                     GROUP BY personal_assistant_agent_id
                    HAVING count(*) <> 1
              ) AS duplicate
            """
        )
    ).scalar_one()
    if duplicate_links:
        raise RuntimeError(
            "Private assistant access migration found one Agent linked to multiple onboarding records"
        )


def _assert_downgrade_is_safe() -> None:
    connection = op.get_bind()
    changed_agents = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
              FROM {_AGENT_STATE_TABLE} AS state
              LEFT JOIN agents AS agent ON agent.id = state.agent_id
             WHERE agent.id IS NULL
                OR agent.access_mode <> 'private'
                OR agent.company_access_level <> 'use'
            """
        )
    ).scalar_one()
    if changed_agents:
        raise RuntimeError(
            "Private assistant access policy changed after migration; refusing to overwrite administrator changes"
        )

    changed_permissions = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
              FROM (
                    SELECT
                        state.agent_id,
                        count(permission.id) AS permission_count,
                        count(permission.id) FILTER (
                            WHERE permission.scope_type::text = 'user'
                              AND permission.scope_id = state.owner_user_id
                              AND permission.access_level = 'manage'
                        ) AS expected_count
                      FROM {_AGENT_STATE_TABLE} AS state
                      LEFT JOIN agent_permissions AS permission
                        ON permission.agent_id = state.agent_id
                     GROUP BY state.agent_id
                    HAVING count(permission.id) <> 1
                        OR count(permission.id) FILTER (
                            WHERE permission.scope_type::text = 'user'
                              AND permission.scope_id = state.owner_user_id
                              AND permission.access_level = 'manage'
                        ) <> 1
              ) AS changed
            """
        )
    ).scalar_one()
    if changed_permissions:
        raise RuntimeError(
            "Private assistant permissions changed after migration; refusing to overwrite administrator changes"
        )


def upgrade() -> None:
    _assert_source_schema()
    inspector = sa.inspect(op.get_bind())
    state_tables = {
        _AGENT_STATE_TABLE,
        _PERMISSION_STATE_TABLE,
    } & set(inspector.get_table_names())
    if state_tables:
        raise RuntimeError(
            "Private assistant access migration found partial state tables: "
            + ", ".join(sorted(state_tables))
        )

    _assert_links_are_owned()

    op.create_table(
        _AGENT_STATE_TABLE,
        sa.Column("onboarding_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prior_access_mode", sa.String(length=20), nullable=False),
        sa.Column("prior_company_access_level", sa.String(length=20), nullable=False),
    )
    op.create_table(
        _PERMISSION_STATE_TABLE,
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("access_level", sa.String(length=20), nullable=False),
    )

    op.execute(
        sa.text(
            f"""
            INSERT INTO {_AGENT_STATE_TABLE} (
                onboarding_id,
                agent_id,
                owner_user_id,
                prior_access_mode,
                prior_company_access_level
            )
            SELECT
                onboarding.id,
                agent.id,
                onboarding.user_id,
                agent.access_mode,
                agent.company_access_level
              FROM user_tenant_onboardings AS onboarding
              JOIN agents AS agent
                ON agent.id = onboarding.personal_assistant_agent_id
             WHERE onboarding.personal_assistant_agent_id IS NOT NULL
               AND (
                    agent.access_mode <> 'private'
                    OR agent.company_access_level <> 'use'
                    OR (
                        SELECT count(*)
                          FROM agent_permissions AS permission
                         WHERE permission.agent_id = agent.id
                    ) <> 1
                    OR NOT EXISTS (
                        SELECT 1
                          FROM agent_permissions AS permission
                         WHERE permission.agent_id = agent.id
                           AND permission.scope_type::text = 'user'
                           AND permission.scope_id = onboarding.user_id
                           AND permission.access_level = 'manage'
                    )
               )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_PERMISSION_STATE_TABLE} (
                permission_id,
                agent_id,
                scope_type,
                scope_id,
                access_level
            )
            SELECT
                permission.id,
                permission.agent_id,
                permission.scope_type::text,
                permission.scope_id,
                permission.access_level
              FROM agent_permissions AS permission
              JOIN {_AGENT_STATE_TABLE} AS state
                ON state.agent_id = permission.agent_id
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE agents AS agent
               SET access_mode = 'private',
                   company_access_level = 'use'
              FROM {_AGENT_STATE_TABLE} AS state
             WHERE agent.id = state.agent_id
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            DELETE FROM agent_permissions AS permission
             USING {_AGENT_STATE_TABLE} AS state
             WHERE permission.agent_id = state.agent_id
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO agent_permissions (
                id,
                agent_id,
                scope_type,
                scope_id,
                access_level
            )
            SELECT
                gen_random_uuid(),
                state.agent_id,
                'user'::permission_scope_enum,
                state.owner_user_id,
                'manage'
              FROM {_AGENT_STATE_TABLE} AS state
            """
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    state_tables = {_AGENT_STATE_TABLE, _PERMISSION_STATE_TABLE}
    present = state_tables & table_names
    if not present:
        return
    if present != state_tables:
        raise RuntimeError(
            "Private assistant access downgrade found partial state tables: "
            + ", ".join(sorted(present))
        )

    _assert_downgrade_is_safe()
    op.execute(
        sa.text(
            f"""
            DELETE FROM agent_permissions AS permission
             USING {_AGENT_STATE_TABLE} AS state
             WHERE permission.agent_id = state.agent_id
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO agent_permissions (
                id,
                agent_id,
                scope_type,
                scope_id,
                access_level
            )
            SELECT
                permission_id,
                agent_id,
                scope_type::permission_scope_enum,
                scope_id,
                access_level
              FROM {_PERMISSION_STATE_TABLE}
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE agents AS agent
               SET access_mode = state.prior_access_mode,
                   company_access_level = state.prior_company_access_level
              FROM {_AGENT_STATE_TABLE} AS state
             WHERE agent.id = state.agent_id
            """
        )
    )
    op.drop_table(_PERMISSION_STATE_TABLE)
    op.drop_table(_AGENT_STATE_TABLE)
