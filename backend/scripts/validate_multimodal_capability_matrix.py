#!/usr/bin/env python3
"""Validate the local text/image/video/voice/music/PPT capability matrix."""

from __future__ import annotations

import argparse
import json

from app.services.agent_template_contract import load_agent_template_manifest
from app.services.agent_tools import RUNTIME_TYPED_APPLICATION_TOOL_NAMES
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.multimodal_capability_matrix import validate_capability_matrix
from app.services.media_provider_routing import validate_media_route_policy
from app.services.skill_seeder import BUILTIN_SKILLS
from app.services.template_seeder import _TEMPLATE_ROOT


def _load_templates() -> dict[str, dict[str, object]]:
    templates: dict[str, dict[str, object]] = {}
    for slug_dir in sorted(path for path in _TEMPLATE_ROOT.iterdir() if path.is_dir()):
        manifest = load_agent_template_manifest(slug_dir)
        templates[slug_dir.name] = {
            "default_skills": list(manifest.default_skills),
            "default_tools": list(manifest.default_tools),
        }
    return templates


def build_report() -> dict[str, object]:
    report = validate_capability_matrix(
        templates=_load_templates(),
        known_skills={skill["folder_name"] for skill in BUILTIN_SKILLS},
        tool_definitions={
            str(tool["name"]): tool for tool in BUILTIN_TOOL_DEFINITIONS
        },
        runtime_typed_tools=set(RUNTIME_TYPED_APPLICATION_TOOL_NAMES),
    )
    result = report.as_dict()
    route_errors = validate_media_route_policy()
    result["route_policy_verified"] = not route_errors
    result["route_policy_errors"] = list(route_errors)
    if route_errors:
        result["status"] = "invalid"
        result["errors"].extend(
            f"media route policy: {error}" for error in route_errors
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate provider-free multimodal registration, typed adapters, "
            "Agent grants, and the reviewed provider/model route policy. "
            "This does not call a Provider."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the complete machine-readable report",
    )
    args = parser.parse_args()

    result = build_report()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Multimodal capability matrix "
            f"{result['status']}: "
            + ", ".join(
                f"{row['key']}={'ready' if row['ready'] else 'invalid'}"
                for row in result["rows"]
            )
        )
        if result["errors"]:
            for error in result["errors"]:
                print(f"- {error}")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
