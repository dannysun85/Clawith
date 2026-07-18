import uuid
from types import SimpleNamespace

import pytest

from app.api import tools as tools_api
from app.models.tool import Tool
from app.services import agent_tools, resource_discovery
from app.services.mcp_security import (
    MCPURLPolicyError,
    mcp_server_namespace,
    normalized_mcp_endpoint,
)
from app.services.tool_config import tenant_scoped_tool_name


class _Result:
    def __init__(self, *, scalar=None, rows=None, scalars=None):
        self._scalar = scalar
        self._rows = rows or []
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)


class _Session:
    def __init__(self, results):
        self._results = iter(results)
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, _query):
        return next(self._results)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def refresh(self, _value):
        return None


def _tool(*, tenant_id, name="mcp_search", source="admin"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source=source,
        name=name,
        mcp_tool_name="search",
        mcp_server_url="https://mcp.example.test/api",
        mcp_server_name="Example",
        parameters_schema={},
        config={},
        config_schema={},
    )


def test_tenant_tool_names_are_stable_global_and_builtin_safe():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    logical_name = "send_email"

    name_a = tenant_scoped_tool_name(logical_name, tenant_a)
    name_b = tenant_scoped_tool_name(logical_name, tenant_b)

    assert name_a == tenant_scoped_tool_name(logical_name, str(tenant_a))
    assert name_a != name_b
    assert name_a != logical_name
    assert len(name_a) <= 64
    assert tenant_scoped_tool_name(logical_name, None) == logical_name


def test_tenant_tool_names_include_server_namespace():
    tenant_id = uuid.uuid4()

    first = tenant_scoped_tool_name(
        "mcp_search",
        tenant_id,
        namespace="Server A",
    )
    second = tenant_scoped_tool_name(
        "mcp_search",
        tenant_id,
        namespace="Server B",
    )

    assert first != second
    assert first == tenant_scoped_tool_name(
        "mcp_search",
        tenant_id,
        namespace="Server A",
    )


def test_mcp_server_namespace_excludes_query_credentials():
    url = (
        "https://MCP.EXAMPLE.test:8443/api/"
        "?workspace=one&apiKey=secret"
    )

    assert normalized_mcp_endpoint(url) == (
        "https://mcp.example.test:8443/api?workspace=one"
    )
    assert mcp_server_namespace("Example", url) == (
        "name:example|endpoint:https://mcp.example.test:8443/api?workspace=one"
    )


def test_mcp_server_namespace_rejects_legacy_userinfo_and_fragments():
    with pytest.raises(MCPURLPolicyError):
        mcp_server_namespace(
            None,
            "https://user:password@mcp.example.test/api#token",
        )


def test_tenant_tool_names_do_not_collapse_long_equal_prefixes():
    tenant_id = uuid.uuid4()
    prefix = "mcp_" + ("a" * 120)

    first = tenant_scoped_tool_name(f"{prefix}_one", tenant_id)
    second = tenant_scoped_tool_name(f"{prefix}_two", tenant_id)

    assert first != second
    assert len(first) <= 64
    assert len(second) <= 64


