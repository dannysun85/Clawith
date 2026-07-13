from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api import credentials as credentials_api
from app.core.security import encrypt_data
from app.schemas.credentials import CredentialCreateIn, CredentialUpdateIn
from app.services.credential_verification import (
    CredentialVerificationResult,
    build_credential_probe_request,
)


class _FakeCredentialDb:
    def __init__(self, credential=None):
        self.credential = credential
        self.added = None
        self.commits = 0

    def add(self, value):
        self.added = value

    async def get(self, _model, _credential_id):
        return self.credential

    async def commit(self):
        self.commits += 1

    async def refresh(self, value):
        value.id = value.id or uuid.uuid4()
        value.created_at = value.created_at or datetime.now(timezone.utc)
        value.updated_at = value.updated_at or datetime.now(timezone.utc)
        value.used_today = value.used_today or 0
        value.error_count = value.error_count or 0
        value.enabled = True if value.enabled is None else value.enabled
        value.status = value.status or "healthy"


@pytest.mark.asyncio
async def test_new_credential_is_unverified_until_probe_succeeds():
    db = _FakeCredentialDb()

    result = await credentials_api.create_credential(
        CredentialCreateIn(
            provider="minimax",
            label="Unverified key",
            api_key="sk-local-test-not-real",
            capabilities=["text", "image"],
        ),
        db=db,
    )

    assert result.status == "unverified"
    assert db.added.status == "unverified"


def test_minimax_probe_url_does_not_duplicate_v1():
    request = build_credential_probe_request(
        provider="minimax",
        base_url="https://api.minimaxi.com/v1/",
        api_key="secret-test-key",
    )

    assert request.url == "https://api.minimaxi.com/v1/models"
    assert request.headers["Authorization"] == "Bearer secret-test-key"


def test_static_credential_health_route_precedes_uuid_route():
    paths = [route.path for route in credentials_api.router.routes]

    assert paths.index("/credentials/health") < paths.index("/credentials/{credential_id}")


@pytest.mark.asyncio
async def test_successful_probe_promotes_credential_to_healthy(monkeypatch):
    credential_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        provider="minimax",
        label="Pending MiniMax",
        api_key_encrypted=encrypt_data("sk-test", credentials_api.settings.SECRET_KEY),
        base_url="https://api.minimaxi.com/v1",
        capabilities=["text"],
        daily_quota=None,
        used_today=0,
        status="unverified",
        modality_status={
            "video:minimax-hailuo-02": {"status": "quota_exceeded"},
        },
        error_count=9,
        weight=1,
        priority=0,
        last_used_at=None,
        enabled=True,
        rpm_limit=None,
        tpm_limit=None,
        window_5h_limit=None,
        tenant_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db = _FakeCredentialDb(credential)

    async def fake_probe(_credential):
        return CredentialVerificationResult(ok=True, provider_status=200, model_count=3)

    monkeypatch.setattr(credentials_api, "verify_provider_credential", fake_probe)

    result = await credentials_api.verify_credential(credential_id, db=db)

    assert result.ok is True
    assert result.status == "healthy"
    assert result.model_count == 3
    assert credential.status == "healthy"
    assert credential.error_count == 0
    assert set(credential.modality_status) == {"video:minimax-hailuo-02"}
    assert db.commits == 1


@pytest.mark.asyncio
async def test_replacing_api_key_requires_explicit_reverification():
    credential_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        provider="minimax",
        label="Degraded MiniMax",
        api_key_encrypted=encrypt_data("sk-old", credentials_api.settings.SECRET_KEY),
        base_url="https://api.minimaxi.com/v1",
        capabilities=["text"],
        daily_quota=None,
        used_today=4,
        status="degraded",
        error_count=8,
        weight=1,
        priority=0,
        last_used_at=None,
        enabled=True,
        rpm_limit=None,
        tpm_limit=None,
        window_5h_limit=None,
        tenant_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db = _FakeCredentialDb(credential)

    result = await credentials_api.update_credential(
        credential_id,
        CredentialUpdateIn(api_key="sk-replacement"),
        db=db,
    )

    assert result.status == "unverified"
    assert credential.error_count == 0
    assert credentials_api.get_credential_api_key(credential) == "sk-replacement"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_failed_probe_keeps_credential_out_of_pool(monkeypatch):
    credential_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        provider="minimax",
        label="Bad MiniMax",
        status="unverified",
    )
    db = _FakeCredentialDb(credential)

    async def fake_probe(_credential):
        return CredentialVerificationResult(ok=False, provider_status=401, message="认证失败")

    monkeypatch.setattr(credentials_api, "verify_provider_credential", fake_probe)

    result = await credentials_api.verify_credential(credential_id, db=db)

    assert result.ok is False
    assert result.status == "unverified"
    assert result.provider_status == 401
    assert credential.status == "unverified"
    assert db.commits == 1
