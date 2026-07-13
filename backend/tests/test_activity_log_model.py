"""Regression coverage for activity-log enum compatibility."""

from app.models.activity_log import AgentActivityLog


def test_activity_action_enum_supports_runtime_actions() -> None:
    enum_values = set(AgentActivityLog.__table__.c.action_type.type.enums)

    assert {
        "agent_file_sent",
        "agent_file_received",
        "oneshot_task",
    } <= enum_values
