import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from types import SimpleNamespace

from app.api import admin as admin_api
from app.api import tenants as tenants_api
from app.services.platform_service import platform_service
from app.services.sso_service import SSOService
from app.services.system_setting_security import strict_system_setting_enabled
from tests.test_auth import RecordingDB, DummyResult
from app.database import _session_ctx
from pydantic import ValidationError


async def run_with_db(db, func, *args, **kwargs):
    token = _session_ctx.set(db)
    try:
        return await func(*args, **kwargs)
    finally:
        _session_ctx.reset(token)

@pytest.mark.asyncio
async def test_get_platform_settings_sso_toggle_default():
    """Verify that get_platform_settings returns sso_custom_domain_redirect_enabled by default."""
    db = RecordingDB(responses=[
        DummyResult(),  # allow_self_create_company lookup -> None (default True)
        DummyResult(),  # invitation_code_enabled lookup -> None (default True)
        DummyResult(),  # sso_custom_domain_redirect_enabled lookup -> None (fail-closed)
    ])
    
    current_user = MagicMock()
    settings = await admin_api.get_platform_settings(current_user=current_user, db=db)
    
    assert settings.sso_custom_domain_redirect_enabled is False
    assert settings.allow_self_create_company is True
    assert settings.invitation_code_enabled is True


@pytest.mark.asyncio
async def test_get_platform_settings_sso_toggle_disabled():
    """Verify that get_platform_settings returns sso_custom_domain_redirect_enabled False if set."""
    setting_record = SimpleNamespace(key="sso_custom_domain_redirect_enabled", value={"enabled": False})
    db = RecordingDB(responses=[
        DummyResult(),  # allow_self_create_company -> None
        DummyResult(),  # invitation_code_enabled -> None
        DummyResult(values=[setting_record]),  # sso_custom_domain_redirect_enabled -> disabled
    ])
    
    current_user = MagicMock()
    settings = await admin_api.get_platform_settings(current_user=current_user, db=db)
    assert settings.sso_custom_domain_redirect_enabled is False


@pytest.mark.parametrize(
    "value",
    [
        {"enabled": "false"},
        {"enabled": 1},
        {"enabled": None},
        "false",
        None,
    ],
)
def test_sso_toggle_accepts_only_a_real_json_boolean(value):
    assert strict_system_setting_enabled(value, default=False) is False


def test_sso_toggle_accepts_explicit_true():
    assert strict_system_setting_enabled({"enabled": True}, default=False) is True


