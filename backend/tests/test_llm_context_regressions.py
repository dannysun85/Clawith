import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.enterprise import _validate_provider_token_limit
from app.services.llm.caller import (
    RouteMeta,
    _build_ordered_api_messages,
    _check_tool_requires_args,
    _is_llm_error_result,
    _process_tool_call,
)
from app.services.llm.client import get_max_tokens
from app.services.llm.utils import (
    convert_chat_messages_to_llm_format,
    truncate_messages_with_pair_integrity,
)


def test_persisted_tool_call_is_restored_as_assistant_tool_pair():
    message_id = uuid.uuid4()
    messages = [
        SimpleNamespace(role="user", content="inspect the file", id=uuid.uuid4(), thinking=None),
        SimpleNamespace(
            role="tool_call",
            id=message_id,
            thinking=None,
            content=json.dumps({
                "name": "read_file",
                "args": {"path": "workspace/report.md"},
                "result": "quarterly result",
                "reasoning_content": "Need the source data",
            }),
        ),
        SimpleNamespace(role="assistant", content="The result is available.", id=uuid.uuid4(), thinking=None),
    ]

    converted = convert_chat_messages_to_llm_format(messages)

    call_id = f"call_{message_id}"
    assert converted[1]["role"] == "assistant"
    assert converted[1]["tool_calls"][0]["id"] == call_id
    assert converted[1]["reasoning_content"] == "Need the source data"
    assert converted[2] == {
        "role": "tool",
        "tool_call_id": call_id,
        "content": "quarterly result",
    }


def test_context_truncation_drops_orphans_but_preserves_complete_tool_pair():
    messages = [
        {"role": "tool", "tool_call_id": "old", "content": "orphan"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "keep", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "keep", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]

    truncated = truncate_messages_with_pair_integrity(messages, 4)

    assert all(message.get("tool_call_id") != "old" for message in truncated)
    assert any(message.get("tool_call_id") == "keep" for message in truncated)
    assert any(
        call["id"] == "keep"
        for message in truncated if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    )


def test_deepseek_runtime_limit_is_clamped_for_legacy_invalid_config():
    assert get_max_tokens("deepseek", "deepseek-chat", 512000) == 393216


def test_successful_llm_content_is_not_misclassified_as_failover_error():
    assert not _is_llm_error_result("OK")
    assert not _is_llm_error_result("A normal assistant response")


@pytest.mark.parametrize(
    "result",
    [
        "[LLM Error] invalid api key",
        "[LLM call error] timeout",
        "[Error] too many tool rounds",
        "⚠️ 未配置 LLM 模型",
    ],
)
def test_all_llm_error_result_prefixes_are_detected(result):
    assert _is_llm_error_result(result)


def test_deepseek_new_invalid_limit_is_rejected_before_persistence():
    with pytest.raises(HTTPException) as exc:
        _validate_provider_token_limit("deepseek", 512000)

    assert exc.value.status_code == 422
    assert "393216" in exc.value.detail


def test_private_model_receives_exactly_one_leading_system_message():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "system", "content": "late policy"},
        {"role": "assistant", "content": "reply"},
        {"role": "system", "content": "onboarding policy"},
    ]

    ordered = _build_ordered_api_messages("agent soul", "dynamic memory", messages)

    assert [message.role for message in ordered] == ["system", "user", "assistant"]
    assert ordered[0].content == "agent soul\n\nlate policy\n\nonboarding policy"
    assert ordered[0].dynamic_content == "dynamic memory"


@pytest.mark.parametrize("tool_name", ["execute_code", "execute_code_e2b"])
@pytest.mark.parametrize("arguments", [{}, {"language": "bash"}, {"code": "  "}, {"code": 42}])
def test_execute_code_requires_non_empty_string_code(tool_name, arguments):
    should_execute, message = _check_tool_requires_args(tool_name, arguments)

    assert should_execute is False
    assert "required parameters" in message or "`code`" in message


def test_execute_code_accepts_valid_code():
    should_execute, message = _check_tool_requires_args(
        "execute_code",
        {"language": "bash", "code": "echo ok"},
    )

    assert should_execute is True
    assert message == ""


@pytest.mark.asyncio
async def test_process_tool_call_blocks_tool_not_enabled_for_agent(monkeypatch):
    executed = []

    async def execute_tool(*args, **kwargs):
        executed.append((args, kwargs))
        return "should not run"

    monkeypatch.setattr("app.services.llm.caller.execute_tool", execute_tool)
    api_messages = []

    error = await _process_tool_call(
        tc={
            "id": "call-disabled",
            "function": {"name": "execute_code", "arguments": '{"code":"echo unsafe"}'},
        },
        api_messages=api_messages,
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="session",
        supports_vision=False,
        on_tool_call=None,
        full_reasoning_content="",
        allowed_tool_names={"read_file"},
    )

    assert error == ""
    assert executed == []
    assert api_messages[-1].role == "tool"
    assert "not enabled" in api_messages[-1].content


@pytest.mark.asyncio
async def test_process_tool_call_passes_current_saas_tier_to_tool(monkeypatch):
    executed = []

    async def execute_tool(*args, **kwargs):
        executed.append((args, kwargs))
        return "ok"

    monkeypatch.setattr("app.services.llm.caller.execute_tool", execute_tool)

    await _process_tool_call(
        tc={
            "id": "call-read",
            "function": {"name": "read_file", "arguments": '{"path":"workspace/a.md"}'},
        },
        api_messages=[],
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id="session",
        supports_vision=False,
        on_tool_call=None,
        full_reasoning_content="",
        allowed_tool_names={"read_file"},
        route_meta=RouteMeta(saas_tier="ultra", modality="text"),
    )

    assert executed[0][1]["saas_tier"] == "ultra"
