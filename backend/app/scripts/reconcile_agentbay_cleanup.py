"""Strict maintenance-window reconciliation for AgentBay provider sessions.

This command is intentionally non-interactive and fail-closed. It only closes a
live or cleanup-required ledger row after the exact provider session was
attached and deletion returned success. Any ambiguous row blocks cutover.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os

from loguru import logger
from sqlalchemy import func, select

from app.database import async_session
from app.models.agentbay_session import AgentBaySessionLedger
from app.services.agentbay_client import _configured_agentbay_client


DEFAULT_RECONCILE_DEADLINE_SECONDS = 120
MAX_RECONCILE_DEADLINE_SECONDS = 600


def _reconcile_deadline_seconds() -> int:
    raw = os.getenv(
        "AGENTBAY_RECONCILE_DEADLINE_SECONDS",
        str(DEFAULT_RECONCILE_DEADLINE_SECONDS),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECONCILE_DEADLINE_SECONDS
    return min(MAX_RECONCILE_DEADLINE_SECONDS, max(1, value))


async def _reconcile_one(ledger_id) -> bool:
    async with async_session() as db:
        ledger = (
            await db.execute(
                select(AgentBaySessionLedger)
                .where(AgentBaySessionLedger.id == ledger_id)
            )
        ).scalar_one_or_none()
        if ledger is None or ledger.status not in {"active", "cleanup_required"}:
            return True
        if not ledger.agent_id or not ledger.provider_session_id:
            logger.error("AgentBay cleanup row lacks a verifiable provider binding")
            return False
        agent_id = ledger.agent_id
        provider_session_id = ledger.provider_session_id
        image_type = ledger.image_type
        await db.rollback()

    try:
        client, _tool_config = await _configured_agentbay_client(agent_id)
        await client.attach_session(provider_session_id, image_type)
        await client.delete_session_strict()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "AgentBay maintenance cleanup remains unconfirmed error_type={}",
            type(exc).__name__,
        )
        return False

    async with async_session() as db:
        ledger = (
            await db.execute(
                select(AgentBaySessionLedger)
                .where(AgentBaySessionLedger.id == ledger_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if ledger is None:
            logger.error("AgentBay cleanup row disappeared during reconciliation")
            return False
        if ledger.status not in {
            "active",
            "cleanup_required",
        } or ledger.provider_session_id != provider_session_id:
            logger.error("AgentBay cleanup row changed during reconciliation")
            return False
        ledger.status = "closed"
        ledger.close_reason = "release_maintenance_provider_delete_verified"
        ledger.error_message = None
        ledger.closed_at = datetime.now(timezone.utc)
        await db.commit()
        return True


async def main() -> int:
    async with async_session() as db:
        ids = list(
            (
                await db.execute(
                    select(AgentBaySessionLedger.id)
                    .where(
                        AgentBaySessionLedger.status.in_(
                            ["active", "cleanup_required"]
                        )
                    )
                    .order_by(AgentBaySessionLedger.id)
                )
            ).scalars().all()
        )

    all_confirmed = True
    try:
        async with asyncio.timeout(_reconcile_deadline_seconds()):
            for ledger_id in ids:
                all_confirmed = await _reconcile_one(ledger_id) and all_confirmed
    except TimeoutError:
        logger.error("AgentBay maintenance reconciliation exceeded its deadline")
        all_confirmed = False

    async with async_session() as db:
        remaining = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AgentBaySessionLedger)
                    .where(
                        AgentBaySessionLedger.status.in_(
                            ["active", "cleanup_required"]
                        )
                    )
                )
            ).scalar_one()
        )
    logger.info(
        "AgentBay maintenance reconciliation complete attempted={} remaining={}",
        len(ids),
        remaining,
    )
    return 0 if all_confirmed and remaining == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