@pytest.mark.asyncio
async def test_resolve_tenant_by_domain_sso_toggle():
    """Verify that resolve_tenant_by_domain respects the sso_custom_domain_redirect_enabled toggle."""
    # When enabled, custom domain lookup should match the tenant by domain
    active_tenant = SimpleNamespace(id="tenant-id", name="Acme", slug="acme", sso_enabled=True, sso_domain="https://acme.com", is_active=True)
    
    # Check 1: SSO toggle enabled, matches tenant
    db_enabled = RecordingDB(responses=[
        DummyResult(values=[SimpleNamespace(value={"enabled": True})]),
        DummyResult(values=[active_tenant]),  # Match for https://acme.com
    ])
    res = await tenants_api.resolve_tenant_by_domain(domain="acme.com", db=db_enabled)
    assert res["id"] == "tenant-id"
    assert res["sso_domain"] == "https://acme.com"

    # Check 2: SSO toggle disabled, does not match tenant by domain, falls back or fails
    setting_disabled = SimpleNamespace(key="sso_custom_domain_redirect_enabled", value={"enabled": False})
    db_disabled = RecordingDB(responses=[
        DummyResult(values=[setting_disabled]),  # sso_custom_domain_redirect_enabled -> False
        DummyResult(),  # Fallback search slug (which fails)
    ])
    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(domain="acme.com", db=db_disabled)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_tenant_domain_uses_exact_host_and_port_without_like_fallback():
    tenant = SimpleNamespace(
        id="tenant-id",
        name="Acme",
        slug="acme",
        sso_enabled=True,
        sso_domain="https://tenant.example:8443",
        is_active=True,
    )
    db = RecordingDB(
        responses=[
            DummyResult(values=[SimpleNamespace(value={"enabled": True})]),
            DummyResult(values=[tenant]),
        ]
    )

    result = await tenants_api.resolve_tenant_by_domain(
        domain="TENANT.EXAMPLE.:8443",
        db=db,
    )

    assert result["id"] == "tenant-id"
    statement = str(db.statements[1])
    assert "lower(tenants.sso_domain) IN" in statement
    assert " LIKE " not in statement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain",
    [
        "tenant.example/path",
        "user@tenant.example",
        "tenant.example?next=evil.example",
        "tenant.example#fragment",
        "tenant.example:invalid",
    ],
)
async def test_resolve_tenant_domain_rejects_non_host_inputs(domain):
    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(domain=domain, db=RecordingDB())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_tenant_domain_fails_closed_on_ambiguous_exact_origin():
    db = RecordingDB(
        responses=[
            DummyResult(values=[SimpleNamespace(value={"enabled": True})]),
            DummyResult(values=[SimpleNamespace(id="one"), SimpleNamespace(id="two")]),
        ]
    )

    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(domain="tenant.example", db=db)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_tenant_by_domain_missing_toggle_fails_closed():
    """A missing setting cannot enable a cross-origin tenant lookup."""
    db = RecordingDB(responses=[
        DummyResult(),  # no sso_custom_domain_redirect_enabled row
        DummyResult(),  # slug fallback does not match the custom domain
    ])

    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(domain="acme.com", db=db)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_synthetic_slug_domain_never_bypasses_missing_toggle():
    db = RecordingDB(responses=[DummyResult()])

    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(
            domain="acme.astra.ai",
            db=db,
        )

    assert exc.value.status_code == 404
    assert len(db.statements) == 1


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "javascript:alert(document.domain)",
        "data:text/html,unsafe",
        "https://user:secret@tenant.example",
        "//tenant.example",
        "https://tenant.example/callback",
        "https://tenant.example/#unsafe",
    ],
)
def test_tenant_update_rejects_unsafe_sso_domain(unsafe_url):
    with pytest.raises(ValidationError):
        tenants_api.TenantUpdate(sso_domain=unsafe_url)


def test_tenant_update_normalizes_safe_sso_domain():
    update = tenants_api.TenantUpdate(
        sso_domain=" https://tenant.example/ ",
    )

    assert update.sso_domain == "https://tenant.example"


def test_email_domain_sso_candidates_are_exact_origins_only():
    assert platform_service.sso_origin_candidates_for_email_domain(
        "ReefTotem.AI."
    ) == (
        "reeftotem.ai",
        "https://reeftotem.ai",
        "http://reeftotem.ai",
    )
    assert platform_service.sso_origin_candidates_for_email_domain(
        "reeftotem.ai/path"
    ) == ()
    assert platform_service.sso_origin_candidates_for_email_domain(
        "tenant.reeftotem.ai@evil.example"
    ) == ()


@pytest.mark.asyncio
async def test_auto_associate_tenant_uses_exact_active_origin_and_fails_ambiguous():
    tenant = SimpleNamespace(id="tenant-id")
    db = RecordingDB(responses=[DummyResult(values=[tenant])])

    result = await SSOService().auto_associate_tenant(db, "user@reeftotem.ai")

    assert result == "tenant-id"
    statement = str(db.statements[0])
    assert "lower(tenants.sso_domain) IN" in statement
    where_clause = statement.split("WHERE", 1)[1]
    assert "tenants.name" not in where_clause
    assert " LIKE " not in where_clause

    ambiguous = RecordingDB(
        responses=[DummyResult(values=[tenant, SimpleNamespace(id="other-tenant")])]
    )
    assert (
        await SSOService().auto_associate_tenant(
            ambiguous,
            "user@reeftotem.ai",
        )
        is None
    )


