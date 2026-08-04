import uuid
from pathlib import Path

import pytest
import yaml

from app.services import agent_tools, media_generation, skill_seeder, tool_seeder
from app.services.media_assets import MediaContractError
from app.services.media_tool_registry import MEDIA_ARTIFACT_TOOL_NAMES


IMAGE_TOOL_NAMES = {
    "generate_image_siliconflow",
    "generate_image_openai",
    "generate_image_google",
    "generate_image_custom",
    "generate_image_minimax",
}
BRAND_SAFE_IMAGE_FIELDS = {
    "overlay_text",
    "overlay_position",
    "brand_asset",
    "brand_position",
    "brand_scale",
}


def _runtime_tool(name: str) -> dict:
    return next(item for item in agent_tools.AGENT_TOOLS if item["function"]["name"] == name)


def _seeded_tool(name: str) -> dict:
    return next(item for item in tool_seeder.BUILTIN_TOOLS if item["name"] == name)


def test_every_image_provider_exposes_the_same_brand_safe_contract():
    for name in IMAGE_TOOL_NAMES:
        runtime_properties = _runtime_tool(name)["function"]["parameters"]["properties"]
        seeded_properties = _seeded_tool(name)["parameters_schema"]["properties"]
        assert BRAND_SAFE_IMAGE_FIELDS <= runtime_properties.keys()
        assert BRAND_SAFE_IMAGE_FIELDS <= seeded_properties.keys()
    for properties in (
        _runtime_tool("generate_image_minimax")["function"]["parameters"]["properties"],
        _seeded_tool("generate_image_minimax")["parameters_schema"]["properties"],
    ):
        reference_help = properties["reference_image"]["description"]
        assert "creative" in reference_help
        assert "may redraw" in reference_help
        assert "public URL" not in reference_help


def test_media_artifact_registry_covers_every_seeded_media_producer():
    seeded_media_producers = {
        item["name"]
        for item in tool_seeder.BUILTIN_TOOLS
        if item.get("category") == "media"
        and (
            str(item.get("name") or "").startswith("generate_")
            or item.get("name") == "check_video_minimax"
        )
    }

    assert MEDIA_ARTIFACT_TOOL_NAMES == seeded_media_producers


def test_video_provider_exposes_exact_copy_and_protected_product_contract():
    required = BRAND_SAFE_IMAGE_FIELDS
    runtime_properties = _runtime_tool("generate_video_minimax")["function"]["parameters"]["properties"]
    seeded_properties = _seeded_tool("generate_video_minimax")["parameters_schema"]["properties"]

    assert required <= runtime_properties.keys()
    assert required <= seeded_properties.keys()
    for properties in (runtime_properties, seeded_properties):
        for field in ("first_frame_image", "last_frame_image"):
            frame_help = properties[field]["description"]
            assert "creative" in frame_help
            assert "may redraw" in frame_help
            assert "preserve" not in frame_help.lower()
            assert "public URL" not in frame_help


def test_brand_safe_media_skill_is_role_scoped_with_explicit_media_grants():
    skill = next(item for item in skill_seeder.BUILTIN_SKILLS if item["folder_name"] == "brand-safe-media")
    skill_path = (
        Path(__file__).parents[1]
        / "agent_template"
        / "skills"
        / "brand-safe-media"
        / "SKILL.md"
    )

    assert skill["is_default"] is False
    assert skill_path.is_file()
    content = skill_path.read_text(encoding="utf-8")
    assert "Do not make the user complete a production form" in content
    assert "Put the exact visible copy in `overlay_text`" in content
    assert "Never use the static product-layer workaround for outcome 2" in content
    assert "Do not blur or soften the scene by default" in content
    assert "If the selected provider cannot accept the reference frame, stop" in content
    assert "background_sanitized=true" in content
    assert "Skills guide the workflow; the native media tools enforce" in content

    templates_root = Path(__file__).parents[1] / "agent_templates"
    for folder in (
        "content-creator",
        "douyin-operator",
        "tiktok-strategist",
        "linkedin-content-creator",
        "growth-hacker",
        "xiaohongshu-operator",
    ):
        metadata = yaml.safe_load((templates_root / folder / "meta.yaml").read_text())
        assert "brand-safe-media" in metadata["default_skills"]
        assert metadata["default_tools"]


def test_douyin_operator_receives_managed_volcengine_commercial_skills():
    templates_root = Path(__file__).parents[1] / "agent_templates"
    metadata = yaml.safe_load(
        (templates_root / "douyin-operator" / "meta.yaml").read_text()
    )
    expected = {
        "volcengine-seedream-commercial",
        "volcengine-seedance-commercial",
    }

    assert expected <= set(metadata["default_skills"])
    assert {
        "generate_image_minimax",
        "generate_video_minimax",
        "check_video_minimax",
        "generate_speech_minimax",
        "compose_video_audio",
    } <= set(metadata["default_tools"])

    registry = {
        skill["folder_name"]: skill
        for skill in skill_seeder.BUILTIN_SKILLS
    }
    for folder in expected:
        assert registry[folder]["is_default"] is False
        skill_root = (
            Path(__file__).parents[1]
            / "agent_template"
            / "skills"
            / folder
        )
        content = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        assert "This Skill" in content
        assert "does not grant" in content
        assert "Never" in content
        assert (
            skill_root / "references" / "official-agent-plan-contract.md"
        ).is_file()
    seedance_content = (
        templates_root.parent
        / "agent_template"
        / "skills"
        / "volcengine-seedance-commercial"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "Seedance 1.5 Pro legacy-task compatibility" in seedance_content
    assert "Large / Max plans to Seedance 2.0" in seedance_content
    assert "`require_audio=false` is the default product contract" in seedance_content


def test_video_brand_asset_is_frozen_outside_agent_workspace():
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    key = media_generation.minimax_video_brand_asset_key(agent_id, task_id, "png")

    assert key == f"_internal/provider_recovery/minimax/video/{agent_id}/{task_id}/brand.png"
    assert "workspace/" not in key


@pytest.mark.asyncio
async def test_unrenderable_exact_copy_is_rejected_before_any_paid_image_call(tmp_path, monkeypatch):
    def reject_copy(_text):
        raise MediaContractError("missing glyph")

    monkeypatch.setattr("app.services.media_assets.validate_overlay_text", reject_copy)
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        lambda *_args, **_kwargs: pytest.fail("provider config must not be loaded"),
    )

    result = await agent_tools._generate_image(
        uuid.uuid4(),
        tmp_path,
        {"prompt": "clean blue background", "overlay_text": "unsupported"},
        "minimax",
    )

    assert "missing glyph" in result
