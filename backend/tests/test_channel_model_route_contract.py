"""Keep every external-channel adapter aligned with the routed model contract."""

from __future__ import annotations

import ast
from pathlib import Path


CHANNEL_FILES = (
    "app/api/dingtalk.py",
    "app/api/discord_bot.py",
    "app/api/feishu.py",
    "app/api/slack.py",
    "app/api/teams.py",
    "app/api/wecom.py",
    "app/api/whatsapp.py",
    "app/services/discord_gateway.py",
    "app/services/wechat_channel.py",
    "app/services/wecom_stream.py",
)


def _load_call_assignments(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target_lengths: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Await):
            continue
        call = node.value.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Name) or func.id != "_load_agent_and_model":
            continue
        target = node.targets[0]
        if isinstance(target, (ast.Tuple, ast.List)):
            target_lengths.append(len(target.elts))
    return target_lengths


def test_external_channels_unpack_routed_model_metadata():
    backend_root = Path(__file__).parents[1]
    checked = 0
    for relative_path in CHANNEL_FILES:
        assignments = _load_call_assignments(backend_root / relative_path)
        checked += len(assignments)
        assert assignments, f"No routed model load found in {relative_path}"
        assert set(assignments) == {4}, f"{relative_path} still unpacks the legacy 3-value contract"
    assert checked >= len(CHANNEL_FILES)
