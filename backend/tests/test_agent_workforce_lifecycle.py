from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.dialects import postgresql

from app.api import agent_workforce as workforce_api
from app.api import agents as agents_api
from app.models.agent import AgentTemplate
from app.schemas.schemas import AgentCreate


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self._values

    def scalar_one_or_none(self):
        if len(self._values) > 1:
            raise AssertionError("expected at most one result")
        return self._values[0] if self._values else None


class _Session:
    def __init__(self, *results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected database query")
        return _Result(self._results.pop(0))


def _template(*, lifecycle_status: str = "enabled") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Gated Role",
        description="Lifecycle test role",
        icon="GR",
        category="office",
        is_builtin=True,
        soul_template="# Soul",
        default_skills=[],
        default_tools=[],
        default_autonomy_policy={},
        capability_bullets=[],
        role_key="gated-role",
        role_revision=1,
        responsibilities=[],
        non_responsibilities=[],
        limitations=[],
        workflows=[],
        deliverables=[],
        evaluation_criteria=[],
        source_provenance={},
        lifecycle_status=lifecycle_status,
        activation_gate="Pass the evaluation gate.",
        workforce_source_role_id="gated-role",
        workforce_decision="add_candidate",
        workforce_pack="office",
        created_at=None,
    )


@pytest.mark.asyncio
async def test_talent_market_query_is_fail_closed_to_enabled_templates() -> None:
    session = _Session([_template()])

    response = await agents_api.list_templates(
        current_user=SimpleNamespace(id=uuid.uuid4()),
        db=session,
    )

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "agent_templates.lifecycle_status = 'enabled'" in sql
    assert response[0]["lifecycle_status"] == "enabled"


@pytest.mark.asyncio
async def test_disabled_template_id_cannot_bypass_recruitment_gate() -> None:
    template = _template(lifecycle_status="candidate_disabled")
    session = _Session([True], [template])
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="member",
        identity=None,
        quota_agent_ttl_hours=24,
    )

    with pytest.raises(HTTPException) as exc_info:
            await agents_api.create_agent(
                AgentCreate(
                    name="Blocked hire",
                    template_id=template.id,
                    permission_scope_type="private",
                ),
            BackgroundTasks(),
            current_user=user,
            db=session,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "agent_template_not_recruitable",
        "lifecycle_status": "candidate_disabled",
        "activation_gate": "Pass the evaluation gate.",
    }


@pytest.mark.asyncio
async def test_ceo_template_cannot_be_created_through_standard_recruitment() -> None:
    template = _template(lifecycle_status="not_recruitable")
    template.name = "CEO"
    template.role_key = "ceo"
    template.activation_gate = "ceo_orchestrator_governor_enable_only"
    session = _Session([template])
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role="org_owner",
        identity=None,
        quota_agent_ttl_hours=24,
    )

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.create_agent(
            AgentCreate(
                name="CEO",
                template_id=template.id,
                permission_scope_type="company",
            ),
            BackgroundTasks(),
            current_user=user,
            db=session,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "agent_template_not_recruitable",
        "lifecycle_status": "not_recruitable",
        "activation_gate": "ceo_orchestrator_governor_enable_only",
    }


@pytest.mark.asyncio
async def test_platform_catalog_exposes_decisions_without_enabling_roles() -> None:
    response = await workforce_api.get_workforce_catalog(
        decision="merge_or_reject",
        pack=None,
        current_user=SimpleNamespace(
            role="platform_admin",
            identity=SimpleNamespace(is_platform_admin=True),
        ),
    )

    assert response["summary"]["total"] == 268
    assert response["count"] == 15
    assert {record["lifecycle"] for record in response["records"]} == {"not_recruitable"}
    assert all(record["reason"] for record in response["records"])


def test_agent_template_model_contains_lifecycle_indexes() -> None:
    index_names = {index.name for index in AgentTemplate.__table__.indexes}

    assert {
        "ix_agent_templates_role_key",
        "ix_agent_templates_lifecycle_status",
        "ix_agent_templates_workforce_source_role_id",
        "ix_agent_templates_workforce_decision",
        "ix_agent_templates_workforce_pack",
    } <= index_names
