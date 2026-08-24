"""MiniMax automatic prompt-cache prefix stability."""

from __future__ import annotations

import json

import pytest

from loguru import logger

from app.services.llm.client import (
    LLMMessage,
    LLMRequestShapeError,
    OpenAICompatibleClient,
    canonicalize_tools_for_prefix_cache,
    create_llm_client,
)
from app.services.token_tracker import extract_token_usage


def _minimax_client() -> OpenAICompatibleClient:
    client = create_llm_client(
        provider="minimax",
        api_key="test-key",
        model="MiniMax-M3",
    )
    assert isinstance(client, OpenAICompatibleClient)
    return client


def _shuffled_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "wait",
                "description": "Wait",
                "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Finish",
                "parameters": {
                    "properties": {"content": {"type": "string"}},
                    "type": "object",
                },
            },
        },
    ]


def test_minimax_client_uses_prefix_cache_not_cache_control():
    client = _minimax_client()
    assert client.supports_prefix_cache is True
    assert client.supports_cache_control is False


def test_qwen_keeps_explicit_cache_control_and_does_not_use_minimax_prefix_layout():
    client = create_llm_client(provider="qwen", api_key="test-key", model="qwen-plus")
    assert isinstance(client, OpenAICompatibleClient)
    assert client.supports_cache_control is True
    assert client.supports_prefix_cache is False

    payload = client._build_payload(
        [
            LLMMessage(
                role="system",
                content="Static Base Prompt",
                dynamic_content="Current Time 12:00:01",
            ),
            LLMMessage(role="user", content="hello"),
        ],
        tools=None,
        temperature=0.2,
        max_tokens=16,
    )
    system = payload["messages"][0]
    assert system["role"] == "system"
    assert isinstance(system["content"], list)
    assert system["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Current Time 12:00:01" in system["content"][1]["text"]
    assert "cache_control" not in system["content"][1]
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]


def test_minimax_keeps_static_system_stable_and_appends_dynamic_after_history():
    client = _minimax_client()
    first = client._build_payload(
        [
            LLMMessage(
                role="system",
                content="Static Base Prompt",
                dynamic_content="Current Time 12:00:01",
            ),
            LLMMessage(role="user", content="hello"),
        ],
        tools=_shuffled_tools(),
        temperature=0.2,
        max_tokens=16,
    )
    second = client._build_payload(
        [
            LLMMessage(
                role="system",
                content="Static Base Prompt",
                dynamic_content="Current Time 12:00:02\nMemory changed",
            ),
            LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content="hi"),
            LLMMessage(role="user", content="follow up"),
        ],
        tools=list(reversed(_shuffled_tools())),
        temperature=0.2,
        max_tokens=16,
    )

    assert first["messages"][0] == {"role": "system", "content": "Static Base Prompt"}
    assert second["messages"][0] == first["messages"][0]
    assert first["tools"] == second["tools"]
    assert [tool["function"]["name"] for tool in first["tools"]] == ["finish", "wait"]
    assert list(first["tools"][0]["function"]["parameters"]) == ["properties", "type"]
    assert "cache_control" not in json.dumps(first)
    assert "cache_control" not in json.dumps(second)

    assert [message["role"] for message in first["messages"]] == ["system", "user", "user"]
    assert first["messages"][1]["content"] == "hello"
    assert "<agent_runtime_context>" in first["messages"][-1]["content"]
    assert "Current Time 12:00:01" in first["messages"][-1]["content"]
    assert "Static Base Prompt" not in first["messages"][-1]["content"]

    assert [message["role"] for message in second["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert second["messages"][1:4] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "follow up"},
    ]
    assert "Current Time 12:00:02" in second["messages"][-1]["content"]
    assert first["messages"][0:2] == second["messages"][0:2]


def test_caller_ordered_messages_keep_minimax_system_prefix_stable():
    from app.services.llm.caller import _build_ordered_api_messages

    client = _minimax_client()
    first = client._build_payload(
        _build_ordered_api_messages(
            "agent soul",
            "Current Time 12:00:01",
            [{"role": "user", "content": "hello"}],
        ),
        tools=_shuffled_tools(),
        temperature=0.2,
        max_tokens=16,
    )
    second = client._build_payload(
        _build_ordered_api_messages(
            "agent soul",
            "Current Time 12:00:02",
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "follow up"},
            ],
        ),
        tools=list(reversed(_shuffled_tools())),
        temperature=0.2,
        max_tokens=16,
    )

    assert first["messages"][0] == {"role": "system", "content": "agent soul"}
    assert second["messages"][0] == first["messages"][0]
    assert first["tools"] == second["tools"]
    assert first["messages"][1]["content"] == "hello"
    assert "Current Time 12:00:01" in first["messages"][-1]["content"]
    assert "Current Time" not in first["messages"][0]["content"]


def test_minimax_prefix_cache_rejects_non_object_tools():
    with pytest.raises(LLMRequestShapeError, match="prefix-cache tools must be JSON objects"):
        canonicalize_tools_for_prefix_cache(["finish"])  # type: ignore[list-item]


def _capture_loguru_warnings() -> tuple[list[str], int]:
    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(str(message)), level="WARNING")
    return records, handler_id


def test_minimax_usage_maps_prompt_tokens_details_cached_tokens():
    usage = extract_token_usage(
        {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    assert usage is not None
    assert usage.input_tokens == 1200
    assert usage.cache_read_tokens == 800
    assert usage.cache_creation_tokens == 0


def test_minimax_usage_warns_on_unmapped_cache_fields():
    unknown_field = "cache_hit_ratio_PRIVATE_CUSTOMER_MARKER"
    records, handler_id = _capture_loguru_warnings()
    try:
        usage = extract_token_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 40},
                unknown_field: 0.4,
            }
        )
    finally:
        logger.remove(handler_id)
    assert usage is not None
    assert usage.cache_read_tokens == 40
    assert any("Unmapped cache usage field detected" in text for text in records)
    assert all(unknown_field not in text for text in records)


def test_unparseable_cache_counter_is_not_silently_zeroed():
    records, handler_id = _capture_loguru_warnings()
    try:
        usage = extract_token_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": "not-a-number"},
            }
        )
    finally:
        logger.remove(handler_id)
    assert usage is not None
    assert usage.cache_read_tokens == 0
    assert any("Unparseable usage field cached_tokens" in text for text in records)
