from types import SimpleNamespace
import uuid

from app.services import agent_seeder


def test_default_agent_seed_order_respects_available_slots():
    assert agent_seeder._default_agent_names_for_available_slots(set(), 1) == ("Meeseeks",)
    assert agent_seeder._default_agent_names_for_available_slots({"Meeseeks"}, 1) == ("Morty",)
    assert agent_seeder._default_agent_names_for_available_slots(set(), 0) == ()
    assert agent_seeder._default_agent_names_for_available_slots(set(), 2) == ("Meeseeks", "Morty")


def test_default_agent_seeder_preserves_renamed_seeded_agent_identities():
    morty_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    meeseeks_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    marker = agent_seeder._parse_default_agent_marker(
        f"morty={morty_id}\nmeeseeks={meeseeks_id}\n"
    )
    agents = [
        SimpleNamespace(id=morty_id, name="Research Lead"),
        SimpleNamespace(id=meeseeks_id, name="Delivery Lead"),
    ]

    resolved = agent_seeder._resolve_default_agent_slots(agents, marker)

    assert resolved["Morty"] is agents[0]
    assert resolved["Meeseeks"] is agents[1]
    assert set(resolved) == {"Morty", "Meeseeks"}
