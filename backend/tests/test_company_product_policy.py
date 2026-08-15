"""Concrete runtime contracts for first-company product policy."""

from app.services.company_product_policy import default_agent_autonomy_policy


def test_default_approval_policies_map_to_distinct_runtime_enforcement() -> None:
    high_risk = default_agent_autonomy_policy("high_risk")
    external = default_agent_autonomy_policy("external_actions")
    all_writes = default_agent_autonomy_policy("all_writes")

    assert high_risk["write_workspace_files"] == "L2"
    assert high_risk["send_external_message"] == "L3"
    assert external["write_workspace_files"] == "L2"
    assert external["create_calendar_event"] == "L3"
    assert all_writes["write_workspace_files"] == "L3"
    assert all_writes["access_business_system_write"] == "L3"


def test_unknown_policy_fails_closed_to_high_risk_and_returns_fresh_mapping() -> None:
    first = default_agent_autonomy_policy("unsupported")
    second = default_agent_autonomy_policy(None)

    assert first == second
    first["write_workspace_files"] = "L3"
    assert second["write_workspace_files"] == "L2"
