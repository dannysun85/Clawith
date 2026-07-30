"""Store provider subscription tier for plan-bound credentials.

Revision ID: add_provider_plan_tier
Revises: reconcile_m3_runtime_caps
Create Date: 2026-07-26 12:00:00
"""

from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = "add_provider_plan_tier"
down_revision: str | Sequence[str] | None = "reconcile_m3_runtime_caps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``initial_schema`` creates tables from current ORM metadata for fresh
    # installs, while upgraded production databases reach this revision
    # without the new column. Support both release starting points.
    op.execute(
        "ALTER TABLE llm_credentials "
        "ADD COLUMN IF NOT EXISTS plan_tier VARCHAR(20)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE llm_credentials "
        "DROP COLUMN IF EXISTS plan_tier"
    )
