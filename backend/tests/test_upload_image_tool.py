import base64
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest


class _Response:
    status_code = 201
    text = ""

    def json(self):
        return {
            "url": "https://cdn.example/poster.png",
            "fileId": "file-1",
            "size": 2048,
            "name": "poster.png",
        }


class _Client:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


@pytest.mark.asyncio
async def test_upload_image_local_file_uses_authenticated_multipart(
    monkeypatch, tmp_path
):
    from app.services import agent_tools

    image = tmp_path / "poster.png"
    image.write_bytes(b"valid-image-bytes")
    client = _Client()
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"private_key": "private-secret"}),
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    result = await agent_tools._upload_image(
        uuid.uuid4(), tmp_path, {"file_path": "poster.png"}
    )

    assert "✅ Image uploaded successfully" in result
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == "https://upload.imagekit.io/api/v2/files/upload"
    assert kwargs["headers"]["Authorization"] == "Basic " + base64.b64encode(
        b"private-secret:"
    ).decode()
    assert kwargs["files"] == {"file": ("poster.png", b"valid-image-bytes")}
    assert kwargs["data"]["fileName"] == "poster.png"


@pytest.mark.asyncio
async def test_upload_image_public_url_uses_validated_form_value(
    monkeypatch, tmp_path
):
    from app.services import agent_tools

    client = _Client()
    validate = AsyncMock(
        return_value=("https://assets.example/product.png", None)
    )
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"private_key": "private-secret"}),
    )
    monkeypatch.setattr(agent_tools, "_validate_public_http_url", validate)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    result = await agent_tools._upload_image(
        uuid.uuid4(), tmp_path, {"url": "assets.example/product.png"}
    )

    assert "https://cdn.example/poster.png" in result
    validate.assert_awaited_once_with("assets.example/product.png")
    _url, kwargs = client.calls[0]
    assert "files" not in kwargs
    assert kwargs["data"]["file"] == "https://assets.example/product.png"
    assert kwargs["data"]["fileName"] == "product.png"


@pytest.mark.asyncio
async def test_upload_image_rejects_workspace_prefix_escape(monkeypatch, tmp_path):
    from app.services import agent_tools

    workspace = tmp_path / "work"
    workspace.mkdir()
    sibling = tmp_path / "work-escape"
    sibling.mkdir()
    (sibling / "secret.png").write_bytes(b"secret")
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"private_key": "private-secret"}),
    )

    result = await agent_tools._upload_image(
        uuid.uuid4(), workspace, {"file_path": "../work-escape/secret.png"}
    )

    assert result == "❌ Access denied: path is outside the workspace"
