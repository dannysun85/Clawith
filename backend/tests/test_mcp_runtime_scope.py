import uuid
from types import SimpleNamespace

import pytest

from app.api import tools as tools_api
from app.models.tool import Tool
from app.services import agent_tools, resource_discovery
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


def test_tenant_tool_names_do_not_collapse_long_equal_prefixes():
    tenant_id = uuid.uuid4()
    prefix = "mcp_" + ("a" * 120)

    first = tenant_scoped_tool_name(f"{prefix}_one", tenant_id)
    second = tenant_scoped_tool_name(f"{prefix}_two", tenant_id)

    assert first != second
    assert len(first) <= 64
    assert len(second) <= 64


@pytest.mark.asyncio
async def test_create_tool_uses_internal_tenant_name_without_shadowing_builtin():
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

    response = await tools_api.create_tool(payload, current_user=user, db=db)

    created = db.added[0]
    assert created.name == tenant_scoped_tool_name("send_email", tenant_id)
    assert created.name != "send_email"
    assert created.tenant_id == tenant_id
    assert created.mcp_tool_name == "send_email"
    assert response["name"] == created.name


@pytest.mark.asyncio
async def test_direct_mcp_import_creates_company_scoped_internal_tool(monkeypatch):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    tenant_session = _Session([_Result(scalar=tenant_id)])
    import_session = _Session([
        _Result(scalar=None),
        _Result(scalar=None),
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
    monkeypatch.setattr(
        resource_discovery,
        "async_session",
        lambda: next(sessions),
    )

    result = await resource_discovery.import_mcp_direct(
        "https://mcp.example.test/api",
        uuid.uuid4(),
        server_name="Example",
    )

    created = next(item for item in import_session.added if isinstance(item, Tool))
    assert created.tenant_id == tenant_id
    assert created.name == tenant_scoped_tool_name("mcp_Example_search", tenant_id)
    assert created.mcp_tool_name == "search"
    assert "Imported MCP server" in result


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
async def test_mcp_runtime_uses_only_own_assigned_tool(monkeypatch):
    from app.services import mcp_client

    tenant_id = uuid.uuid4()
    own_tool = _tool(tenant_id=tenant_id)
    calls = []

    class _Client:
        def __init__(self, url, api_key=None):
            calls.append((url, api_key))

        async def call_tool(self, name, arguments):
            return f"called:{name}:{arguments['q']}"

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

    assert result == "called:search:hello"
    assert calls == [(own_tool.mcp_server_url, "own-key")]


def test_mcp_server_literal_route_preserves_masked_url_and_api_key():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    tenant_id = uuid.uuid4()
    schema = {"fields": [{"key": "api_key", "type": "password"}]}
    stored_url = "https://user:password@mcp.example.test/api?apiKey=url-secret"
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

    assert response.status_code == 200
    assert response.json() == {"ok": True, "updated": 1}
    assert tool.mcp_server_url == stored_url
    assert tools_api._decrypt_sensitive_fields(tool.config, schema)["api_key"] == "stored-api-secret"
