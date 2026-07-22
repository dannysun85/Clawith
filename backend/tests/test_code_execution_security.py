import shlex
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
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
    CODE_EXECUTION_TOOL_NAMES,
    code_execution_denial_reason,
    code_execution_tenant_authorized,
)
from app.services.sandbox.api.e2b_backend import _build_e2b_command
from app.services.sandbox.config import SandboxConfig
from app.services.tool_config import merge_config_preserving_sensitive


ROOT = Path(__file__).resolve().parents[2]


def _settings(**overrides):
    values = {
        "CODE_EXECUTION_ENABLED": False,
        "CODE_EXECUTION_ALLOWED_TENANT_IDS": "",
        "CODE_EXECUTION_ALLOWED_TOOL_NAMES": "execute_code,execute_code_e2b",
        "CODE_EXECUTION_ALLOWED_SANDBOX_TYPES": "e2b",
        "CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS": "",
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
        tool_name="execute_code",
        sandbox_type="subprocess",
    )
    assert "not approved" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name="execute_code_e2b",
        sandbox_type="unknown-provider",
    )
    assert (
        code_execution_denial_reason(
            settings,
            tenant_id,
            tool_name="execute_code_e2b",
            sandbox_type="e2b",
            allow_network=True,
        )
        is None
    )
    assert "network approval" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name="execute_code_e2b",
        sandbox_type="e2b",
        allow_network=False,
    )


def test_production_code_requires_exact_tool_provider_and_endpoint_grants():
    tenant_id = uuid.uuid4()
    settings = _settings(
        CODE_EXECUTION_ENABLED=True,
        CODE_EXECUTION_ALLOWED_TENANT_IDS=str(tenant_id),
        CODE_EXECUTION_ALLOWED_TOOL_NAMES="execute_code_e2b,agentbay_code_execute",
        CODE_EXECUTION_ALLOWED_SANDBOX_TYPES="e2b,self_hosted,agentbay",
        CODE_EXECUTION_ALLOWED_SANDBOX_ENDPOINTS="https://sandbox.example.test/api",
    )

    assert "not approved" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name="agentbay_command_exec",
    )
    assert "cannot be rerouted" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name="execute_code_e2b",
        sandbox_type="self_hosted",
        allow_network=True,
        api_url="https://sandbox.example.test/api",
    )
    assert "endpoint is not approved" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name=None,
        sandbox_type="self_hosted",
        allow_network=True,
        api_url="http://169.254.169.254/latest/meta-data",
    )
    assert "endpoint is not approved" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name=None,
        sandbox_type="self_hosted",
        allow_network=True,
        api_url="https://user:secret@sandbox.example.test/api?token=x",
    )
    assert code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name=None,
        sandbox_type="self_hosted",
        allow_network=True,
        api_url="https://sandbox.example.test/api/",
    ) is None
    assert "network approval" in code_execution_denial_reason(
        settings,
        tenant_id,
        tool_name="agentbay_code_execute",
        sandbox_type="agentbay",
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
        lambda: SimpleNamespace(
            CODE_EXECUTION_REQUIRE_APPROVAL=True,
            SECRET_KEY="approval-test-secret",
        ),
    )

    class NestedTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def begin_nested(self):
            return NestedTransaction()

    service = AutonomyService()

    async def no_notification(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_request_approval", no_notification)
    db = FakeDB()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        autonomy_policy={"execute_code": "L1"},
    )
    details = build_tool_approval_details(
        agent.id,
        "execute_code",
        "execute_code",
        {"code": "print(1)"},
        uuid.uuid4(),
    )

    result = await service.check_and_enforce(
        db,
        agent,
        "execute_code",
        details,
    )

    assert result["allowed"] is False
    assert result["level"] == "L3"
    assert any(value.__class__.__name__ == "ApprovalRequest" for value in db.added)


