import shlex
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import tools as tools_api
from app.services import agent_tools
from app.services.autonomy_service import (
    AutonomyService,
    _verified_tool_arguments,
    build_tool_approval_details,
)
from app.services.code_execution_policy import (
    code_execution_denial_reason,
    code_execution_tenant_authorized,
)
from app.services.sandbox.api.e2b_backend import _build_e2b_command
from app.services.tool_config import merge_config_preserving_sensitive


def _settings(**overrides):
    values = {
        "CODE_EXECUTION_ENABLED": False,
        "CODE_EXECUTION_ALLOWED_TENANT_IDS": "",
        "CODE_EXECUTION_ALLOWED_SANDBOX_TYPES": (
            "e2b,aio_sandbox,self_hosted,judge0,codesandbox"
        ),
        "ENVIRONMENT": "production",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_code_execution_requires_platform_and_exact_tenant_grant():
    tenant_id = uuid.uuid4()

    assert code_execution_tenant_authorized(_settings(), tenant_id) is False
    assert (
        code_execution_tenant_authorized(
            _settings(
                CODE_EXECUTION_ENABLED=True,
                CODE_EXECUTION_ALLOWED_TENANT_IDS="*",
            ),
            tenant_id,
        )
        is False
    )
    assert (
        code_execution_tenant_authorized(
            _settings(
                CODE_EXECUTION_ENABLED=True,
                CODE_EXECUTION_ALLOWED_TENANT_IDS=str(tenant_id),
            ),
            tenant_id,
        )
        is True
    )


def test_production_code_execution_rejects_local_or_unapproved_sandbox():
    tenant_id = uuid.uuid4()
    settings = _settings(
        CODE_EXECUTION_ENABLED=True,
        CODE_EXECUTION_ALLOWED_TENANT_IDS=str(tenant_id),
    )

    assert "isolated external sandbox" in code_execution_denial_reason(
        settings,
        tenant_id,
        sandbox_type="subprocess",
    )
    assert "not approved" in code_execution_denial_reason(
        settings,
        tenant_id,
        sandbox_type="unknown-provider",
    )
    assert (
        code_execution_denial_reason(
            settings,
            tenant_id,
            sandbox_type="e2b",
            allow_network=True,
        )
        is None
    )
    assert "network access" in code_execution_denial_reason(
        settings,
        tenant_id,
        sandbox_type="e2b",
        allow_network=False,
    )


def test_e2b_command_keeps_adversarial_code_inside_single_argument():
    code = "print(\"it's safe\"); __import__('os').system('echo still-in-sandbox')"
    command = _build_e2b_command("python", code)

    assert shlex.split(command) == ["python3", "-c", code]


def test_signed_approval_preserves_complete_long_payload_and_rejects_tampering(
    monkeypatch,
):
    from app import config as config_module

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(SECRET_KEY="approval-test-secret"),
    )
    agent_id = uuid.uuid4()
    requested_by = uuid.uuid4()
    arguments = {"language": "python", "code": "print('x')\n" * 200}

    details = build_tool_approval_details(
        agent_id,
        "execute_code",
        arguments,
        requested_by,
    )

    assert "args" not in details
    assert arguments["code"] not in str(details)
    assert _verified_tool_arguments(agent_id, details) == (
        "execute_code",
        arguments,
    )

    details["args_hash"] = "0" * 64
    with pytest.raises(ValueError, match="integrity"):
        _verified_tool_arguments(agent_id, details)


@pytest.mark.asyncio
async def test_execute_code_is_forced_to_l3_even_if_agent_policy_requests_l1(
    monkeypatch,
):
    from app import config as config_module

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(CODE_EXECUTION_REQUIRE_APPROVAL=True),
    )

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    service = AutonomyService()

    async def no_notification(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_request_approval", no_notification)
    db = FakeDB()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        autonomy_policy={"execute_code": "L1"},
    )

    result = await service.check_and_enforce(
        db,
        agent,
        "execute_code",
        {"tool": "execute_code", "args": {"code": "print(1)"}},
    )

    assert result["allowed"] is False
    assert result["level"] == "L3"
    assert any(value.__class__.__name__ == "ApprovalRequest" for value in db.added)


def test_masked_or_omitted_secret_never_overwrites_stored_value():
    schema = {"fields": [{"key": "api_key", "type": "password"}]}
    existing = {"api_key": "stored-secret", "timeout": 30}

    assert merge_config_preserving_sensitive(
        existing,
        {"api_key": "****cret", "timeout": 60},
        schema,
    ) == {"api_key": "stored-secret", "timeout": 60}
    assert merge_config_preserving_sensitive(
        existing,
        {"timeout": 60},
        schema,
    ) == {"api_key": "stored-secret", "timeout": 60}
    assert merge_config_preserving_sensitive(existing, {}, schema) == {}


