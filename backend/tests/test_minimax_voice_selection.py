from __future__ import annotations

from app.services.agent_tools import _minimax_default_voice_id
from app.services import tool_seeder


def test_auto_voice_selection_matches_the_script_language():
    assert (
        _minimax_default_voice_id("忙，也要喝口热的。")
        == "Chinese (Mandarin)_Warm_Bestie"
    )
    assert (
        _minimax_default_voice_id("Stay warm, even on the busiest day.")
        == "English_expressive_narrator"
    )


def test_speech_tool_exposes_auto_as_the_global_default():
    definition = next(
        tool
        for tool in tool_seeder.BUILTIN_TOOLS
        if tool["name"] == "generate_speech_minimax"
    )

    assert definition["config"]["voice_id"] == "auto"
    voice_field = next(
        field
        for field in definition["config_schema"]["fields"]
        if field["key"] == "voice_id"
    )
    assert voice_field["default"] == "auto"
