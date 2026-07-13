"""Disable legacy webhook triggers that do not have an HMAC secret.

Revision ID: disable_unsigned_webhooks
Revises: reconcile_credit_ledger
Create Date: 2026-07-11

URL tokens are not a sufficient trust boundary because they commonly leak
through proxy logs and third-party configuration. New webhook triggers always
receive an independent signing secret; this migration contains older unsafe
triggers until an administrator rotates their configuration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "disable_unsigned_webhooks"
down_revision: Union[str, Sequence[str], None] = "reconcile_credit_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "agent_triggers" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.execute(
        """
        UPDATE agent_triggers
        SET is_enabled = false
        WHERE type = 'webhook'
          AND is_enabled = true
          AND NULLIF(BTRIM(config ->> 'secret'), '') IS NULL
        """
    )


def downgrade() -> None:
    # Re-enabling previously unsigned automation endpoints would recreate the
    # vulnerability, so containment is intentionally retained on downgrade.
    pass
