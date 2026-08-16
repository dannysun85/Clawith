import uuid
from types import SimpleNamespace

import pytest

from app.services.product_roles import (
    classify_agent_product_roles,
    project_legacy_assistant_disposition,
    resolve_agent_product_roles,
)


class _Result:
    def __init__(self, values):
        self.values = list(values)

    def scalar_one_or_none(self):
        if len(self.values) > 1:
            raise AssertionError("expected at most one row")
        return self.values[0] if self.values else None

    def scalars(self):
        return self

    def all(self):
        return self.values


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("unexpected database query")
        return _Result(self.results.pop(0))


def _agent(
    *,
    agent_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
    is_system: bool = False,
    legacy_assistant_state: str | None = None,
):
    return SimpleNamespace(
        id=agent_id or uuid.uuid4(),
        template_id=template_id,
        is_system=is_system,
        legacy_assistant_state=legacy_assistant_state,
    )


def test_product_roles_keep_current_and_historical_assistants_out_of_employee_roster() -> None:
    assistant_template_id = uuid.uuid4()
    current = _agent(template_id=assistant_template_id)
    historical = _agent(template_id=assistant_template_id)
    employee = _agent()

    roles = classify_agent_product_roles(
        [current, historical, employee],
        personal_assistant_agent_id=current.id,
        private_assistant_template_ids={assistant_template_id},
    )

    assert roles == {
        current.id: "personal_assistant",
        historical.id: "legacy_personal_assistant",
        employee.id: "agent_employee",
    }


def test_onboarding_relation_wins_over_template_and_system_agents_are_not_legacy() -> None:
    assistant_template_id = uuid.uuid4()
    current_without_template = _agent()
    system_with_template = _agent(template_id=assistant_template_id, is_system=True)

    roles = classify_agent_product_roles(
        [current_without_template, system_with_template],
        personal_assistant_agent_id=current_without_template.id,
        private_assistant_template_ids={assistant_template_id},
    )

    assert roles[current_without_template.id] == "personal_assistant"
    assert roles[system_with_template.id] == "agent_employee"


def test_explicit_conversion_moves_legacy_assistant_into_employee_roster() -> None:
    assistant_template_id = uuid.uuid4()
    converted = _agent(
        template_id=assistant_template_id,
        legacy_assistant_state="converted",
    )
    archived = _agent(
        template_id=assistant_template_id,
        legacy_assistant_state="archived",
    )

    roles = classify_agent_product_roles(
        [converted, archived],
        personal_assistant_agent_id=None,
        private_assistant_template_ids={assistant_template_id},
    )

    assert roles[converted.id] == "agent_employee"
    assert roles[archived.id] == "legacy_personal_assistant"
    assert project_legacy_assistant_disposition(
        converted,
        product_role=roles[converted.id],
    ) == "converted"
    assert project_legacy_assistant_disposition(
        archived,
        product_role=roles[archived.id],
    ) == "archived"


@pytest.mark.asyncio
async def test_resolver_uses_onboarding_and_builtin_template_identity() -> None:
    assistant_template_id = uuid.uuid4()
    current = _agent(template_id=assistant_template_id)
    historical = _agent(template_id=assistant_template_id)
    employee = _agent()
    session = _Session([current.id], [assistant_template_id])

    roles = await resolve_agent_product_roles(
        session,
        viewer_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agents=[current, historical, employee],
    )

    assert len(session.statements) == 2
    assert roles[current.id] == "personal_assistant"
    assert roles[historical.id] == "legacy_personal_assistant"
    assert roles[employee.id] == "agent_employee"
