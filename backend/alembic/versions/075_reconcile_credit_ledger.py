"""Reconcile legacy credit balances with their audit ledger.

Revision ID: reconcile_credit_ledger
Revises: repair_retroactive_schema
Create Date: 2026-07-11

Early subscription builds could initialize a tenant's balance without writing
the matching credit transaction. Preserve the user-visible balance and add an
explicit migration adjustment so the append-only ledger becomes complete.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "reconcile_credit_ledger"
down_revision: Union[str, Sequence[str], None] = "repair_retroactive_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("credit_balances") or not _table_exists("credit_transactions"):
        return

    op.execute(
        """
        WITH ledger_totals AS (
            SELECT tenant_id, COALESCE(SUM(delta), 0)::integer AS total
            FROM credit_transactions
            GROUP BY tenant_id
        ), drift AS (
            SELECT
                balances.tenant_id,
                balances.balance,
                (balances.balance - COALESCE(ledger_totals.total, 0))::integer AS delta
            FROM credit_balances AS balances
            LEFT JOIN ledger_totals ON ledger_totals.tenant_id = balances.tenant_id
            WHERE balances.balance <> COALESCE(ledger_totals.total, 0)
        )
        INSERT INTO credit_transactions (
            id,
            tenant_id,
            delta,
            balance_after,
            reason,
            ref_type,
            ref_id
        )
        SELECT
            gen_random_uuid(),
            tenant_id,
            delta,
            balance,
            'adjust',
            'migration',
            '07500000-0000-4000-8000-000000000001'::uuid
        FROM drift
        WHERE delta <> 0
        """
    )


def downgrade() -> None:
    # Adjustment rows are part of the append-only audit trail. Removing them
    # would recreate ledger drift, so a downgrade intentionally retains them.
    pass
