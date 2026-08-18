import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.tools import (
    _authorize_tool_record,
    _require_platform_admin,
    _resolve_target_tenant_id,
    _tool_record_visible_to_agent,
)


def make_tool(**overrides):
    values = {
        "id": uuid.uuid4(),
        "source": "builtin",
        "tenant_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_builtin_tools_are_visible_across_tenants():
    tenant_id = uuid.uuid4()
    tool = make_tool(source="builtin", tenant_id=None)

    assert _tool_record_visible_to_agent(tool, tenant_id, {}) is True


def test_admin_tools_are_visible_only_to_same_tenant():
    tenant_id = uuid.uuid4()
    foreign_tenant_id = uuid.uuid4()
    same_tenant_tool = make_tool(source="admin", tenant_id=tenant_id)
    foreign_tool = make_tool(source="admin", tenant_id=foreign_tenant_id)

    assert _tool_record_visible_to_agent(same_tenant_tool, tenant_id, {}) is True
    assert _tool_record_visible_to_agent(foreign_tool, tenant_id, {}) is False


def test_assignment_cannot_expose_foreign_admin_tool():
    tenant_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    foreign_tool = make_tool(
        source="admin",
        id=tool_id,
        tenant_id=uuid.uuid4(),
    )

    assert (
        _tool_record_visible_to_agent(
            foreign_tool,
            tenant_id,
            {str(tool_id): object()},
        )
        is False
    )


def test_agent_installed_tools_require_assignment_and_exact_tenant():
    tenant_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    installed_tool = make_tool(source="agent", id=tool_id, tenant_id=tenant_id)
    foreign_tool = make_tool(
        source="agent",
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )
    ownerless_tool = make_tool(source="agent", id=uuid.uuid4(), tenant_id=None)

    assert _tool_record_visible_to_agent(installed_tool, tenant_id, {}) is False
    assert _tool_record_visible_to_agent(installed_tool, tenant_id, {str(tool_id): object()}) is True
    assert _tool_record_visible_to_agent(
        foreign_tool,
        tenant_id,
        {str(foreign_tool.id): object()},
    ) is False
    assert _tool_record_visible_to_agent(
        ownerless_tool,
        tenant_id,
        {str(ownerless_tool.id): object()},
    ) is False


def make_user(tenant_id: uuid.UUID | None, role: str = "member", platform_identity: bool = False):
    return SimpleNamespace(
        tenant_id=tenant_id,
        role=role,
        identity=SimpleNamespace(is_platform_admin=platform_identity),
    )


def test_regular_users_cannot_select_a_tenant_for_tool_management():
    # A plain member is not a company governor, so even their own tenant is rejected.
    user = make_user(uuid.uuid4())

    with pytest.raises(HTTPException, match="Company governance access required") as error:
        _resolve_target_tenant_id(user, None)

    assert error.value.status_code == 403


def test_company_admin_cannot_select_another_tenant_for_tools():
    user = make_user(uuid.uuid4(), role="org_admin")

    with pytest.raises(HTTPException, match="Cross-tenant tool access is not allowed") as error:
        _resolve_target_tenant_id(user, str(uuid.uuid4()))

    assert error.value.status_code == 403


def test_platform_operator_can_use_the_global_tool_scope():
    user = make_user(None, platform_identity=True)

    assert _resolve_target_tenant_id(user, None) is None


def test_company_admin_cannot_mutate_a_foreign_tenant_tool():
    tenant_id = uuid.uuid4()
    user = make_user(tenant_id, role="org_admin")
    foreign_tool = make_tool(source="admin", tenant_id=uuid.uuid4())

    with pytest.raises(HTTPException, match="Tool tenant access denied") as error:
        _authorize_tool_record(user, foreign_tool, tenant_id)

    assert error.value.status_code == 403


def test_company_admin_cannot_mutate_the_platform_tool_scope():
    user = make_user(uuid.uuid4(), role="org_admin")

    with pytest.raises(HTTPException, match="Platform admin access required") as error:
        _require_platform_admin(user)

    assert error.value.status_code == 403
