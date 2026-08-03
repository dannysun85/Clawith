from pathlib import Path

import yaml

from app.services import agent_tools, skill_seeder
from app.services.builtin_tool_definitions import BUILTIN_TOOL_DEFINITIONS
from app.services.tool_capability_policy import EXPLICIT_GRANT_TOOL_NAMES


SKILL_FOLDER = "commercial-voiceover"
VOICE_TOOLS = {"generate_speech_minimax", "compose_video_audio"}


def _template(folder: str) -> dict:
    root = Path(__file__).parents[1] / "agent_templates"
    return yaml.safe_load((root / folder / "meta.yaml").read_text(encoding="utf-8"))


def test_commercial_voiceover_skill_is_managed_and_role_scoped() -> None:
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
    assert "does not grant a Tool" in content
    assert "Omit `voice_id`" in content
    assert "Voice identifiers are Provider-specific" in content
    assert "Never retry manually after a" in content
    assert "Do not claim voice cloning" in content
    assert "Listen to the delivered file" in content
    assert "successful Provider response is not a" in content

    for folder in {"content-creator", "douyin-operator", "tiktok-strategist"}:
        assert SKILL_FOLDER in _template(folder)["default_skills"]

    for folder in {
        "chief-of-staff",
        "growth-hacker",
        "xiaohongshu-operator",
    }:
        assert SKILL_FOLDER not in _template(folder)["default_skills"]


def test_speech_tools_are_provider_neutral_product_defaults_with_typed_runtime() -> None:
    definitions = {item["name"]: item for item in BUILTIN_TOOL_DEFINITIONS}

    assert VOICE_TOOLS.isdisjoint(EXPLICIT_GRANT_TOOL_NAMES)
    assert definitions["generate_speech_minimax"]["display_name"] == "Generate Speech"
    speech_contract = definitions["generate_speech_minimax"]["description"]
    voice_help = definitions["generate_speech_minimax"]["parameters_schema"][
        "properties"
    ]["voice_id"]["description"]
    assert "managed media route" in speech_contract
    assert "eligible provider" in speech_contract
    assert "MiniMax credential pool" not in speech_contract
    assert "provider-specific voice identifier" in voice_help
    assert "never invent" in voice_help

    for name in VOICE_TOOLS:
        assert definitions[name]["is_default"] is True
        assert name in agent_tools.RUNTIME_TYPED_APPLICATION_TOOL_NAMES


def test_douyin_operator_has_complete_multimodal_method_and_tool_contract() -> None:
    metadata = _template("douyin-operator")

    assert {
        "content-writing",
        "brand-safe-media",
        "volcengine-seedream-commercial",
        "volcengine-seedance-commercial",
        "commercial-voiceover",
        "commercial-presentation",
    } <= set(metadata["default_skills"])
    assert {
        "generate_image_minimax",
        "generate_speech_minimax",
        "generate_video_minimax",
        "check_video_minimax",
        "compose_video_audio",
    } <= set(metadata["default_tools"])
