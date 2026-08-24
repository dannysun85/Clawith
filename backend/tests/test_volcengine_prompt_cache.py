"""Volcengine Agent Plan Anthropic prefix-cache stability."""

from __future__ import annotations

from app.services.llm.client import (
    AnthropicClient,
    LLMMessage,
    create_llm_client,
)


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


def _volcengine_client() -> AnthropicClient:
    client = create_llm_client(
        provider="volcengine_agent_plan",
        api_key="test-key",
        model="doubao-seed-2.1-turbo",
    )
    assert isinstance(client, AnthropicClient)
    return client


def _system_text(payload: dict) -> str:
    return "\n".join(block["text"] for block in payload["system"])


def _user_text(message: dict) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict)
    )


def test_volcengine_client_uses_prefix_cache_on_anthropic_protocol():
    client = _volcengine_client()
    assert client.supports_prefix_cache is True

    native = create_llm_client(
        provider="anthropic",
        api_key="test-key",
        model="claude-sonnet-4-5",
    )
    assert isinstance(native, AnthropicClient)
    assert native.supports_prefix_cache is False


def test_native_anthropic_keeps_dynamic_content_in_system():
    client = AnthropicClient(api_key="test-key", model="claude-sonnet-4-5")
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
    assert "Current Time 12:00:01" in _system_text(payload)
    assert [message["role"] for message in payload["messages"]] == ["user"]
    assert _user_text(payload["messages"][0]) == "hello"


def test_volcengine_keeps_static_system_stable_and_appends_dynamic_after_history():
    client = _volcengine_client()
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

    assert first["system"] == [
        {
            "type": "text",
            "text": "Static Base Prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert second["system"] == first["system"]
    assert "Current Time" not in _system_text(first)
    assert first["tools"] == second["tools"]
    assert [tool["name"] for tool in first["tools"]] == ["finish", "wait"]
    assert list(first["tools"][0]["input_schema"]) == ["properties", "type"]
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    assert [message["role"] for message in first["messages"]] == ["user", "user"]
    assert first["messages"][0] == {"role": "user", "content": "hello"}
    assert "<agent_runtime_context>" in _user_text(first["messages"][-1])
    assert "Current Time 12:00:01" in _user_text(first["messages"][-1])
    assert "cache_control" not in first["messages"][-1]["content"][-1]

    assert [message["role"] for message in second["messages"]] == [
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert second["messages"][2] == {"role": "user", "content": "follow up"}
    assert "Current Time 12:00:02" in _user_text(second["messages"][-1])
    assert first["messages"][0] == second["messages"][0]
