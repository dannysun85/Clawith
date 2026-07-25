"""Reviewed assignment policy for non-ambient builtin Tools.

Product-default Tools are available through the normal visibility policy.
Every explicit-grant Tool must instead be either referenced by at least one
Agent role template or listed here with a narrow reason why assignment is
manual/system-owned.  This makes a newly added explicit Tool fail CI until its
Agent routing decision is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from app.services.tool_capability_policy import EXPLICIT_GRANT_TOOL_NAMES


EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS: Mapping[str, str] = {
    "agentbay_code_edit_file": "AgentBay provider selection is configured per Agent",
    "agentbay_code_execute": "AgentBay provider selection is configured per Agent",
    "agentbay_code_read_file": "AgentBay provider selection is configured per Agent",
    "agentbay_code_write_file": "AgentBay provider selection is configured per Agent",
    "agentbay_command_exec": "AgentBay provider selection is configured per Agent",
    "execute_code_e2b": "sandbox provider selection is configured per Agent",
    "install_skill": "capability installation is an explicit Agent manager action",
    "update_kr_content": "reserved for the tenant OKR system Agent",
    "update_kr_progress": "reserved for the tenant OKR system Agent",
    "upload_image": "user upload is initiated from an interactive product flow",
}


@dataclass(frozen=True)
class ToolAssignmentGovernanceReport:
    role_scoped: tuple[str, ...]
    manual_or_system_scoped: tuple[str, ...]
    global_default: tuple[str, ...]


def validate_reviewed_tool_assignments(
    templates: Iterable[Mapping[str, object]],
    *,
    canonical_default_by_name: Mapping[str, bool],
) -> ToolAssignmentGovernanceReport:
    """Validate that every Tool has an intentional assignment path."""
    referenced = {
        name
        for template in templates
        for name in template.get("default_tools", [])
        if isinstance(name, str) and name.strip()
    }
    canonical_names = set(canonical_default_by_name)
    unknown_references = sorted(referenced - canonical_names)
    if unknown_references:
        raise ValueError(f"Agent templates reference unknown Tools: {unknown_references}")

    role_scoped = set(EXPLICIT_GRANT_TOOL_NAMES) & referenced
    exempt = set(EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS)
    missing_decisions = sorted(set(EXPLICIT_GRANT_TOOL_NAMES) - role_scoped - exempt)
    stale_exemptions = sorted(exempt - (set(EXPLICIT_GRANT_TOOL_NAMES) - role_scoped))
    if missing_decisions or stale_exemptions:
        details: list[str] = []
        if missing_decisions:
            details.append(
                "explicit Tools without Agent template or reviewed exemption="
                f"{missing_decisions}"
            )
        if stale_exemptions:
            details.append(f"stale explicit Tool exemptions={stale_exemptions}")
        raise ValueError("; ".join(details))

    global_default = {
        name for name, is_default in canonical_default_by_name.items() if is_default
    }
    return ToolAssignmentGovernanceReport(
        role_scoped=tuple(sorted(role_scoped)),
        manual_or_system_scoped=tuple(sorted(exempt)),
        global_default=tuple(sorted(global_default)),
    )


__all__ = [
    "EXPLICIT_TOOL_ASSIGNMENT_EXEMPTIONS",
    "ToolAssignmentGovernanceReport",
    "validate_reviewed_tool_assignments",
]
