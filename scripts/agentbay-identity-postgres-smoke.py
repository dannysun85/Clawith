#!/usr/bin/env python3
"""Prove AgentBay provider identity ownership under real PostgreSQL races."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, select

from app import database as database_module
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.agentbay_session import (
    AGENTBAY_PROVIDER_COLLISION_STATUS,
    AgentBaySessionLedger,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import agentbay_client


class _TwoPartyBarrier:
    def __init__(self) -> None:
        self._arrived = 0
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived == 2:
            self._ready.set()
        await self._ready.wait()


class _InitialReadBarrierSession:
    def __init__(self, db, barrier: _TwoPartyBarrier) -> None:
        self._db = db
        self._barrier = barrier
        self._first_execute = True

    async def execute(self, *args, **kwargs):
        result = await self._db.execute(*args, **kwargs)
        if self._first_execute:
            self._first_execute = False
            await self._barrier.wait()
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


class _BarrierContext:
    def __init__(self, context, barrier: _TwoPartyBarrier) -> None:
        self._context = context
        self._barrier = barrier

    async def __aenter__(self):
        db = await self._context.__aenter__()
        return _InitialReadBarrierSession(db, self._barrier)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._context.__aexit__(exc_type, exc, tb)


class _FirstTwoSessionsBarrierFactory:
    def __init__(self, factory) -> None:
        self._factory = factory
        self._barrier = _TwoPartyBarrier()
        self._calls = 0

    def __call__(self, *args, **kwargs):
        context = self._factory(*args, **kwargs)
        self._calls += 1
        if self._calls <= 2:
            return _BarrierContext(context, self._barrier)
        return context


async def _assert_collision_group(provider_session_id: str, expected_lanes: set[str]) -> None:
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(AgentBaySessionLedger)
                    .where(
                        (
                            AgentBaySessionLedger.provider_session_id
                            == provider_session_id
                        )
                        | (
                            AgentBaySessionLedger.context["provider_identity_collision_ledger_id"]
                            .as_string()
                            .is_not(None)
                        )
                    )
                    .order_by(AgentBaySessionLedger.id)
                )
            ).scalars().all()
        )
        # Filter the JSON-pointer arm back to this collision group. Another
        # smoke sequence can coexist in the same table.
        keeper = next(
            row for row in rows if row.provider_session_id == provider_session_id
        )
        rows = [
            row
            for row in rows
            if row.provider_session_id == provider_session_id
            or (row.context or {}).get("provider_identity_collision_ledger_id")
            == str(keeper.id)
        ]
        assert {row.chat_session_id for row in rows} == expected_lanes
        assert all(row.status == AGENTBAY_PROVIDER_COLLISION_STATUS for row in rows)
        assert sum(row.provider_session_id == provider_session_id for row in rows) == 1
        assert all(
            (row.context or {}).get("provider_identity_collision_ledger_id")
            == str(keeper.id)
            for row in rows
        )
        unresolved_claims = (
            await db.execute(
                select(func.count(AgentBaySessionLedger.id)).where(
                    AgentBaySessionLedger.provider_session_id
                    == provider_session_id
                )
            )
        ).scalar_one()
        assert unresolved_claims == 1


async def _force_initial_read_race(*operations):
    original_factory = database_module.async_session
    database_module.async_session = _FirstTwoSessionsBarrierFactory(original_factory)
    try:
        return await asyncio.gather(*operations)
    finally:
        database_module.async_session = original_factory


async def main() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with async_session() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="AgentBay Identity PostgreSQL Smoke",
                slug=f"agentbay-identity-{tenant_id.hex[:12]}",
                im_provider="web_only",
                is_active=True,
            )
        )
        await db.flush()
        db.add(
            User(
                id=user_id,
                display_name="AgentBay Identity Smoke",
                role="member",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                name="AgentBay Identity Smoke Agent",
                creator_id=user_id,
                tenant_id=tenant_id,
                status="idle",
            )
        )
        await db.commit()

    provider_create_race = f"provider-create-race-{uuid.uuid4()}"
    create_lanes = {str(uuid.uuid4()), str(uuid.uuid4())}
    provider_cleanup_race = f"provider-cleanup-race-{uuid.uuid4()}"
    cleanup_lanes = {str(uuid.uuid4()), str(uuid.uuid4())}

    try:
        create_results = await _force_initial_read_race(
            *[
                agentbay_client._record_agentbay_ledger(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    session_id=lane,
                    provider_session_id=provider_create_race,
                    image_type="browser",
                )
                for lane in create_lanes
            ]
        )
        assert sorted(create_results) == ["provider_collision", "recorded"]
        await _assert_collision_group(provider_create_race, create_lanes)

        cleanup_lane, create_lane = cleanup_lanes
        mixed_results = await _force_initial_read_race(
            agentbay_client._record_agentbay_cleanup_required(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                session_id=cleanup_lane,
                provider_session_id=provider_cleanup_race,
                image_type="code",
                reason="postgres_smoke_cleanup_race",
            ),
            agentbay_client._record_agentbay_ledger(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                session_id=create_lane,
                provider_session_id=provider_cleanup_race,
                image_type="code",
            ),
        )
        assert mixed_results[0] is None
        assert mixed_results[1] in {"recorded", "provider_collision"}
        await _assert_collision_group(provider_cleanup_race, cleanup_lanes)
        print("agentbay_identity_postgres_smoke=ok")
    finally:
        database_module.async_session = async_session
        async with async_session() as db:
            await db.execute(
                delete(AgentBaySessionLedger).where(
                    AgentBaySessionLedger.tenant_id == tenant_id
                )
            )
            await db.execute(delete(Agent).where(Agent.id == agent_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
