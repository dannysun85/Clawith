"""Widen creative brief schema-version identifiers.

Revision ID: widen_creative_brief_schema
Revises: ceo_coordination_mode
Create Date: 2026-08-21 03:00:00

The v2 presentation contract persists ``presentation-brief-v1`` (21
characters), while the original receipt table allowed only 16.  This
expand-only migration keeps existing rows intact and lets every registered v2
brief identifier use the same durable receipt path.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "widen_creative_brief_schema"
down_revision: str | Sequence[str] | None = "ceo_coordination_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "deliverable_creative_briefs",
        "schema_version",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "deliverable_creative_briefs",
        "schema_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
