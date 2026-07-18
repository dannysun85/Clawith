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


def _load_called_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _load_llm_call_positional_arg_counts(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        len(node.args)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_call_llm_with_config"
    ]


def test_external_channels_unpack_routed_model_metadata():
    backend_root = Path(__file__).parents[1]
    checked = 0
    for relative_path in CHANNEL_FILES:
        assignments = _load_call_assignments(backend_root / relative_path)
        checked += len(assignments)
        assert assignments, f"No routed model load found in {relative_path}"
        assert set(assignments) == {4}, f"{relative_path} still unpacks the legacy 3-value contract"
    assert checked >= len(CHANNEL_FILES)


def test_external_channels_forward_routed_model_metadata():
    backend_root = Path(__file__).parents[1]
    checked = 0
    for relative_path in CHANNEL_FILES:
        arg_counts = _load_llm_call_positional_arg_counts(backend_root / relative_path)
        called_functions = _load_called_function_names(backend_root / relative_path)
        checked += 1
        if arg_counts:
            assert min(arg_counts) >= 6, f"{relative_path} drops routed model metadata"
        else:
            # v1.11 channel adapters hand the resolved model to Durable Runtime;
            # the Runtime snapshot then carries the tier/modality to the model
            # step.  Requiring a legacy direct LLM call here would defeat that
            # architecture and risk executing a message twice.
            assert "enqueue_channel_chat_runtime" in called_functions, (
                f"No routed Runtime or LLM call found in {relative_path}"
            )
    assert checked >= len(CHANNEL_FILES)


def test_wechat_uses_durable_runtime_instead_of_a_second_history_loop():
    backend_root = Path(__file__).parents[1]
    called_functions = _load_called_function_names(
        backend_root / "app/services/wechat_channel.py"
    )

    assert "enqueue_channel_chat_runtime" in called_functions
    assert "convert_chat_messages_to_llm_format" not in called_functions
