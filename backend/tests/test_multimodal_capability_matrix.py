from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from app.services.agent_tools import RUNTIME_TYPED_APPLICATION_TOOL_NAMES
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.multimodal_capability_matrix import (
    CAPABILITY_MATRIX,
    validate_capability_matrix,
)
from app.services.skill_seeder import BUILTIN_SKILLS
from app.services.template_seeder import _TEMPLATE_ROOT
from app.services.agent_template_contract import load_agent_template_manifest


def _registries() -> tuple[dict[str, dict[str, object]], set[str], dict[str, dict[str, object]], set[str]]:
    templates: dict[str, dict[str, object]] = {}
    for slug_dir in sorted(path for path in _TEMPLATE_ROOT.iterdir() if path.is_dir()):
        manifest = load_agent_template_manifest(slug_dir)
        templates[slug_dir.name] = {
            "default_skills": list(manifest.default_skills),
            "default_tools": list(manifest.default_tools),
        }
    definitions = {
        str(tool["name"]): deepcopy(tool) for tool in BUILTIN_TOOL_DEFINITIONS
    }
    return (
        templates,
        {str(skill["folder_name"]) for skill in BUILTIN_SKILLS},
        definitions,
        set(RUNTIME_TYPED_APPLICATION_TOOL_NAMES),
    )


def test_current_multimodal_matrix_is_ready_without_provider_calls() -> None:
    templates, skills, definitions, typed = _registries()

    report = validate_capability_matrix(
        templates=templates,
        known_skills=skills,
        tool_definitions=definitions,
        runtime_typed_tools=typed,
    )

    assert report.ready
    assert report.errors == ()
    assert all(row["ready"] for row in report.rows)
    assert {row["key"] for row in report.rows} == {
        "text",
        "image",
        "video",
        "voice",
        "music",
        "presentation",
    }
    assert report.as_dict()["provider_health_verified"] is False


def test_entrypoint_cannot_claim_media_when_default_tool_is_removed() -> None:
    templates, skills, definitions, typed = _registries()
    definitions["generate_image_minimax"]["is_default"] = False

    report = validate_capability_matrix(
        templates=templates,
        known_skills=skills,
        tool_definitions=definitions,
        runtime_typed_tools=typed,
    )

    assert not report.ready
    assert any("image: entrypoint 'private-assistant' cannot see tool" in item for item in report.errors)


def test_missing_provider_skill_is_fail_closed() -> None:
    templates, skills, definitions, typed = _registries()
    skills.remove("volcengine-seedance-commercial")

    report = validate_capability_matrix(
        templates=templates,
        known_skills=skills,
        tool_definitions=definitions,
        runtime_typed_tools=typed,
    )

    assert not report.ready
    assert any("volcengine-seedance-commercial" in item for item in report.errors)


def test_explicit_music_tool_must_be_granted_to_a_specialist() -> None:
    templates, skills, definitions, typed = _registries()
    templates["content-creator"]["default_tools"].remove("generate_music_minimax")

    report = validate_capability_matrix(
        templates=templates,
        known_skills=skills,
        tool_definitions=definitions,
        runtime_typed_tools=typed,
    )

    assert not report.ready
    assert any("music: specialist 'content-creator' lacks authorized tool" in item for item in report.errors)


def test_music_cannot_become_an_ambient_default() -> None:
    templates, skills, definitions, typed = _registries()
    definitions["generate_music_minimax"]["is_default"] = True

    report = validate_capability_matrix(
        templates=templates,
        known_skills=skills,
        tool_definitions=definitions,
        runtime_typed_tools=typed,
    )

    assert not report.ready
    assert any("music: explicit tool 'generate_music_minimax' cannot be a product default" in item for item in report.errors)


def test_presentation_requires_typed_conversion_adapters() -> None:
    templates, skills, definitions, typed = _registries()
    typed.remove("convert_html_to_pptx")

    report = validate_capability_matrix(
        templates=templates,
        known_skills=skills,
        tool_definitions=definitions,
        runtime_typed_tools=typed,
    )

    assert not report.ready
    assert any(
        "convert_html_to_pptx" in item and "no typed runtime adapter" in item
        for item in report.errors
    )


def test_text_route_policy_keeps_provider_selection_internal() -> None:
    text = next(spec for spec in CAPABILITY_MATRIX if spec.key == "text")

    assert "MiniMax-M3" in text.route_policy
    assert "Agent Plan" in text.route_policy
    assert text.entrypoint_template == "private-assistant"


def test_json_cli_emits_parseable_stdout_without_import_diagnostics() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    script = backend_root / "scripts" / "validate_multimodal_capability_matrix.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--json"],
        cwd=backend_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "ready"
    assert completed.stdout.lstrip().startswith("{")