@pytest.mark.asyncio
async def test_resolve_platform_root_domain_returns_empty_success(monkeypatch):
    """The public platform host is not a tenant-specific SSO domain."""
    monkeypatch.setattr(
        tenants_api,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://opc.reeftotem.ai"),
    )
    db = RecordingDB(responses=[DummyResult()])

    result = await tenants_api.resolve_tenant_by_domain(
        domain="opc.reeftotem.ai",
        db=db,
    )

    assert result is None


@pytest.mark.asyncio
async def test_resolve_platform_root_alias_returns_empty_success(monkeypatch):
    """Every explicitly configured product origin stays tenant-neutral."""
    monkeypatch.setattr(
        tenants_api,
        "get_settings",
        lambda: SimpleNamespace(
            PUBLIC_BASE_URL="https://opc.rama-server.com",
            PUBLIC_BASE_URL_ALIASES=(
                "https://opc.reeftotem.ai, https://preview.example.test:8443/"
            ),
        ),
    )

    assert await tenants_api.resolve_tenant_by_domain(
        domain="OPC.REEFTOTEM.AI.",
        db=RecordingDB(),
    ) is None
    assert await tenants_api.resolve_tenant_by_domain(
        domain="preview.example.test:8443",
        db=RecordingDB(),
    ) is None


