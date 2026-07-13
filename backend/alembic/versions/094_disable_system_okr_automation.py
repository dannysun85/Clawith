"""Disable platform-seeded OKR automation until explicitly re-enabled.

Revision ID: disable_system_okr_automation
Revises: seed_minimax_m3_understanding
Create Date: 2026-07-14

Tenant OKR settings are preserved. Only the platform-owned cron triggers and
their unfinished executions are stopped, so an operator can later opt in via
OKR_AUTOMATION_ENABLED without reconstructing tenant data.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "disable_system_okr_automation"
down_revision: str | Sequence[str] | None = "seed_minimax_m3_understanding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OKR_TRIGGER_NAMES = (
    "daily_okr_collection",
    "daily_okr_report",
    "weekly_okr_report",
    "biweekly_okr_checkin",
    "monthly_okr_report",
)


def _quoted_names() -> str:
    return ", ".join(f"'{name}'" for name in OKR_TRIGGER_NAMES)


def upgrade() -> None:
    names = _quoted_names()
    bind = op.get_bind()
    bind.exec_driver_sql(
        f"""
        UPDATE agent_triggers
        SET is_enabled = false
        WHERE is_system = true
          AND name IN ({names})
        """
    )
    bind.exec_driver_sql(
        f"""
        UPDATE trigger_executions AS execution
        SET status = 'failed',
            finished_at = COALESCE(execution.finished_at, now()),
            lease_owner = NULL,
            lease_expires_at = NULL,
            last_error = 'Disabled by platform OKR automation safety switch'
        FROM agent_triggers AS trigger
        WHERE execution.trigger_id = trigger.id
          AND trigger.is_system = true
          AND trigger.name IN ({names})
          AND execution.status IN ('pending', 'processing')
        """
    )


def downgrade() -> None:
    # Never restart token-consuming automation as a side effect of rollback.
    # Tenant settings remain untouched and an operator may re-enable it later.
    pass
