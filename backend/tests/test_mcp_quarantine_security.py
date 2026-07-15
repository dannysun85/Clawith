import uuid

from app.core.security import decrypt_data
from app.scripts.secure_mcp_quarantine import (
    _secure_assignment_snapshot,
    _snapshot_tool_tenant_id,
    _secure_tool_snapshot,
    _strict_encrypt_config,
)
from app.services.tool_config import (
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    mask_sensitive_fields,
    merge_config_preserving_sensitive,
)


TEST_SECRET = "mcp-quarantine-test-secret"


def test_strict_snapshot_encryption_covers_schema_and_heuristic_keys():
    secured = _strict_encrypt_config(
        {
            "privateKey": "private-value",
            "custom_password": "password-value",
            "headers": {"Authorization": "Bearer nested-secret"},
            "workspace": "public-value",
        },
        {"fields": [{"key": "custom_password", "type": "password"}]},
        secret_key=TEST_SECRET,
    )

    assert secured["privateKey"] != "private-value"
    assert secured["custom_password"] != "password-value"
    assert decrypt_data(secured["privateKey"], TEST_SECRET) == "private-value"
    assert decrypt_data(secured["custom_password"], TEST_SECRET) == "password-value"
    assert decrypt_data(secured["headers"]["Authorization"], TEST_SECRET) == "Bearer nested-secret"
    assert secured["workspace"] == "public-value"


def test_snapshot_tenant_resolution_only_adopts_unique_agent_installed_tool():
    tenant = uuid.uuid4()
    assert _snapshot_tool_tenant_id(
        source="agent",
        actual_tenant_id=None,
        assigned_tenant_id=tenant,
        assigned_tenant_count=1,
    ) == tenant
    for source in ("builtin", "admin"):
        assert _snapshot_tool_tenant_id(
            source=source,
            actual_tenant_id=None,
            assigned_tenant_id=tenant,
            assigned_tenant_count=1,
        ) is None
    assert _snapshot_tool_tenant_id(
        source="agent",
        actual_tenant_id=None,
        assigned_tenant_id=tenant,
        assigned_tenant_count=2,
    ) is None


def test_nested_credentials_round_trip_mask_and_preserve(monkeypatch):
    from app.services import tool_config

    monkeypatch.setattr(
        tool_config,
        "get_settings",
        lambda: type("Settings", (), {"SECRET_KEY": TEST_SECRET})(),
    )
    raw = {
        "headers": {
            "Authorization": "Bearer nested-secret",
            "Accept": "application/json",
        },
        "workspace": "one",
    }

    encrypted = encrypt_sensitive_fields(raw)
    assert encrypted["headers"]["Authorization"] != "Bearer nested-secret"
    assert decrypt_sensitive_fields(encrypted) == raw
    assert mask_sensitive_fields(raw)["headers"]["Authorization"].startswith("****")

    merged = merge_config_preserving_sensitive(
        raw,
        {"headers": {"Authorization": "****cret", "Accept": "text/plain"}},
    )
    assert merged["headers"]["Authorization"] == "Bearer nested-secret"
    assert merged["headers"]["Accept"] == "text/plain"


def test_tenant_tool_snapshot_splits_and_encrypts_query_credentials():
    enabled, server_url, config = _secure_tool_snapshot(
        enabled=True,
        server_url="https://mcp.example.test/api?workspace=one&tavilyApiKey=secret",
        config={"api_key": "config-secret"},
        config_schema={},
        server_name="Example",
        source="agent",
        tenant_id=uuid.uuid4(),
        secret_key=TEST_SECRET,
    )

    assert enabled is True
    assert server_url == "https://mcp.example.test/api?workspace=one"
    assert "secret" not in server_url
    assert decrypt_data(config["api_key"], TEST_SECRET) == "config-secret"
    assert "secret" not in config["mcp_url_query_secrets"]
    assert "tavilyApiKey" in decrypt_data(
        config["mcp_url_query_secrets"],
        TEST_SECRET,
    )


def test_unsafe_or_ownerless_tool_snapshot_fails_closed():
    assert _secure_tool_snapshot(
        enabled=True,
        server_url="https://user:secret@mcp.example.test/api",
        config={"api_key": "secret"},
        config_schema={},
        server_name="Example",
        source="agent",
        tenant_id=uuid.uuid4(),
        secret_key=TEST_SECRET,
    ) == (False, None, {})
    assert _secure_tool_snapshot(
        enabled=True,
        server_url="https://mcp.example.test/api?apiKey=shared-secret",
        config={},
        config_schema={},
        server_name="Example",
        source="admin",
        tenant_id=None,
        secret_key=TEST_SECRET,
    ) == (False, None, {})
    assert _secure_tool_snapshot(
        enabled=True,
        server_url="https://mcp.example.test/api",
        config={"api_key": "orphan-secret"},
        config_schema={},
        server_name="Example",
        source="agent",
        tenant_id=None,
        secret_key=TEST_SECRET,
    ) == (False, None, {})


def test_assignment_snapshot_requires_exact_visibility_and_scrubs_atlassian():
    tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    enabled, config = _secure_assignment_snapshot(
        enabled=True,
        config={"api_key": "secret"},
        config_schema={},
        server_name="Example",
        source="agent",
        tool_tenant_id=tenant,
        agent_tenant_id=tenant,
        secret_key=TEST_SECRET,
    )
    assert enabled is True
    assert decrypt_data(config["api_key"], TEST_SECRET) == "secret"

    assert _secure_assignment_snapshot(
        enabled=True,
        config={"api_key": "foreign-secret"},
        config_schema={},
        server_name="Example",
        source="agent",
        tool_tenant_id=tenant,
        agent_tenant_id=other_tenant,
        secret_key=TEST_SECRET,
    ) == (False, {})

    assert _secure_assignment_snapshot(
        enabled=True,
        config={"atlassian_api_key": "legacy-secret"},
        config_schema={},
        server_name="Atlassian Rovo",
        source="builtin",
        tool_tenant_id=None,
        agent_tenant_id=tenant,
        secret_key=TEST_SECRET,
    ) == (True, {})
