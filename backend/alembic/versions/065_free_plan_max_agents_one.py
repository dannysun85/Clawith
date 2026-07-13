"""Set Free plan max_agents to one.

Revision ID: free_plan_max_agents_one
Revises: subscription_mvp_phase1
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "free_plan_max_agents_one"
down_revision: Union[str, Sequence[str], None] = "subscription_mvp_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE plans SET max_agents = 1 WHERE code = 'free'")


def downgrade() -> None:
    op.execute("UPDATE plans SET max_agents = 2 WHERE code = 'free'")
