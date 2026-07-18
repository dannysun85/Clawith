"""Install or upgrade tables owned by the pinned LangGraph checkpointer."""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.services.agent_runtime.checkpointer import create_checkpointer


async def setup_checkpoint_tables(settings: Settings | None = None) -> None:
    """Run the upstream idempotent migration ledger inside its isolated schema.

    Alembic creates ``langgraph_checkpoint`` first. This explicit deployment
    step then lets the pinned saver version create or upgrade only its own
    tables. Application startup intentionally does not run checkpoint DDL.
    """
    async with create_checkpointer(settings) as checkpointer:
        await checkpointer.setup()


def main() -> None:
    asyncio.run(setup_checkpoint_tables())


if __name__ == "__main__":
    main()
