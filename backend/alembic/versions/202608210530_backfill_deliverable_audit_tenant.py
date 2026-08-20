"""Backfill tenant scope for Deliverable audit rows.

Revision ID: backfill_deliv_audit_tenant
Revises: widen_creative_brief_schema
Create Date: 2026-08-21 05:30:00

Deliverable audit writers historically copied the tenant identifier into the
JSON details but omitted the indexed ``tenant_id`` column.  That made valid
approval and quality-review events disappear from tenant-scoped audit views.
Only rows whose stored identifier exactly matches an existing tenant are
repaired; malformed or unrelated audit rows remain untouched.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "backfill_deliv_audit_tenant"
down_revision: str | Sequence[str] | None = "widen_creative_brief_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE audit_logs AS audit
        SET tenant_id = tenant.id
        FROM tenants AS tenant
        WHERE audit.tenant_id IS NULL
          AND audit.action LIKE 'deliverable.%'
          AND audit.details ->> 'tenant_id' = tenant.id::text
        """
    )


def downgrade() -> None:
    # Tenant attribution is a correctness repair.  Removing the release must
    # not erase valid audit scope from historical rows.
    pass