@pytest.mark.asyncio
async def test_create_tool_uses_internal_tenant_name_without_shadowing_builtin(
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    db = _Session([_Result(scalar=None)])
    user = SimpleNamespace(tenant_id=tenant_id, role="org_admin", identity=None)
    payload = tools_api.ToolCreate(
        name="send_email",
        display_name="Tenant mail",
        type="mcp",
        mcp_server_url="https://mcp.example.test/api",
        mcp_server_name="Example",
        mcp_tool_name="send_email",
    )
    async def allow_public_url(url):
        return url

    monkeypatch.setattr(tools_api, "validate_public_mcp_url", allow_public_url)

    response = await tools_api.create_tool(payload, current_user=user, db=db)

    created = db.added[0]
    assert created.name == tenant_scoped_tool_name(
        "send_email",
        tenant_id,
        namespace=mcp_server_namespace(
            "Example",
            "https://mcp.example.test/api",
        ),
    )
    assert created.name != "send_email"
    assert created.tenant_id == tenant_id
    assert created.mcp_tool_name == "send_email"
    assert response["name"] == created.name


@pytest.mark.asyncio
async def test_global_mcp_create_rejects_query_credentials(monkeypatch):
    db = _Session([])
    user = SimpleNamespace(tenant_id=None, role="platform_admin", identity=None)
    payload = tools_api.ToolCreate(
        name="shared_search",
        display_name="Shared search",
        type="mcp",
        mcp_server_url="https://mcp.example.test/api?apiKey=company-secret",
        mcp_server_name="Example",
        mcp_tool_name="search",
    )

    async def allow_public_url(url):
        return url

    monkeypatch.setattr(tools_api, "validate_public_mcp_url", allow_public_url)

    with pytest.raises(Exception, match="Global MCP tools cannot contain credentials"):
        await tools_api.create_tool(payload, current_user=user, db=db)

    assert db.added == []


@pytest.mark.asyncio
async def test_direct_mcp_import_creates_company_scoped_internal_tool(monkeypatch):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_session = _Session([_Result(scalar=tenant_id)])
    import_session = _Session([
        _Result(scalar=agent_id),
        _Result(scalars=[]),
        _Result(scalar=None),
        _Result(),
        _Result(),
    ])
    sessions = iter([tenant_session, import_session])

    class _Client:
        def __init__(self, _url, api_key=None):
            assert api_key is None

        async def list_tools(self):
            return [{
                "name": "search",
                "description": "Search",
                "inputSchema": {"type": "object", "properties": {}},
            }]

    monkeypatch.setattr(mcp_client, "MCPClient", _Client)
    async def allow_public_url(url):
        return url

    monkeypatch.setattr(
        resource_discovery,
        "validate_public_mcp_url",
        allow_public_url,
    )
    monkeypatch.setattr(
        resource_discovery,
        "async_session",
        lambda: next(sessions),
    )

    result = await resource_discovery.import_mcp_direct(
        "https://mcp.example.test/api",
        agent_id,
        server_name="Example",
    )

    created = next(item for item in import_session.added if isinstance(item, Tool))
    assert created.tenant_id == tenant_id
    assert created.name == tenant_scoped_tool_name(
        "mcp_Example_search",
        tenant_id,
        namespace=mcp_server_namespace(
            "Example",
            "https://mcp.example.test/api",
        ),
    )
    assert created.mcp_tool_name == "search"
    assert "Imported MCP server" in result


@pytest.mark.asyncio
async def test_direct_mcp_import_without_named_catalog_fails_closed(monkeypatch):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_session = _Session([_Result(scalar=tenant_id)])
    import_session = _Session([
        _Result(scalar=agent_id),
        _Result(scalars=[]),
    ])
    sessions = iter([tenant_session, import_session])

    class _Client:
        def __init__(self, _url, api_key=None):
            assert api_key == "secret"

        async def list_tools(self):
            return []

    async def allow_public_url(url):
        return url

    monkeypatch.setattr(mcp_client, "MCPClient", _Client)
    monkeypatch.setattr(
        resource_discovery,
        "validate_public_mcp_url",
        allow_public_url,
    )
    monkeypatch.setattr(
        resource_discovery,
        "async_session",
        lambda: next(sessions),
    )

    result = await resource_discovery.import_mcp_direct(
        "https://mcp.example.test/api",
        agent_id,
        server_name="Example",
        api_key="secret",
    )

    assert result.startswith("❌ MCP import stopped")
    assert "No executable tool was created or enabled" in result
    assert tenant_session.added == []
    assert import_session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_existing_named_tool", [False, True])
async def test_smithery_live_empty_catalog_never_creates_registry_only_tools(
    monkeypatch,
    has_existing_named_tool,
):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    base_mcp_url = "https://example.run.tools"
    existing = _tool(
        tenant_id=tenant_id,
        name="mcp_existing_named",
        source="agent",
    )
    existing.mcp_server_url = base_mcp_url
    existing.enabled = True
    key_session = _Session([
        _Result(scalar=None),
        _Result(scalar=None),
    ])
    import_session = _Session([
        _Result(scalars=[existing] if has_existing_named_tool else []),
    ])
    sessions = iter([key_session, import_session])

    class _HTTPClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    registry_responses = iter([
        (
            200,
            {
                "servers": [{
                    "qualifiedName": "vendor/example",
                    "displayName": "Example",
                    "remote": True,
                }],
            },
        ),
        (
            200,
            {
                "deploymentUrl": base_mcp_url,
                "tools": [{
                    "name": "registry_only",
                    "description": "Stale registry entry",
                    "inputSchema": {"type": "object"},
                }],
            },
        ),
    ])

    async def bounded_registry_request(*_args, **_kwargs):
        return next(registry_responses)

    async def agent_tenant(_agent_id):
        return tenant_id

    async def allow_public_url(url):
        return url

    async def ready_connection(*_args):
        return {"namespace": "astra", "connection_id": "example"}

    async def no_op(*_args, **_kwargs):
        return None

    async def no_legacy(*_args, **_kwargs):
        return {}

    class _MCPClient:
        def __init__(self, _url, api_key=None):
            assert api_key == "smithery-secret"

        async def list_tools(self):
            return []

    monkeypatch.setattr(resource_discovery.httpx, "AsyncClient", _HTTPClient)
    monkeypatch.setattr(
        resource_discovery,
        "_bounded_json_request",
        bounded_registry_request,
    )
    monkeypatch.setattr(resource_discovery, "_get_agent_tenant_id", agent_tenant)
    monkeypatch.setattr(resource_discovery, "validate_public_mcp_url", allow_public_url)
    monkeypatch.setattr(resource_discovery, "_ensure_smithery_connection", ready_connection)
    monkeypatch.setattr(resource_discovery, "lock_agent_tool_owner", no_op)
    monkeypatch.setattr(resource_discovery, "_lock_tenant_mcp_import", no_op)
    monkeypatch.setattr(
        resource_discovery,
        "_quarantine_legacy_generic_mcp_tools",
        no_legacy,
    )
    monkeypatch.setattr(resource_discovery, "async_session", lambda: next(sessions))
    monkeypatch.setattr(mcp_client, "MCPClient", _MCPClient)

    result = await resource_discovery.import_mcp_from_smithery(
        "vendor/example",
        agent_id,
        config={"smithery_api_key": "smithery-secret"},
    )

    assert import_session.added == []
    assert "registry_only" not in result
    if has_existing_named_tool:
        assert result.startswith("⚠️")
        assert "previously discovered named tool" in result
        assert existing.enabled is True
        assert existing.mcp_server_url == base_mcp_url
    else:
        assert result.startswith("❌ MCP import stopped")
        assert "No executable tool was created or enabled" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_state", ["failed", "authorization_pending"])
async def test_smithery_unready_connection_preserves_named_tool_state(
    monkeypatch,
    connection_state,
):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    existing = _tool(
        tenant_id=tenant_id,
        name="mcp_existing_named",
        source="agent",
    )
    existing.enabled = True
    original_state = (
        existing.enabled,
        existing.mcp_server_url,
        dict(existing.config),
    )
    key_session = _Session([_Result(scalar=None), _Result(scalar=None)])
    import_session = _Session([])
    sessions = iter([key_session, import_session])

    class _HTTPClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    registry_responses = iter([
        (
            200,
            {
                "servers": [{
                    "qualifiedName": "vendor/example",
                    "displayName": "Example",
                    "remote": True,
                }],
            },
        ),
        (
            200,
            {
                "deploymentUrl": "https://example.run.tools",
                "tools": [{"name": "registry_tool", "inputSchema": {}}],
            },
        ),
    ])

    async def bounded_registry_request(*_args, **_kwargs):
        return next(registry_responses)

    async def agent_tenant(_agent_id):
        return tenant_id

    async def allow_public_url(url):
        return url

    async def connection_result(*_args):
        if connection_state == "failed":
            return {"error": "provider unavailable"}
        return {
            "namespace": "astra",
            "connection_id": "authorization-pending",
            "auth_url": "https://smithery.example/authorize",
        }

    async def no_op(*_args, **_kwargs):
        return None

    async def no_legacy(*_args, **_kwargs):
        return {}

    async def unexpected_upsert(*_args, **_kwargs):
        raise AssertionError("an unready connection must not write named tool state")

    class _MCPClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self):
            raise RuntimeError("authorization is not complete")

    monkeypatch.setattr(resource_discovery.httpx, "AsyncClient", _HTTPClient)
    monkeypatch.setattr(
        resource_discovery,
        "_bounded_json_request",
        bounded_registry_request,
    )
    monkeypatch.setattr(resource_discovery, "_get_agent_tenant_id", agent_tenant)
    monkeypatch.setattr(resource_discovery, "validate_public_mcp_url", allow_public_url)
    monkeypatch.setattr(resource_discovery, "_ensure_smithery_connection", connection_result)
    monkeypatch.setattr(resource_discovery, "lock_agent_tool_owner", no_op)
    monkeypatch.setattr(resource_discovery, "_lock_tenant_mcp_import", no_op)
    monkeypatch.setattr(
        resource_discovery,
        "_quarantine_legacy_generic_mcp_tools",
        no_legacy,
    )
    monkeypatch.setattr(resource_discovery, "upsert_agent_tool", unexpected_upsert)
    monkeypatch.setattr(resource_discovery, "async_session", lambda: next(sessions))
    monkeypatch.setattr(mcp_client, "MCPClient", _MCPClient)

    result = await resource_discovery.import_mcp_from_smithery(
        "vendor/example",
        agent_id,
        config={"smithery_api_key": "smithery-secret"},
    )

    assert "left unchanged" in result
    assert import_session.added == []
    assert (
        existing.enabled,
        existing.mcp_server_url,
        existing.config,
    ) == original_state


