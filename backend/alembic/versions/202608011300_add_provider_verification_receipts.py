"""Persist explicit provider credential verification receipts.

Revision ID: provider_verification_receipts
Revises: okr_evidence_links
Create Date: 2026-08-01 13:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "provider_verification_receipts"
down_revision: str | Sequence[str] | None = "okr_evidence_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("llm_credentials")
    }
    if "last_verification_at" not in columns:
        op.add_column(
            "llm_credentials",
            sa.Column(
                "last_verification_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if "verification_receipt" not in columns:
        op.add_column(
            "llm_credentials",
            sa.Column(
                "verification_receipt",
                sa.JSON(),
                nullable=True,
            ),
        )

    # Do not synthesize receipts for legacy ``healthy`` rows.  They remain
    # configured but must be explicitly re-verified before the new media
    # readiness view can call them account-verified.


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("llm_credentials")
    }
    if "verification_receipt" in columns:
        op.drop_column("llm_credentials", "verification_receipt")
    if "last_verification_at" in columns:
        op.drop_column("llm_credentials", "last_verification_at")