def test_cross_tenant_tool_target_requires_platform_admin():
    own_tenant = uuid.uuid4()
    foreign_tenant = uuid.uuid4()
    org_admin = SimpleNamespace(
        tenant_id=own_tenant,
        role="org_admin",
        identity=None,
    )
    platform_admin = SimpleNamespace(
        tenant_id=own_tenant,
        role="platform_admin",
        identity=None,
    )

    with pytest.raises(HTTPException) as denied:
        tools_api._resolve_target_tenant_id(org_admin, str(foreign_tenant))
    assert denied.value.status_code == 403
    assert (
        tools_api._resolve_target_tenant_id(
            platform_admin,
            str(foreign_tenant),
        )
        == foreign_tenant
    )


def test_only_platform_admin_can_enable_code_network_access():
    tool = SimpleNamespace(name="execute_code")
    org_admin = SimpleNamespace(
        tenant_id=uuid.uuid4(),
        role="org_admin",
        identity=None,
    )
    platform_admin = SimpleNamespace(
        tenant_id=org_admin.tenant_id,
        role="platform_admin",
        identity=None,
    )

    with pytest.raises(HTTPException) as denied:
        tools_api._enforce_code_network_permission(
            org_admin,
            tool,
            {"allow_network": True},
        )
    assert denied.value.status_code == 403
    tools_api._enforce_code_network_permission(
        platform_admin,
        tool,
        {"allow_network": True},
    )


def test_mcp_url_credentials_are_masked_and_preserved_on_round_trip():
    stored = "https://user:password@mcp.example.test/api?apiKey=super-secret&mode=sse#token"

    masked = tools_api._mask_mcp_server_url(stored)

    assert "password" not in masked
    assert "super-secret" not in masked
    assert masked == "https://****@mcp.example.test/api?apiKey=%2A%2A%2A%2A&mode=sse#****"
    assert tools_api._merge_masked_mcp_server_url(stored, masked) == stored


@pytest.mark.asyncio
async def test_manage_endpoint_rejects_use_only_agent_access(monkeypatch):
    from app.core import permissions

    async def use_only(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4()), "use"

    monkeypatch.setattr(permissions, "check_agent_access", use_only)

    with pytest.raises(HTTPException) as denied:
        await tools_api._require_agent_tool_access(
            SimpleNamespace(),
            SimpleNamespace(),
            uuid.uuid4(),
            manage=True,
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_runtime_rechecks_explicit_agent_code_assignment(monkeypatch):
    from app import config as config_module

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    settings = _settings(
        CODE_EXECUTION_ENABLED=True,
        CODE_EXECUTION_ALLOWED_TENANT_IDS=str(tenant_id),
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    async def get_tenant(_agent_id):
        return str(tenant_id)

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", get_tenant)

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self, value):
            self.value = value

        async def execute(self, _query):
            return Result(self.value)

    class SessionContext:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return FakeDB(self.value)

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: SessionContext(None),
    )
    assert "not authorized for this Agent" in await agent_tools._code_tool_denial_reason(
        "execute_code",
        agent_id,
    )

    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: SessionContext(uuid.uuid4()),
    )
    assert (
        await agent_tools._code_tool_denial_reason(
            "execute_code",
            agent_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_agent_tool_config_response_masks_agent_and_company_secrets(
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    schema = {"fields": [{"key": "api_key", "type": "password"}]}
    tool = SimpleNamespace(
        id=tool_id,
        source="builtin",
        tenant_id=None,
        config_schema=schema,
    )
    assignment = SimpleNamespace(config={"api_key": "agent-secret"})

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self):
            self.results = iter((Result(tool), Result(assignment)))

        async def execute(self, _query):
            return next(self.results)

    async def allow_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def no_assignments(*_args, **_kwargs):
        return {}

    async def company_config(*_args, **_kwargs):
        return {"api_key": "company-secret"}

    monkeypatch.setattr(tools_api, "_require_agent_tool_access", allow_access)
    monkeypatch.setattr(tools_api, "_load_agent_tool_assignments", no_assignments)
    monkeypatch.setattr(tools_api, "get_tool_company_config", company_config)

    response = await tools_api.get_agent_tool_config(
        agent_id,
        tool_id,
        SimpleNamespace(),
        FakeDB(),
    )

    serialized = str(response)
    assert "agent-secret" not in serialized
    assert "company-secret" not in serialized
    assert response["agent_config"]["api_key"].startswith("****")
    assert response["global_config"]["api_key"].startswith("****")
