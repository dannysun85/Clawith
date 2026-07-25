#!/usr/bin/env python3
"""Validate Agent role, Skill, Tool, and runtime adapter alignment."""

from __future__ import annotations

from app.services.agent_template_contract import (
    load_agent_template_manifest,
    validate_template_capability_references,
)
from app.services.agent_tools import RUNTIME_TYPED_APPLICATION_TOOL_NAMES
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.skill_seeder import BUILTIN_SKILLS
from app.services.template_seeder import _TEMPLATE_ROOT
from app.services.tool_assignment_governance import validate_reviewed_tool_assignments


def main() -> int:
    known_skills = {skill["folder_name"] for skill in BUILTIN_SKILLS}
    canonical = {tool["name"]: bool(tool["is_default"]) for tool in BUILTIN_TOOL_DEFINITIONS}
    seed_templates: list[dict[str, object]] = []
    version_counts: dict[int, int] = {}

    for slug_dir in sorted(path for path in _TEMPLATE_ROOT.iterdir() if path.is_dir()):
        manifest = load_agent_template_manifest(slug_dir)
        validate_template_capability_references(
            manifest,
            known_skill_folders=known_skills,
            known_tool_names=set(canonical),
            runtime_typed_tool_names=RUNTIME_TYPED_APPLICATION_TOOL_NAMES,
        )
        version_counts[manifest.schema_version] = (
            version_counts.get(manifest.schema_version, 0) + 1
        )
        seed_templates.append(manifest.to_seed_dict(soul_template="validated"))

    report = validate_reviewed_tool_assignments(
        seed_templates,
        canonical_default_by_name=canonical,
    )
    print(
        "Agent capability contracts valid: "
        f"templates={len(seed_templates)} versions={version_counts} "
        f"skills={len(known_skills)} tools={len(canonical)} "
        f"runtime_typed={len(RUNTIME_TYPED_APPLICATION_TOOL_NAMES)} "
        f"role_scoped={len(report.role_scoped)} "
        f"manual_or_system={len(report.manual_or_system_scoped)} "
        f"global_default={len(report.global_default)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