@pytest.mark.asyncio
async def test_approval_feishu_card_is_never_sent_before_durable_commit(
    monkeypatch,
):
    from app.services import notification_service
    from app.services import autonomy_service as autonomy_module

    events = []

    async def persist_notification(*_args, **_kwargs):
        events.append("web_notification")
        return SimpleNamespace()

    async def send_card(*_args, **_kwargs):
        events.append("feishu_card")

    class FailingCommitDB:
        async def commit(self):
            events.append("commit")
            raise RuntimeError("commit failed")

    monkeypatch.setattr(notification_service, "send_notification", persist_notification)
    monkeypatch.setattr(
        autonomy_module.feishu_service,
        "send_approval_card",
        send_card,
    )
    service = AutonomyService()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="Approval Commit Test",
    )
    details = build_tool_approval_details(
        agent.id,
        "write_workspace_files",
        "write_file",
        {"path": "workspace/test.txt", "content": "test"},
        agent.creator_id,
    )
    approval = SimpleNamespace(
        id=uuid.uuid4(),
        action_type="write_workspace_files",
        details=details,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await service._request_approval(FailingCommitDB(), agent, approval)

    assert events == ["web_notification", "commit"]


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


def test_only_platform_admin_can_modify_code_isolation_controls():
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
        tools_api._enforce_code_control_permission(
            org_admin,
            tool,
            {"sandbox_type": "self_hosted", "allow_network": False},
        )
    assert denied.value.status_code == 403
    tools_api._enforce_code_control_permission(
        platform_admin,
        tool,
        {"sandbox_type": "e2b", "allow_network": True},
    )


def test_sandbox_config_accepts_runtime_plaintext_api_key():
    fallback = SandboxConfig(api_key="environment-fallback")

    resolved = SandboxConfig.from_dict(
        {"sandbox_type": "e2b", "api_key": "tenant-runtime-plaintext"},
        fallback,
    )

    assert resolved.api_key == "tenant-runtime-plaintext"


def test_every_code_tool_is_mapped_to_l3_autonomy_action():
    assert {
        name for name in CODE_EXECUTION_TOOL_NAMES
        if agent_tools._TOOL_AUTONOMY_MAP.get(name) != "execute_code"
    } == set()


def test_l3_approval_message_matches_enabled_execution_worker(monkeypatch):
    approval_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(APPROVAL_EXECUTION_ENABLED=True),
    )

    message = agent_tools._queued_approval_message(approval_id)

    assert "secure worker will execute the signed action once" in message
    assert "Do not retry this tool call" in message
    assert "No side effect has completed yet" in message
    assert str(approval_id) in message
    assert "Please wait for approval before retrying" not in message


def test_l3_approval_message_reports_paused_worker(monkeypatch):
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(APPROVAL_EXECUTION_ENABLED=False),
    )

    message = agent_tools._queued_approval_message(uuid.uuid4())

    assert "automatic execution is paused" in message
    assert "No code, command, or side effect will run after approval" in message


@pytest.mark.asyncio
async def test_direct_approval_dispatch_supports_agentbay_file_helpers(
    monkeypatch,
    tmp_path,
):
    async def no_denial(*_args, **_kwargs):
        return None

    async def tenant_id(*_args, **_kwargs):
        return str(uuid.uuid4())

    async def write_file(agent_id, workspace, arguments):
        return f"wrote:{agent_id}:{workspace}:{arguments['path']}"

    @asynccontextmanager
    async def no_control_lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(agent_tools, "_code_tool_denial_reason", no_denial)
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_id)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_tools, "_agentbay_code_write_file", write_file)
    monkeypatch.setattr(
        "app.services.agentbay_control_lock.agentbay_tool_execution_lease",
        no_control_lock,
    )

    result = await agent_tools._execute_tool_direct(
        "agentbay_code_write_file",
        {"path": "script.py", "content": "print(1)"},
        uuid.uuid4(),
    )

    assert "script.py" in result


def test_startup_seeder_never_auto_assigns_code_helpers():
    source = (ROOT / "backend/app/services/tool_seeder.py").read_text(
        encoding="utf-8"
    )
    auto_assignment_region = source[
        source.index("# AgentBay desktop window helpers") :
        source.index("OBSOLETE_TOOLS")
    ]

    assert "agentbay_file_transfer" not in auto_assignment_region
    assert "agentbay_code_" not in auto_assignment_region
    assert "agentbay_command_exec" not in auto_assignment_region


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

    async def get_config(_agent_id, _tool_name):
        return {
            "sandbox_type": "e2b",
            "api_key": "secret",
            "allow_network": True,
        }

    monkeypatch.setattr(agent_tools, "_get_tool_config", get_config)

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
        "execute_code_e2b",
        agent_id,
    )

    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: SessionContext(uuid.uuid4()),
    )
    assert (
        await agent_tools._code_tool_denial_reason(
            "execute_code_e2b",
            agent_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_typed_code_execution_blocks_before_autonomy_and_dispatch(monkeypatch):
    async def deny(_tool_name, _agent_id):
        return "Code execution is not authorized for this Agent"

    async def forbidden_autonomy(**_kwargs):
        raise AssertionError("autonomy must not run for an unauthorized Code tool")

    monkeypatch.setattr(agent_tools, "_code_tool_denial_reason", deny)
    monkeypatch.setattr(
        agent_tools,
        "enforce_builtin_tool_autonomy_outcome",
        forbidden_autonomy,
    )

    outcome = await agent_tools.execute_builtin_tool_outcome(
        "execute_code",
        {"language": "python", "code": "print('blocked')"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "code_execution_not_authorized"


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
        name="some_tool",
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
