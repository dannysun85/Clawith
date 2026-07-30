"""Converge the Astra deliverables and upstream v1.11.3 release heads.

Revision ID: merge_v1113_astra_heads
Revises: add_deliverable_quality_reviews, allow_checkpoint_deliveries
Create Date: 2026-07-31 12:00:00

Both parents contain already-released additive schema changes.  This merge
revision deliberately performs no DDL: it records that an installation must
apply both the Astra commercial-deliverables lineage and the upstream
checkpoint-delivery/logical-deletion lineage before it reaches the release
head.
"""

from collections.abc import Sequence


revision: str = "merge_v1113_astra_heads"
down_revision: tuple[str, str] = (
    "add_deliverable_quality_reviews",
    "allow_checkpoint_deliveries",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
