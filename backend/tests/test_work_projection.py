from datetime import UTC, datetime
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import work as work_api
from app.api.work import (
    _build_work_statement,
    _confirmation_fingerprint,
    _fingerprint,
    get_work_task,
)
from app.core.permissions import is_agent_executable
from app.schemas.work import WorkTaskCreate, WorkTaskPreflight, WorkTaskResultReview
from app.services.work_projection import (
    project_execution_status,
    project_task_result_review_status,
    project_user_stage,
)
from app.services.work_deliverable_contract import work_task_deliverable_contract


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _SingleQueryDb:
    def __init__(self, value):
        self.value = value
        self.query_count = 0

    async def execute(self, _statement):
        self.query_count += 1
        return _ScalarResult(self.value)


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


@pytest.mark.asyncio
async def test_result_review_idempotency_survives_a_later_retry_state(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    run_id = uuid.uuid4()
    request = WorkTaskResultReview(
        run_id=run_id,
        action="request_changes",
        comment="补充可量化上线门槛",
        client_request_id=uuid.uuid4(),
    )
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        created_by=user_id,
        status="doing",
        work_statement={
            "acceptance_contract": {
                "version": 1,
                "criteria": ["方案可执行"],
                "owner_review_required": True,
            }
        },
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        task_id=task_id,
        run_id=run_id,
        actor_user_id=user_id,
        request_fingerprint=work_api._task_result_review_fingerprint(request),
        action="request_changes",
        comment="补充可量化上线门槛",
        created_at=datetime.now(UTC),
    )
    db = _SingleQueryDb(receipt)

    async def owned_task(*_args, **_kwargs):
        return task

    monkeypatch.setattr(work_api, "_owned_work_task", owned_task)

    result = await work_api.review_work_task_result(
        task_id,
        request,
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,  # type: ignore[arg-type]
    )

    assert result.created is False
    assert result.receipt.run_id == run_id
    assert db.query_count == 1


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
    assert WorkItemOut.model_fields["formal_delivery_spec"].annotation is not None


def test_work_task_contract_preserves_explicit_poster_ratio_and_copy() -> None:
    objective = "\n".join(
        [
            "竖版 9:16 商业宣传海报",
            "主标题：量化交易平台",
            "副标题：智能策略・实时信号・数据驱动决策",
            "标语：从复杂市场中，捕捉更清晰的交易方向",
            "CTA：立即体验",
        ]
    )
    task = SimpleNamespace(
        work_type="image",
        intent="fallback",
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": objective,
        },
    )

    contract = work_task_deliverable_contract(task)

    assert contract is not None
    assert contract.work_type == "poster"
    assert contract.goal == objective
    assert contract.spec == {
        "aspect_ratio": "9:16",
        "exact_copy": "\n".join(
            [
                "量化交易平台",
                "智能策略・实时信号・数据驱动决策",
                "从复杂市场中，捕捉更清晰的交易方向",
                "立即体验",
            ]
        ),
    }


def test_work_task_contract_refuses_to_guess_ambiguous_ratio() -> None:
    task = SimpleNamespace(
        work_type="image",
        intent="fallback",
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": "同时准备 9:16 和 1:1 两个版本",
        },
    )

    contract = work_task_deliverable_contract(task)

    assert contract is not None
    assert contract.spec == {}


def test_work_task_contract_extracts_explicit_copy_from_original_poster_prose() -> None:
    objective = (
        "竖版 9:16 商业宣传海报，画面中部居中放置发光渐变立体白色大标题"
        "【量化交易平台】，标题下方小字副标题「智能策略・实时信号・数据驱动决策」，"
        "再下方一行浅紫色小字标语「从复杂市场中，捕捉更清晰的交易方向」；"
        "画面右下角有渐变粉紫发光圆角按钮，按钮内白色文字 “立即体验”。"
    )
    task = SimpleNamespace(
        work_type="image",
        intent=objective,
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": objective,
        },
    )

    contract = work_task_deliverable_contract(task)

    assert contract is not None
    assert contract.spec == {
        "aspect_ratio": "9:16",
        "exact_copy": "\n".join(
            [
                "量化交易平台",
                "智能策略・实时信号・数据驱动决策",
                "从复杂市场中，捕捉更清晰的交易方向",
                "立即体验",
            ]
        ),
    }


def test_work_task_contract_preserves_footer_and_button_from_inline_poster_prose() -> None:
    objective = (
        "竖版 9:16 商业宣传海报，主标题【把 AI 公司真正运行起来】；"
        "副标题【数字员工・任务协作・WorkProduct 审核】；"
        "标语【从任务到成果，企业运营真正闭环】；"
        "落款【ReefTotem｜深圳前海瑞孚图腾科技有限公司】；按钮【立即体验】。"
    )
    task = SimpleNamespace(
        work_type="image",
        intent=objective,
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": objective,
        },
    )

    contract = work_task_deliverable_contract(task)

    assert contract is not None
    assert contract.spec == {
        "aspect_ratio": "9:16",
        "exact_copy": "\n".join(
            [
                "把 AI 公司真正运行起来",
                "数字员工・任务协作・WorkProduct 审核",
                "从任务到成果，企业运营真正闭环",
                "ReefTotem｜深圳前海瑞孚图腾科技有限公司",
                "立即体验",
            ]
        ),
    }


def test_work_task_contract_does_not_treat_button_style_as_cta_copy() -> None:
    objective = (
        "竖版 9:16 海报，主标题【A】；副标题【B】；标语【C】；"
        "按钮样式【渐变粉紫发光圆角】，按钮内白色文字【立即体验】。"
    )
    task = SimpleNamespace(
        work_type="image",
        intent=objective,
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": objective,
        },
    )

    contract = work_task_deliverable_contract(task)

    assert contract is not None
    assert contract.spec == {
        "aspect_ratio": "9:16",
        "exact_copy": "A\nB\nC\n立即体验",
    }

    style_only = SimpleNamespace(
        work_type="image",
        intent="fallback",
        work_statement={
            "delivery_mode": "task_only",
            "work_type": "image",
            "objective": (
                "竖版 9:16 海报，主标题【A】；副标题【B】；"
                "标语【C】；按钮样式【渐变粉紫发光圆角】。"
            ),
        },
    )

    style_only_contract = work_task_deliverable_contract(style_only)

    assert style_only_contract is not None
    assert style_only_contract.spec["exact_copy"] == "A\nB\nC"


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


def test_completed_work_requires_owner_approval_before_business_completion() -> None:
    statement = {
        "acceptance_contract": {
            "version": 1,
            "owner_review_required": True,
        }
    }
    pending = project_task_result_review_status(
        task_status="done",
        work_statement=statement,
        receipt_action=None,
    )
    approved = project_task_result_review_status(
        task_status="done",
        work_statement=statement,
        receipt_action="approve",
    )
    changes = project_task_result_review_status(
        task_status="done",
        work_statement=statement,
        receipt_action="request_changes",
    )

    assert pending == "pending"
    assert approved == "approved"
    assert changes == "request_changes"
    assert project_user_stage(
        task_status="done",
        execution_status="completed",
        deliverable_status=None,
        artifact_status=None,
        review_status=None,
        task_result_review_status=pending,
    ) == "review"
    assert project_user_stage(
        task_status="done",
        execution_status="completed",
        deliverable_status=None,
        artifact_status=None,
        review_status=None,
        task_result_review_status=changes,
    ) == "blocked"


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
