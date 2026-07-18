from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.scripts import setup_langgraph_checkpoints


@pytest.mark.asyncio
async def test_setup_uses_the_pinned_saver_migration_ledger(monkeypatch) -> None:
    saver = type("Saver", (), {"setup": AsyncMock()})()
    settings = Settings(DATABASE_URL="postgresql+asyncpg://app:secret@db/clawith")
    received = []

    @asynccontextmanager
    async def fake_checkpointer(actual_settings):
        received.append(actual_settings)
        yield saver

    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "create_checkpointer",
        fake_checkpointer,
    )

    await setup_langgraph_checkpoints.setup_checkpoint_tables(settings)

    assert received == [settings]
    saver.setup.assert_awaited_once_with()


def test_main_runs_the_explicit_async_setup(monkeypatch) -> None:
    calls = []

    async def fake_setup() -> None:
        calls.append("setup")

    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "setup_checkpoint_tables",
        fake_setup,
    )

    setup_langgraph_checkpoints.main()

    assert calls == ["setup"]
