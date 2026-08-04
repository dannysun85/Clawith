import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.work import (
    _build_work_statement,
    _confirmation_fingerprint,
    _fingerprint,
    get_work_task,
)
from app.core.permissions import is_agent_executable
from app.schemas.work import WorkTaskCreate, WorkTaskPreflight
from app.services.work_projection import project_execution_status, project_user_stage


def test_task_done_is_completed_work_not_formal_delivery() -> None:
    execution = project_execution_status(task_status="done", terminal_run_event="run_completed")

    assert execution == "completed"
    assert project_user_stage(
        task_status="done",
        execution_status=execution,
        deliverable_status=None,
        artifact_status=None,
        review_status=None,
    ) == "completed"


def test_formal_delivery_requires_succeeded_request_and_approved_artifact() -> None:
    assert project_user_stage(
        task_status="done",
        execution_status="completed",
        deliverable_status="succeeded",
        artifact_status="candidate",
        review_status=None,
    ) == "artifact"
    assert project_user_stage(
        task_status="done",
        execution_status="completed",
        deliverable_status="succeeded",
        artifact_status="approved",
        review_status="passed",
    ) == "delivery"


def test_work_contract_exposes_task_only_and_formal_delivery_modes() -> None:
    from app.schemas.work import WorkItemOut

    assert WorkItemOut.model_fields["delivery_mode"].annotation is not None


def test_quality_review_and_approval_remain_distinct_stages() -> None:
    assert project_user_stage(
        task_status=None,
        execution_status="completed",
        deliverable_status="waiting_approval",
        artifact_status="candidate",
        review_status="open",
    ) == "review"
    assert project_user_stage(
        task_status=None,
        execution_status="completed",
        deliverable_status="waiting_approval",
        artifact_status=None,
        review_status=None,
    ) == "approval"


def test_temporary_expert_requires_an_immutable_role_snapshot_input() -> None:
    with pytest.raises(ValidationError):
        WorkTaskCreate(
            client_request_id=uuid.uuid4(),
            title="Review contract",
            intent="Review the current contract",
            executor_kind="temporary_expert",
        )


def test_group_executor_requires_a_session_and_ordered_agent_participants() -> None:
    with pytest.raises(ValidationError):
        WorkTaskPreflight(
            title="Launch campaign",
            intent="Coordinate the campaign launch",
            executor_kind="group",
        )

    first = uuid.uuid4()
    draft = WorkTaskPreflight(
        title="Launch campaign",
        intent="Coordinate the campaign launch",
        executor_kind="group",
        group_id=uuid.uuid4(),
        group_session_id=uuid.uuid4(),
        group_agent_participant_ids=[first, uuid.uuid4()],
    )

    assert draft.group_agent_participant_ids[0] == first


def test_group_executor_rejects_duplicate_agent_participants() -> None:
    participant_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        WorkTaskPreflight(
            title="Launch campaign",
            intent="Coordinate the campaign launch",
            executor_kind="group",
            group_id=uuid.uuid4(),
            group_session_id=uuid.uuid4(),
            group_agent_participant_ids=[participant_id, participant_id],
        )


def test_workbench_idempotency_ignores_only_the_client_request_id() -> None:
    first = WorkTaskCreate(
        client_request_id=uuid.uuid4(),
        confirmation_fingerprint="0" * 64,
        title="Review contract",
        intent="Review the current contract",
    )
    replay = first.model_copy(
        update={
            "client_request_id": uuid.uuid4(),
            "confirmation_fingerprint": "1" * 64,
        }
    )

    assert _fingerprint(first) == _fingerprint(replay)


def test_confirmation_fingerprint_binds_the_resolved_executor() -> None:
    draft = WorkTaskPreflight(
        title="Review contract",
        intent="Review the current contract",
    )

    assert _confirmation_fingerprint(draft, agent_id=uuid.uuid4()) != (
        _confirmation_fingerprint(draft, agent_id=uuid.uuid4())
    )


def test_creative_work_statement_preserves_task_only_boundary() -> None:
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, name="Creative coordinator")
    draft = WorkTaskPreflight(
        title="Prepare the campaign",
        intent="Prepare a commercial video brief",
        work_type="video",
    )

    statement = _build_work_statement(
        draft,
        agent=agent,
        executor_snapshot={},
    )

    assert statement["delivery_mode"] == "task_only"
    assert statement["expected_output"] == "confirmed_video_brief"
    assert statement["cost"]["formal_media_requires_separate_preflight"] is True


def test_idle_native_agent_is_available_for_workbench_execution() -> None:
    agent = SimpleNamespace(
        status="idle",
        deleted_at=None,
        deletion_requested_at=None,
        is_expired=False,
        expires_at=None,
    )

    assert is_agent_executable(agent)


def test_stopped_or_deleting_agent_is_not_available_for_execution() -> None:
    base = {
        "deleted_at": None,
        "deletion_requested_at": None,
        "is_expired": False,
        "expires_at": None,
    }

    assert not is_agent_executable(SimpleNamespace(status="stopped", **base))
    assert not is_agent_executable(
        SimpleNamespace(status="idle", **(base | {"deletion_requested_at": object()}))
    )


@pytest.mark.asyncio
async def test_single_work_task_endpoint_restores_scoped_projection(monkeypatch) -> None:
    task_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    expected = SimpleNamespace(task_id=task_id)

    async def project(db, *, user: object, task_id: uuid.UUID):
        assert db == "db"
        assert user is not None
        assert task_id == expected.task_id
        return expected

    monkeypatch.setattr("app.api.work._work_item_for_task", project)

    result = await get_work_task(
        task_id=task_id,
        current_user=user,  # type: ignore[arg-type]
        db="db",  # type: ignore[arg-type]
    )

    assert result is expected