@pytest.mark.asyncio
async def test_legacy_generic_mcp_is_quarantined_without_deleting_config():
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    generic = SimpleNamespace(
        id=uuid.uuid4(),
        enabled=True,
        mcp_server_name="Example",
        mcp_server_url="https://mcp.example.test/api",
    )
    assignment = SimpleNamespace(config={"api_key": "legacy-secret"})
    db = _Session([
        _Result(scalars=[generic]),
        _Result(scalars=[assignment]),
        _Result(),
    ])

    recovered = await resource_discovery._quarantine_legacy_generic_mcp_tools(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        server_namespace=mcp_server_namespace(
            "Example",
            "https://mcp.example.test/api",
        ),
        server_name="Example",
    )

    assert generic.enabled is False
    assert recovered == {"api_key": "legacy-secret"}


@pytest.mark.asyncio
async def test_mcp_runtime_rejects_foreign_tenant_assignment(monkeypatch):
    own_tenant = uuid.uuid4()
    foreign_tool = _tool(tenant_id=uuid.uuid4())
    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: _Session([
            _Result(scalar=own_tenant),
            _Result(rows=[(foreign_tool, {"api_key": "foreign-secret"})]),
        ]),
    )

    result = await agent_tools._execute_mcp_tool(
        foreign_tool.name,
        {},
        agent_id=uuid.uuid4(),
    )

    assert result == "❌ MCP tool is unavailable for this Agent"
    assert "foreign-secret" not in result


