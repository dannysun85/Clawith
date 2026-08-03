from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import agent_workforce as workforce_api


class _Result:
    def __init__(self, values):
        self.values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self.values

    def scalar_one_or_none(self):
        if len(self.values) > 1:
            raise AssertionError("expected at most one row")
        return self.values[0] if self.values else None


class _Db:
    def __init__(self, *query_results):
        self.query_results = list(query_results)
        self.added = []
        self.commit_count = 0

    async def execute(self, _statement):
        if not self.query_results:
            raise AssertionError("unexpected query")
        return _Result(self.query_results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commit_count += 1


def _template():
    return SimpleNamespace(
        id=uuid.uuid4(),
        lifecycle_status="candidate_disabled",
        workforce_decision="add_candidate",
        role_key="candidate-role",
        role_revision=2,
        default_skills=[],
        default_tools=[],
        default_mcp_servers=[],
    )


def _evaluation(*, status="passed"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        gate_status=status,
        safety_pass=True,
        capability_pass=True,
        rolled_back_at=None,
        promoted_at=None,
        promoted_by=None,
    )


def _platform_admin():
    return SimpleNamespace(id=uuid.uuid4(), role="platform_admin", identity=None)


@pytest.mark.asyncio
async def test_activation_batch_promotes_only_after_all_items_pass() -> None:
    template = _template()
    evaluation = _evaluation()
    db = _Db([template], [evaluation])

    response = await workforce_api.enable_agent_template_batch(
        workforce_api.AgentTemplateEnableBatchIn(template_ids=[template.id]),
        current_user=_platform_admin(),
        db=db,
    )

    assert response["enabled"] == [str(template.id)]
    assert response["batch_size"] == 1
    assert template.lifecycle_status == "enabled"
    assert evaluation.promoted_at is not None
    assert db.commit_count == 1
    assert db.added[0].action == "agent_template_promoted"


@pytest.mark.asyncio
async def test_activation_batch_is_atomic_when_one_evaluation_fails() -> None:
    first = _template()
    second = _template()
    db = _Db([first, second], [_evaluation()], [_evaluation(status="failed")])

    with pytest.raises(HTTPException) as exc_info:
        await workforce_api.enable_agent_template_batch(
            workforce_api.AgentTemplateEnableBatchIn(
                template_ids=[first.id, second.id]
            ),
            current_user=_platform_admin(),
            db=db,
        )

    assert exc_info.value.status_code == 409
    assert first.lifecycle_status == "candidate_disabled"
    assert second.lifecycle_status == "candidate_disabled"
    assert db.commit_count == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_global_template_mutation_rejects_non_platform_admin() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await workforce_api.enable_agent_template_batch(
            workforce_api.AgentTemplateEnableBatchIn(template_ids=[uuid.uuid4()]),
            current_user=SimpleNamespace(
                id=uuid.uuid4(), role="org_admin", identity=None
            ),
            db=_Db(),
        )

    assert exc_info.value.status_code == 403
