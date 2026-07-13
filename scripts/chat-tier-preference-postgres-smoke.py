#!/usr/bin/env python3
"""Prove chat-tier CAS refreshes a stale SQLAlchemy identity-map row."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete

from app.api.chat_sessions import _lock_user_chat_preference
from app.database import async_session
from app.models.tenant import Tenant  # noqa: F401 - register FK target metadata
from app.models.user import User


TENANT_ID = uuid.UUID("07500000-0000-4000-8000-000000000002")


async def main() -> None:
    user_id = uuid.uuid4()
    try:
        async with async_session() as seed:
            seed.add(User(
                id=user_id,
                tenant_id=TENANT_ID,
                display_name="Chat tier CAS smoke",
                role="member",
                is_active=True,
                registration_source="migration-smoke",
                preferred_chat_tier="lite",
                preferred_chat_tier_revision=0,
            ))
            await seed.commit()

        async with async_session() as first, async_session() as stale:
            first_user = await first.get(User, user_id)
            stale_user = await stale.get(User, user_id)
            assert first_user is not None and stale_user is not None
            assert first_user.preferred_chat_tier_revision == 0
            assert stale_user.preferred_chat_tier_revision == 0

            locked_first = await _lock_user_chat_preference(first, user_id)
            assert locked_first is first_user
            locked_first.preferred_chat_tier = "ultra"
            locked_first.preferred_chat_tier_revision = 1
            await first.commit()

            locked_stale = await _lock_user_chat_preference(stale, user_id)
            assert locked_stale is stale_user
            assert locked_stale.preferred_chat_tier == "ultra"
            assert locked_stale.preferred_chat_tier_revision == 1
            incoming_revision = 0
            assert incoming_revision != locked_stale.preferred_chat_tier_revision
            await stale.rollback()

        async with async_session() as valid:
            locked_valid = await _lock_user_chat_preference(valid, user_id)
            assert locked_valid is not None
            assert locked_valid.preferred_chat_tier_revision == 1
            locked_valid.preferred_chat_tier = "pro"
            locked_valid.preferred_chat_tier_revision = 2
            await valid.commit()

        async with async_session() as verify:
            stored = await verify.get(User, user_id)
            assert stored is not None
            assert stored.preferred_chat_tier == "pro"
            assert stored.preferred_chat_tier_revision == 2
    finally:
        async with async_session() as cleanup:
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()

    print("Chat tier preference PostgreSQL stale-identity CAS smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