@pytest.mark.asyncio
async def test_mcp_runtime_requires_one_enabled_assigned_match(monkeypatch):
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: _Session([
            _Result(scalar=tenant_id),
            _Result(rows=[]),
        ]),
    )

    result = await agent_tools._execute_mcp_tool(
        "mcp_unassigned",
        {},
        agent_id=uuid.uuid4(),
    )

    assert result == "❌ MCP tool is unavailable for this Agent"


@pytest.mark.asyncio
async def test_mcp_runtime_rejects_agent_owned_legacy_generic(monkeypatch):
    tenant_id = uuid.uuid4()
    generic = _tool(
        tenant_id=tenant_id,
        name="mcp_example_internal_tenant_name",
        source="agent",
    )
    generic.mcp_tool_name = None
    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: _Session([
            _Result(scalar=tenant_id),
            _Result(rows=[(generic, {"api_key": "preserved-secret"})]),
        ]),
    )

    result = await agent_tools._execute_mcp_tool(
        generic.name,
        {},
        agent_id=uuid.uuid4(),
    )

    assert result == "❌ MCP tool is unavailable for this Agent"
    assert "preserved-secret" not in result


@pytest.mark.asyncio
async def test_mcp_runtime_uses_only_own_assigned_tool(monkeypatch):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    own_tool = _tool(tenant_id=tenant_id)
    calls = []

    class _Client:
        def __init__(self, url, api_key=None):
            calls.append((url, api_key))

        async def call_tool_result(self, name, arguments):
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"called:{name}:{arguments['q']}",
                        }
                    ]
                }
            }

    monkeypatch.setattr(mcp_client, "MCPClient", _Client)
    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: _Session([
            _Result(scalar=tenant_id),
            _Result(rows=[(own_tool, {"api_key": "own-key"})]),
        ]),
    )

    result = await agent_tools._execute_mcp_tool(
        own_tool.name,
        {"q": "hello"},
        agent_id=uuid.uuid4(),
    )

    assert result == "✅ called:search:hello"
    assert calls == [(own_tool.mcp_server_url, "own-key")]


