import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.work import _fingerprint
from app.core.permissions import is_agent_executable
from app.schemas.work import WorkTaskCreate
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


def test_workbench_idempotency_ignores_only_the_client_request_id() -> None:
    first = WorkTaskCreate(
        client_request_id=uuid.uuid4(),
        title="Review contract",
        intent="Review the current contract",
    )
    replay = first.model_copy(update={"client_request_id": uuid.uuid4()})

    assert _fingerprint(first) == _fingerprint(replay)


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
