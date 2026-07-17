"""Account-recovery and product-boundary contracts for identity providers."""

from pathlib import Path
import json
import uuid

from fastapi import HTTPException
import pytest

from app.api import enterprise as enterprise_api


class _ScalarResult:
    def scalar(self):
        return 1


class _CaptureDB:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _ScalarResult()


@pytest.mark.asyncio
async def test_provider_disable_recovery_gate_checks_any_active_identity_membership():
    db = _CaptureDB()

    count = await enterprise_api._count_provider_users_without_local_recovery(
        db,
        uuid.uuid4(),
    )

    sql = str(db.statement.compile(compile_kwargs={"literal_binds": True}))
    assert count == 1
    assert "EXISTS" in sql
    assert "active_user.identity_id = identities.id" in sql
    assert "active_user.is_active IS true" in sql
    assert "linked_user.is_active" not in sql
    assert "active_tenant.is_active IS true" in sql


def test_unimplemented_generic_oauth2_is_not_advertised_in_enterprise_ui():
    source = (
        Path(__file__).parents[2]
        / "frontend/src/pages/enterprise-settings/tabs/OrgTab.tsx"
    ).read_text(encoding="utf-8")
    idp_list = source.split("const IDP_TYPES = [", 1)[1].split("];", 1)[0]

    assert "type: 'oauth2'" not in idp_list
    assert "Generic OIDC Provider" not in idp_list


def test_login_ui_does_not_advertise_unimplemented_cross_device_qr_sso():
    frontend = Path(__file__).parents[2] / "frontend/src"
    login_source = (frontend / "pages/Login.tsx").read_text(encoding="utf-8")
    login_css = (frontend / "index.css").read_text(encoding="utf-8")
    translations = "\n".join(
        (frontend / f"i18n/{language}.json").read_text(encoding="utf-8")
        for language in ("en", "zh")
    )

    assert "login-qr-" not in login_source
    assert "login-qr-" not in login_css
    for key in ("qrLogin", "unifiedSSO", "scanWithPlatform", "qrExpired"):
        assert f'"{key}"' not in translations


def test_provider_config_response_recursively_redacts_legacy_and_nested_secrets():
    config = {
        "appsecret": "legacy-app-secret",
        "app_secret_key": "legacy-app-secret-key",
        "corpsecret": "legacy-corp-secret",
        "service_account_json": json.dumps(
            {
                "client_email": "sync@example.test",
                "private_key": "service-account-private-key",
            }
        ),
        "nested": {
            "private_key": "nested-private-key",
            "region": "cn",
        },
        "items": [
            {"access_token": "list-access-token", "label": "safe"},
        ],
        "json_blob": json.dumps(
            {"client_secret": "json-client-secret", "region": "us"}
        ),
        "token_url": "https://idp.example.test/token",
    }

    sanitized = enterprise_api._sanitize_identity_provider_config(
        "google_workspace",
        config,
    )

    payload = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    for secret in (
        "legacy-app-secret",
        "legacy-app-secret-key",
        "legacy-corp-secret",
        "service-account-private-key",
        "nested-private-key",
        "list-access-token",
        "json-client-secret",
    ):
        assert secret not in payload
    assert sanitized["nested"]["region"] == "cn"
    assert sanitized["items"] == [{"label": "safe"}]
    assert json.loads(sanitized["json_blob"]) == {"region": "us"}
    assert sanitized["token_url"] == "https://idp.example.test/token"
    assert set(sanitized["_configured_secret_fields"]) >= {
        "appsecret",
        "app_secret_key",
        "corpsecret",
        "service_account_json",
        "nested.private_key",
        "items.0.access_token",
        "json_blob.client_secret",
    }


def test_masked_provider_update_preserves_existing_write_only_credentials():
    existing = {
        "client_id": "client-id",
        "client_secret": "stored-secret",
    }
    incoming = {
        "client_id": "updated-client-id",
        "_configured_secret_fields": ["client_secret"],
    }

    merged = enterprise_api._merge_identity_provider_config(existing, incoming)

    assert merged == {
        "client_id": "updated-client-id",
        "client_secret": "stored-secret",
    }


@pytest.mark.asyncio
async def test_generic_oauth2_creation_is_rejected_before_database_use():
    data = enterprise_api.IdentityProviderCreate(
        provider_type="oauth2",
        name="Unsupported",
        tenant_id=uuid.uuid4(),
    )

    with pytest.raises(HTTPException) as exc:
        await enterprise_api.create_identity_provider(
            data,
            current_user=object(),
            db=object(),
        )

    assert exc.value.status_code == 422
    assert "not implemented" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_dedicated_oauth2_creation_is_gone_before_database_use():
    data = enterprise_api.IdentityProviderOAuth2Create(
        name="Unsupported",
        app_id="client",
        app_secret="secret",
        authorize_url="https://idp.example.test/authorize",
        token_url="https://idp.example.test/token",
        user_info_url="https://idp.example.test/userinfo",
    )

    with pytest.raises(HTTPException) as exc:
        await enterprise_api.create_oauth2_provider(
            data,
            current_user=object(),
            db=object(),
        )

    assert exc.value.status_code == 410
