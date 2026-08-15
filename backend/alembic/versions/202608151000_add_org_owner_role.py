"""Add the tenant-scoped org_owner role.

Revision ID: add_org_owner_role
Revises: media_daily_allowance_claims
Create Date: 2026-08-15 10:00:00
"""

from collections.abc import Sequence

from alembic import op


revision: str = "add_org_owner_role"
down_revision: str | Sequence[str] | None = "media_daily_allowance_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL makes a newly added enum label usable only after the
    # transaction that added it commits. The next migration creates a partial
    # index whose predicate references ``org_owner``, so this revision needs a
    # real commit boundary even when ``alembic upgrade head`` applies both
    # revisions in one command.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role_enum ADD VALUE IF NOT EXISTS 'org_owner' AFTER 'platform_admin'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum label in place. Rebuild the type only after
    # the dependent governance migration has converted all owners to admins.
    op.execute("UPDATE users SET role = 'org_admin' WHERE role::text = 'org_owner'")
    op.execute("ALTER TYPE user_role_enum RENAME TO user_role_enum_with_owner")
    op.execute("CREATE TYPE user_role_enum AS ENUM ('platform_admin', 'org_admin', 'agent_admin', 'member')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role_enum "
        "USING role::text::user_role_enum"
    )
    op.execute("DROP TYPE user_role_enum_with_owner")
