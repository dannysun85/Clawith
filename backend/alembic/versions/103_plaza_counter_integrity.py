"""Repair Plaza counters and enforce one like per author.

Revision ID: plaza_counter_integrity
Revises: relink_media_credit_reservations
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "plaza_counter_integrity"
down_revision: str | Sequence[str] | None = "relink_media_credit_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # Historical check-then-insert toggles could create several likes for the
    # same post/author. Keep the oldest evidence row deterministically before
    # installing the uniqueness fence.
    op.execute(
        sa.text(
            """
            WITH ranked_likes AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY post_id, author_id
                        ORDER BY created_at, id
                    ) AS like_rank
                FROM plaza_likes
            )
            DELETE FROM plaza_likes
            WHERE id IN (
                SELECT id FROM ranked_likes WHERE like_rank > 1
            )
            """
        )
    )
    # Rebuild both denormalized counters from their authoritative child rows.
    op.execute(
        sa.text(
            """
            UPDATE plaza_posts AS post
            SET likes_count = (
                    SELECT count(*)
                    FROM plaza_likes AS plaza_like
                    WHERE plaza_like.post_id = post.id
                ),
                comments_count = (
                    SELECT count(*)
                    FROM plaza_comments AS comment
                    WHERE comment.post_id = post.id
                )
            """
        )
    )

    if bind.dialect.name == "postgresql":
        # 001 creates current metadata on a fresh database, while production
        # upgrades arrive with the legacy automatically named foreign keys.
        # Drop every FK on the exact child column, then recreate the canonical
        # cascade constraint so both paths converge without duplicate DDL.
        op.execute(
            sa.text(
                """
                DO $$
                DECLARE constraint_name text;
                BEGIN
                  FOR constraint_name IN
                    SELECT pgc.conname
                    FROM pg_constraint AS pgc
                    JOIN pg_attribute AS attribute
                      ON attribute.attrelid = pgc.conrelid
                     AND attribute.attnum = ANY(pgc.conkey)
                    WHERE pgc.contype = 'f'
                      AND pgc.conrelid = 'plaza_likes'::regclass
                      AND attribute.attname = 'post_id'
                  LOOP
                    EXECUTE format(
                      'ALTER TABLE plaza_likes DROP CONSTRAINT %I',
                      constraint_name
                    );
                  END LOOP;
                  FOR constraint_name IN
                    SELECT pgc.conname
                    FROM pg_constraint AS pgc
                    JOIN pg_attribute AS attribute
                      ON attribute.attrelid = pgc.conrelid
                     AND attribute.attnum = ANY(pgc.conkey)
                    WHERE pgc.contype = 'f'
                      AND pgc.conrelid = 'plaza_comments'::regclass
                      AND attribute.attname = 'post_id'
                  LOOP
                    EXECUTE format(
                      'ALTER TABLE plaza_comments DROP CONSTRAINT %I',
                      constraint_name
                    );
                  END LOOP;
                END $$
                """
            )
        )
        op.create_foreign_key(
            "fk_plaza_likes_post_id_posts",
            "plaza_likes",
            "plaza_posts",
            ["post_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            "fk_plaza_comments_post_id_posts",
            "plaza_comments",
            "plaza_posts",
            ["post_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # A fresh 001 may already have this exact named constraint. PostgreSQL has
    # no ADD CONSTRAINT IF NOT EXISTS, so detect it through the inspector.
    unique_names = {
        item.get("name")
        for item in sa.inspect(bind).get_unique_constraints("plaza_likes")
    }
    if "uq_plaza_likes_post_author" not in unique_names:
        op.create_unique_constraint(
            "uq_plaza_likes_post_author",
            "plaza_likes",
            ["post_id", "author_id"],
        )


def downgrade() -> None:
    # The uniqueness/cascade fences and repaired counters are backward-readable
    # safety guarantees. Do not reintroduce duplicate likes or orphaned rows.
    pass