@pytest.mark.asyncio
async def test_resolve_unknown_domain_still_fails_closed_with_platform_aliases(
    monkeypatch,
):
    monkeypatch.setattr(
        tenants_api,
        "get_settings",
        lambda: SimpleNamespace(
            PUBLIC_BASE_URL="https://opc.rama-server.com",
            PUBLIC_BASE_URL_ALIASES="https://opc.reeftotem.ai",
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(
            domain="unknown.example.test",
            db=RecordingDB(responses=[DummyResult()]),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_tenant_sso_base_url_toggle():
    """Verify that get_tenant_sso_base_url respects the sso_redirect_enabled kwarg."""
    tenant = SimpleNamespace(
        slug="acme",
        sso_enabled=True,
        sso_domain="https://acme.com",
    )

    # 1. Enabled: returns the custom sso_domain
    url = await platform_service.get_tenant_sso_base_url(
        db=None, tenant=tenant, sso_redirect_enabled=True
    )
    assert url == "https://acme.com"

    # 2. Disabled: falls back to public base URL
    with patch.object(platform_service, "get_public_base_url", return_value="https://try.astra.ai"):
        url = await platform_service.get_tenant_sso_base_url(
            db=None, tenant=tenant, sso_redirect_enabled=False
        )
        assert url == "https://try.astra.ai"

    # 3. Global enablement without tenant SSO must never synthesize a hostname.
    tenant.sso_enabled = False
    with patch.object(platform_service, "get_public_base_url", return_value="https://opc.reeftotem.ai"):
        url = await platform_service.get_tenant_sso_base_url(
            db=None,
            tenant=tenant,
            sso_redirect_enabled=True,
        )
        assert url == "https://opc.reeftotem.ai"


@pytest.mark.asyncio
async def test_get_tenant_sso_base_url_omitted_flag_fails_closed():
    tenant = SimpleNamespace(
        slug="acme",
        sso_enabled=True,
        sso_domain="https://acme.example",
    )
    db = RecordingDB(responses=[DummyResult()])

    with patch.object(
        platform_service,
        "get_public_base_url",
        return_value="https://opc.reeftotem.ai",
    ):
        url = await platform_service.get_tenant_sso_base_url(db, tenant)

    assert url == "https://opc.reeftotem.ai"


@pytest.mark.asyncio
async def test_get_tenant_sso_base_url_rejects_legacy_unsafe_row():
    tenant = SimpleNamespace(
        slug="acme",
        sso_enabled=True,
        sso_domain="javascript:alert(document.domain)",
    )

    with patch.object(
        platform_service,
        "get_public_base_url",
        return_value="https://opc.reeftotem.ai",
    ):
        url = await platform_service.get_tenant_sso_base_url(
            None,
            tenant,
            sso_redirect_enabled=True,
        )

    assert url == "https://opc.reeftotem.ai"


@pytest.mark.asyncio
async def test_switch_tenant_sso_toggle():
    """Verify that switch_tenant API respects the sso_custom_domain_redirect_enabled toggle."""
    from app.api import auth as auth_api
    from app.schemas.schemas import TenantSwitchRequest
    import uuid

    target_tenant_id = uuid.uuid4()
    target_user = SimpleNamespace(id=uuid.uuid4(), role="member", is_active=True)
    tenant = SimpleNamespace(
        id=target_tenant_id,
        slug="acme",
        sso_enabled=True,
        sso_domain="https://acme.com",
        is_active=True,
    )
    current_user = SimpleNamespace(
        identity_id=uuid.uuid4(),
        identity=SimpleNamespace(auth_version=0),
    )
    data = TenantSwitchRequest(tenant_id=target_tenant_id)
    request = MagicMock()

    # Case 1: Toggle enabled -> redirect_url is returned
    db_enabled = RecordingDB(responses=[
        DummyResult(values=[target_user]), # user check
        DummyResult(values=[tenant]),      # tenant details
        DummyResult(values=[SimpleNamespace(value={"enabled": True})]),
    ])
    with patch("app.api.auth.create_access_token", return_value="jwt-token"):
        res = await run_with_db(db_enabled, auth_api.switch_tenant, data, request, current_user)
        assert res.access_token == "jwt-token"
        assert res.target_tenant_id == target_tenant_id
        assert res.redirect_url is not None
        assert "https://acme.com" in res.redirect_url
        assert "?token=" not in res.redirect_url
        assert "#session_token=jwt-token" in res.redirect_url
        assert f"target_tenant_id={target_tenant_id}" in res.redirect_url

    # Case 2: Toggle disabled -> redirect_url is None
    setting_disabled = SimpleNamespace(key="sso_custom_domain_redirect_enabled", value={"enabled": False})
    db_disabled = RecordingDB(responses=[
        DummyResult(values=[target_user]), # user check
        DummyResult(values=[tenant]),      # tenant details
        DummyResult(values=[setting_disabled]), # auth_api setting check (disabled)
    ])
    with patch("app.api.auth.create_access_token", return_value="jwt-token"):
        res = await run_with_db(db_disabled, auth_api.switch_tenant, data, request, current_user)
        assert res.access_token == "jwt-token"
        assert res.target_tenant_id == target_tenant_id
        assert res.redirect_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sso_enabled", "sso_domain"),
    [
        (False, "https://acme.com"),
        (True, None),
    ],
)
async def test_switch_tenant_never_synthesizes_redirect_without_tenant_sso(
    sso_enabled,
    sso_domain,
):
    """A global toggle cannot invent a tenant hostname or bypass tenant SSO state."""
    from app.api import auth as auth_api
    from app.schemas.schemas import TenantSwitchRequest
    import uuid

    target_tenant_id = uuid.uuid4()
    target_user = SimpleNamespace(id=uuid.uuid4(), role="member", is_active=True)
    tenant = SimpleNamespace(
        id=target_tenant_id,
        slug="acme",
        sso_enabled=sso_enabled,
        sso_domain=sso_domain,
        is_active=True,
    )
    current_user = SimpleNamespace(
        identity_id=uuid.uuid4(),
        identity=SimpleNamespace(auth_version=0),
    )
    db = RecordingDB(
        responses=[
            DummyResult(values=[target_user]),
            DummyResult(values=[tenant]),
            DummyResult(values=[SimpleNamespace(value={"enabled": True})]),
        ]
    )

    with patch("app.api.auth.create_access_token", return_value="jwt-token"):
        result = await run_with_db(
            db,
            auth_api.switch_tenant,
            TenantSwitchRequest(tenant_id=target_tenant_id),
            MagicMock(),
            current_user,
        )

    assert result.access_token == "jwt-token"
    assert result.redirect_url is None
