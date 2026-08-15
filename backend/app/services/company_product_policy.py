"""Company product defaults that have concrete runtime effects."""

from __future__ import annotations


DEFAULT_AGENT_AUTONOMY_POLICY: dict[str, str] = {
    "read_files": "L1",
    "write_workspace_files": "L2",
    "send_feishu_message": "L2",
    "send_external_message": "L3",
    "modify_soul": "L3",
    "access_business_system_read": "L2",
    "access_business_system_write": "L3",
    "delete_files": "L3",
    "create_calendar_event": "L2",
    "financial_operations": "L3",
}

APPROVAL_POLICY_OVERRIDES: dict[str, dict[str, str]] = {
    "high_risk": {},
    "external_actions": {
        "send_feishu_message": "L3",
        "send_external_message": "L3",
        "access_business_system_write": "L3",
        "create_calendar_event": "L3",
        "financial_operations": "L3",
    },
    "all_writes": {
        "write_workspace_files": "L3",
        "send_feishu_message": "L3",
        "send_external_message": "L3",
        "modify_soul": "L3",
        "access_business_system_write": "L3",
        "delete_files": "L3",
        "create_calendar_event": "L3",
        "financial_operations": "L3",
    },
}


def default_agent_autonomy_policy(policy: str | None) -> dict[str, str]:
    """Return a fresh autonomy policy for one newly created Agent."""

    normalized = policy if policy in APPROVAL_POLICY_OVERRIDES else "high_risk"
    return {
        **DEFAULT_AGENT_AUTONOMY_POLICY,
        **APPROVAL_POLICY_OVERRIDES[normalized],
    }


__all__ = ["APPROVAL_POLICY_OVERRIDES", "default_agent_autonomy_policy"]
