from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import agent_credentials as credentials_api


@pytest.mark.asyncio
async def test_use_level_agent_user_can_manage_only_their_private_login(monkeypatch):
    async def allow_use(_db, _user, _agent_id):
        return SimpleNamespace(id=_agent_id), "use"

    monkeypatch.setattr(credentials_api, "check_agent_access", allow_use)

    await credentials_api._require_self_credential_access(
        object(),
        SimpleNamespace(id=uuid.uuid4(), role="member"),
        uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_user_without_agent_access_cannot_manage_private_login(monkeypatch):
    async def deny(_db, _user, _agent_id):
        return SimpleNamespace(id=_agent_id), "none"

    monkeypatch.setattr(credentials_api, "check_agent_access", deny)

    with pytest.raises(HTTPException) as exc_info:
        await credentials_api._require_self_credential_access(
            object(),
            SimpleNamespace(id=uuid.uuid4(), role="member"),
            uuid.uuid4(),
        )

    assert exc_info.value.status_code == 403


def test_credential_response_never_contains_cookie_ciphertext():
    credential = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        credential_type="website",
        platform="example.com",
        display_name="Example",
        status="active",
        cookies_json="encrypted-cookie-secret",
        cookies_updated_at=None,
        last_login_at=None,
        last_injected_at=None,
        created_at=None,
        updated_at=None,
    )

    response = credentials_api._to_response(credential)

    assert "cookies_json" not in response
    assert response["has_cookies"] is True
    assert "encrypted-cookie-secret" not in str(response)