@pytest.mark.asyncio
async def test_global_mcp_runtime_ignores_legacy_shared_credential(monkeypatch):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    shared_tool = _tool(tenant_id=None)
    shared_tool.config = {"api_key": "foreign-legacy-key"}
    calls = []

    class _Client:
        def __init__(self, url, api_key=None):
            calls.append((url, api_key))

        async def call_tool_result(self, name, arguments):
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"called:{name}:{arguments['q']}",
                        }
                    ]
                }
            }

    monkeypatch.setattr(mcp_client, "MCPClient", _Client)
    monkeypatch.setattr(
        agent_tools,
        "async_session",
        lambda: _Session([
            _Result(scalar=tenant_id),
            _Result(rows=[(shared_tool, {"api_key": "own-agent-key"})]),
        ]),
    )

    result = await agent_tools._execute_mcp_tool(
        shared_tool.name,
        {"q": "hello"},
        agent_id=uuid.uuid4(),
    )

    assert result == "✅ called:search:hello"
    assert calls == [(shared_tool.mcp_server_url, "own-agent-key")]


def test_mcp_server_literal_route_rejects_legacy_userinfo_and_fragment():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    tenant_id = uuid.uuid4()
    schema = {"fields": [{"key": "api_key", "type": "password"}]}
    stored_url = "https://user:password@mcp.example.test/api?apiKey=url-secret#token"
    tool = SimpleNamespace(
        tenant_id=tenant_id,
        mcp_server_url=stored_url,
        config_schema=schema,
        config=tools_api._encrypt_sensitive_fields(
            {"api_key": "stored-api-secret"},
            schema,
        ),
    )
    db = _Session([_Result(scalars=[tool])])
    user = SimpleNamespace(
        tenant_id=tenant_id,
        role="org_admin",
        identity=None,
    )

    async def current_admin():
        return user

    async def current_db():
        yield db

    app = FastAPI()
    app.include_router(tools_api.router)
    app.dependency_overrides[tools_api.get_current_admin] = current_admin
    app.dependency_overrides[tools_api.get_db] = current_db

    with TestClient(app) as client:
        response = client.put(
            "/tools/mcp-server",
            json={
                "server_name": "Example",
                "server_url": tools_api._mask_mcp_server_url(stored_url),
                "api_key": "****cret",
            },
        )

    assert response.status_code == 400
    assert tool.mcp_server_url == stored_url
    assert tools_api._decrypt_sensitive_fields(tool.config, schema)["api_key"] == "stored-api-secret"
