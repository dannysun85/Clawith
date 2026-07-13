import pytest

from app.services import agent_seeder


def test_default_agent_seed_order_respects_available_slots():
    assert agent_seeder._default_agent_names_for_available_slots(set(), 1) == ("Meeseeks",)
    assert agent_seeder._default_agent_names_for_available_slots({"Meeseeks"}, 1) == ("Morty",)
    assert agent_seeder._default_agent_names_for_available_slots(set(), 0) == ()
    assert agent_seeder._default_agent_names_for_available_slots(set(), 2) == ("Meeseeks", "Morty")


@pytest.mark.asyncio
async def test_default_agent_seeder_preserves_renamed_seeded_agents(monkeypatch):
    async def seeded_marker():
        return "morty=11111111-1111-1111-1111-111111111111\nmeeseeks=22222222-2222-2222-2222-222222222222\n"

    class ForbiddenSessionFactory:
        def __call__(self):
            raise AssertionError("database must not be queried after both defaults were seeded")

    monkeypatch.setattr(agent_seeder, "_read_seed_marker", seeded_marker)
    monkeypatch.setattr(agent_seeder, "async_session", ForbiddenSessionFactory())

    await agent_seeder.seed_default_agents()
