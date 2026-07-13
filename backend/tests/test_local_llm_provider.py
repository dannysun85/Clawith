"""Local OpenAI-compatible provider regressions."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm import caller
from app.services.llm.client import OpenAICompatibleClient, create_llm_client, get_provider_manifest
from app.services.llm.load_balancer import NoCredentialAvailable


@pytest.mark.parametrize("provider", ["vllm", "ollama", "sglang"])
def test_local_provider_allows_no_auth_header(provider):
    client = create_llm_client(provider, api_key="", model="local-model")

    assert isinstance(client, OpenAICompatibleClient)
    assert client._get_headers() == {"Content-Type": "application/json"}


def test_provider_manifest_marks_only_local_runtimes_as_key_optional():
    manifest = {item["provider"]: item for item in get_provider_manifest()}

    assert manifest["vllm"]["requires_api_key"] is False
    assert manifest["ollama"]["requires_api_key"] is False
    assert manifest["sglang"]["requires_api_key"] is False
    assert manifest["openai"]["requires_api_key"] is True


@pytest.mark.asyncio
async def test_platform_vllm_model_runs_without_credential_pool_entry(monkeypatch):
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="vllm",
        modality="text",
        base_url="http://vllm.internal:8000/v1",
    )
    monkeypatch.setattr(
        caller,
        "pick_credential",
        AsyncMock(side_effect=NoCredentialAvailable("vllm", "text")),
    )

    assert await caller.resolve_model_key(model) == (
        "",
        "http://vllm.internal:8000/v1",
        None,
    )


@pytest.mark.asyncio
async def test_platform_openai_model_still_requires_credential_pool_entry(monkeypatch):
    model = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="openai",
        modality="text",
        base_url=None,
    )
    error = NoCredentialAvailable("openai", "text")
    monkeypatch.setattr(caller, "pick_credential", AsyncMock(side_effect=error))

    with pytest.raises(NoCredentialAvailable):
        await caller.resolve_model_key(model)
