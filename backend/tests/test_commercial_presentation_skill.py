from pathlib import Path

import yaml

from app.services import agent_tools, skill_seeder
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.tool_capability_policy import EXPLICIT_GRANT_TOOL_NAMES


SKILL_FOLDER = "commercial-presentation"
PRESENTATION_TOOLS = {"convert_html_to_pptx", "convert_html_to_pdf"}


def test_commercial_presentation_skill_is_managed_and_role_scoped() -> None:
    skill = next(
        item
        for item in skill_seeder.BUILTIN_SKILLS
        if item["folder_name"] == SKILL_FOLDER
    )
    skill_path = (
        Path(__file__).parents[1]
        / "agent_template"
        / "skills"
        / SKILL_FOLDER
        / "SKILL.md"
    )

    assert skill["is_default"] is False
    assert skill_path.is_file()
    content = skill_path.read_text(encoding="utf-8")
    assert "This Skill guides presentation planning" in content
    assert "It does not" in content
    assert "`builtin.presentation.v1`" in content
    assert "Never choose or reveal a media" in content
    assert "Never present an outline, HTML file" in content
    assert "Do not invent an" in content
    assert "Treat a successful conversion as a candidate" in content


def test_reviewed_roles_receive_presentation_sop_without_broad_ambient_skill() -> None:
    templates_root = Path(__file__).parents[1] / "agent_templates"
    expected_roles = {
        "chief-of-staff",
        "content-creator",
        "douyin-operator",
        "growth-hacker",
        "product-manager",
        "project-manager",
    }

    for folder in expected_roles:
        metadata = yaml.safe_load(
            (templates_root / folder / "meta.yaml").read_text(encoding="utf-8")
        )
        assert SKILL_FOLDER in metadata["default_skills"]

    for folder in {"tiktok-strategist", "xiaohongshu-operator"}:
        metadata = yaml.safe_load(
            (templates_root / folder / "meta.yaml").read_text(encoding="utf-8")
        )
        assert SKILL_FOLDER not in metadata["default_skills"]


def test_private_assistant_is_the_provider_neutral_multimodal_entrypoint() -> None:
    metadata = yaml.safe_load(
        (
            Path(__file__).parents[1]
            / "agent_templates"
            / "private-assistant"
            / "meta.yaml"
        ).read_text(encoding="utf-8")
    )

    assert {
        "brand-safe-media",
        "commercial-presentation",
        "commercial-voiceover",
    } <= set(metadata["default_skills"])
    assert not any(
        "volcengine" in skill or "minimax" in skill
        for skill in metadata["default_skills"]
    )


def test_presentation_conversion_tools_are_product_defaults_with_typed_runtime() -> None:
    definitions = {
        item["name"]: item
        for item in BUILTIN_TOOL_DEFINITIONS
    }

    assert PRESENTATION_TOOLS.isdisjoint(EXPLICIT_GRANT_TOOL_NAMES)
    for name in PRESENTATION_TOOLS:
        assert definitions[name]["is_default"] is True
        assert name in agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES
