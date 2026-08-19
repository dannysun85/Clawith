"""FR-I6: selection receipt auto-selection, user re-selection, idempotency."""

from __future__ import annotations

import uuid

import pytest

from app.models.deliverable import (
    DeliverableExecution,
    DeliverableExecutionUnit,
    DeliverableRequest,
    DeliverableSelectionReceipt,
)
from app.services.deliverable_executions import DeliverableExecutionError
from app.services.selection_receipts import (
    CandidateScore,
    apply_user_selection,
    auto_client_selection_id,
    candidate_scoreboard,
    ensure_auto_selection,
    select_default_candidate,
)


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        if self.value is None:
            return []
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(self, *execute_values: object | None) -> None:
        self.execute_values = list(execute_values)
        self.added: list[object] = []
        self.flush_count = 0

    async def execute(self, _statement):
        return _Result(self.execute_values.pop(0))

    async def flush(self) -> None:
        self.flush_count += 1

    def add(self, value: object) -> None:
        self.added.append(value)


def _request(**overrides) -> DeliverableRequest:
    request = DeliverableRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        client_request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        work_type="poster",
        workflow_id="builtin.poster.v2",
        workflow_version="2.0.0",
        goal="为保温杯制作抖音海报",
        inputs=[],
        spec={"channel": "social", "aspect_ratio": "9:16", "candidate_count": 2},
        tier="pro",
        approval_policy=["final"],
        output_contract=["png"],
        status="waiting_approval",
        current_stage="output_review",
        version=3,
        contract_revision=1,
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


def _execution(request: DeliverableRequest) -> DeliverableExecution:
    execution = DeliverableExecution(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_number=1,
        kind="initial",
        status="waiting_approval",
        current_stage="output_review",
        workflow_id=request.workflow_id,
        workflow_version=request.workflow_version,
        contract_snapshot={},
        preflight_snapshot={},
        idempotency_key=request.client_request_id,
        request_fingerprint="b" * 64,
    )
    request.current_execution_id = execution.id
    return execution


def _unit(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    stage_key: str,
    unit_key: str,
    *,
    status: str = "pending",
    result_snapshot: dict | None = None,
    quality_evaluation: dict | None = None,
) -> DeliverableExecutionUnit:
    return DeliverableExecutionUnit(
        id=uuid.uuid4(),
        tenant_id=request.tenant_id,
        request_id=request.id,
        execution_id=execution.id,
        stage_key=stage_key,
        unit_key=unit_key,
        status=status,
        dependency_hash="c" * 64,
        attempt_count=0,
        input_snapshot={},
        result_snapshot=result_snapshot or {},
        quality_evaluation=quality_evaluation or {},
    )


def _candidate_units(
    request: DeliverableRequest,
    execution: DeliverableExecution,
    *,
    qa: dict[str, tuple[str, int]],
) -> list[DeliverableExecutionUnit]:
    units: list[DeliverableExecutionUnit] = []
    for index, (unit_key, (qa_status, score)) in enumerate(sorted(qa.items()), start=1):
        artifact_hash = f"{index:x}" * 64
        units.append(
            _unit(
                request,
                execution,
                "candidate_generate",
                unit_key,
                status="succeeded",
                result_snapshot={
                    "candidate_artifact_path": (
                        f"workspace/deliverables/{request.id}/candidates/{unit_key}.png"
                    ),
                    "artifact_sha256": artifact_hash,
                },
            )
        )
        units.append(
            _unit(
                request,
                execution,
                "candidate_qa",
                unit_key,
                status="succeeded" if qa_status == "passed" else "failed",
                quality_evaluation={
                    "candidate_qa": {
                        "schema_version": "candidate-qa-v1",
                        "unit_key": unit_key,
                        "artifact_path": (
                            f"workspace/deliverables/{request.id}/candidates/{unit_key}.png"
                        ),
                        "artifact_sha256": artifact_hash,
                        "status": qa_status,
                        "score": score,
                        "checks": [],
                    },
                    "enforcement": "shadow",
                },
            )
        )
    units.append(_unit(request, execution, "selection", "final"))
    return units


# ─── pure scoreboard/selection ──────────────────────────────────


def test_scoreboard_reads_qa_reports_and_artifact_hashes() -> None:
    request = _request()
    execution = _execution(request)
    units = _candidate_units(
        request,
        execution,
        qa={"candidate-01": ("passed", 72), "candidate-02": ("failed", 40)},
    )
    board = candidate_scoreboard(units, enforcement="enforcing")
    assert [entry.unit_key for entry in board] == ["candidate-01", "candidate-02"]
    first, second = board
    assert first.qa_status == "passed" and first.score == 72 and first.eligible
    assert first.artifact_sha256 == "1" * 64
    assert second.qa_status == "failed" and not second.eligible


def test_scoreboard_shadow_keeps_failed_candidates_selectable_for_auto() -> None:
    request = _request()
    execution = _execution(request)
    units = _candidate_units(request, execution, qa={"candidate-01": ("failed", 55)})
    board = candidate_scoreboard(units, enforcement="shadow")
    assert board[0].qa_status == "failed" and board[0].eligible


def test_default_selection_picks_highest_qa_score_deterministically() -> None:
    board = (
        CandidateScore(
            unit_key="candidate-02",
            qa_status="passed",
            score=88,
            artifact_sha256="b" * 64,
            artifact_path="p2",
            eligible=True,
        ),
        CandidateScore(
            unit_key="candidate-01",
            qa_status="passed",
            score=88,
            artifact_sha256="a" * 64,
            artifact_path="p1",
            eligible=True,
        ),
    )
    winner, reason = select_default_candidate(board, enforcement="enforcing")
    assert winner is not None and winner.unit_key == "candidate-01"
    assert reason == "auto_top_qa_score"


def test_default_selection_enforcing_never_picks_a_failed_candidate() -> None:
    board = (
        CandidateScore(
            unit_key="candidate-01",
            qa_status="failed",
            score=95,
            artifact_sha256="a" * 64,
            artifact_path="p1",
            eligible=False,
        ),
    )
    winner, reason = select_default_candidate(board, enforcement="enforcing")
    assert winner is None and reason == "no_qa_passed_candidate"


def test_default_selection_shadow_falls_back_when_nothing_passed() -> None:
    board = (
        CandidateScore(
            unit_key="candidate-01",
            qa_status="failed",
            score=61,
            artifact_sha256="a" * 64,
            artifact_path="p1",
            eligible=True,
        ),
        CandidateScore(
            unit_key="candidate-02",
            qa_status="failed",
            score=47,
            artifact_sha256="b" * 64,
            artifact_path="p2",
            eligible=True,
        ),
    )
    winner, reason = select_default_candidate(board, enforcement="shadow")
    assert winner is not None and winner.unit_key == "candidate-01"
    assert reason == "auto_shadow_fallback_no_qa_passed"


# ─── auto selection receipt ─────────────────────────────────────


async def test_auto_selection_records_hash_bound_receipt_once() -> None:
    request = _request()
    execution = _execution(request)
    units = _candidate_units(
        request,
        execution,
        qa={"candidate-01": ("passed", 70), "candidate-02": ("passed", 91)},
    )
    session = _Session(execution, None, units, [])
    receipt = await ensure_auto_selection(session, request=request, enforcement="enforcing")
    assert receipt is not None
    assert receipt.actor == "auto"
    assert receipt.selected_unit_key == "candidate-02"
    assert receipt.client_selection_id == auto_client_selection_id(execution.id)
    scores = {entry["unit_key"]: entry for entry in receipt.candidate_scores}
    assert scores["candidate-02"]["artifact_sha256"] == "2" * 64
    assert scores["candidate-02"]["score"] == 91
    selection_unit = next(
        unit for unit in units if unit.stage_key == "selection"
    )
    assert selection_unit.status == "succeeded"
    assert selection_unit.result_snapshot["selected_unit_key"] == "candidate-02"
    assert selection_unit.result_snapshot["selection_receipt_id"] == str(receipt.id)

    # Replay: the same execution returns the stored receipt unchanged.
    replay = _Session(execution, receipt)
    replayed = await ensure_auto_selection(replay, request=request, enforcement="enforcing")
    assert replayed is receipt
    assert replay.flush_count == 0


async def test_auto_selection_waits_for_evaluated_candidates() -> None:
    request = _request()
    execution = _execution(request)
    units = [
        _unit(request, execution, "candidate_generate", "candidate-01"),
        _unit(request, execution, "candidate_qa", "candidate-01"),
    ]
    session = _Session(execution, None, units)
    receipt = await ensure_auto_selection(session, request=request, enforcement="enforcing")
    assert receipt is None
    assert session.added == []


async def test_auto_selection_ignores_non_v2_requests() -> None:
    request = _request(workflow_id="builtin.poster.v1", workflow_version="1.0.0")
    session = _Session()
    assert await ensure_auto_selection(session, request=request) is None
    assert session.execute_values == []


# ─── user re-selection ──────────────────────────────────────────


async def test_user_reselection_records_receipt_and_is_idempotent() -> None:
    request = _request()
    execution = _execution(request)
    units = _candidate_units(
        request,
        execution,
        qa={"candidate-01": ("passed", 70), "candidate-02": ("passed", 91)},
    )
    action_id = uuid.uuid4()
    session = _Session(execution, None, units, [])
    receipt = await apply_user_selection(
        session,
        request=request,
        selected_unit_key="candidate-01",
        actor_user_id=request.created_by_user_id,
        client_selection_id=action_id,
    )
    assert receipt.actor == "user"
    assert receipt.actor_user_id == request.created_by_user_id
    assert receipt.selected_unit_key == "candidate-01"
    assert receipt.selection_reason == "user_selected_at_output_review"

    replay = _Session(execution, receipt)
    replayed = await apply_user_selection(
        replay,
        request=request,
        selected_unit_key="candidate-01",
        actor_user_id=request.created_by_user_id,
        client_selection_id=action_id,
    )
    assert replayed is receipt

    conflict = _Session(execution, receipt)
    with pytest.raises(DeliverableExecutionError) as excinfo:
        await apply_user_selection(
            conflict,
            request=request,
            selected_unit_key="candidate-02",
            actor_user_id=request.created_by_user_id,
            client_selection_id=action_id,
        )
    assert excinfo.value.code == "deliverable_selection_id_reused"


async def test_user_reselection_rejects_qa_failed_and_unknown_candidates() -> None:
    request = _request()
    execution = _execution(request)
    units = _candidate_units(
        request,
        execution,
        qa={"candidate-01": ("failed", 88), "candidate-02": ("passed", 60)},
    )
    session = _Session(execution, None, units)
    with pytest.raises(DeliverableExecutionError) as excinfo:
        await apply_user_selection(
            session,
            request=request,
            selected_unit_key="candidate-01",
            actor_user_id=request.created_by_user_id,
            client_selection_id=uuid.uuid4(),
        )
    assert excinfo.value.code == "deliverable_selection_candidate_ineligible"

    unknown = _Session(execution, None, units)
    with pytest.raises(DeliverableExecutionError) as excinfo:
        await apply_user_selection(
            unknown,
            request=request,
            selected_unit_key="candidate-09",
            actor_user_id=request.created_by_user_id,
            client_selection_id=uuid.uuid4(),
        )
    assert excinfo.value.code == "deliverable_selection_candidate_unknown"


async def test_user_reselection_requires_v2_poster() -> None:
    request = _request(workflow_id="builtin.poster.v1", workflow_version="1.0.0")
    session = _Session()
    with pytest.raises(DeliverableExecutionError) as excinfo:
        await apply_user_selection(
            session,
            request=request,
            selected_unit_key="candidate-01",
            actor_user_id=request.created_by_user_id,
            client_selection_id=uuid.uuid4(),
        )
    assert excinfo.value.code == "deliverable_selection_not_available"


def test_selection_receipt_model_is_tenant_scoped_and_idempotent() -> None:
    table = DeliverableSelectionReceipt.__table__
    assert "tenant_id" in table.c and "request_id" in table.c
    constraint_names = {constraint.name for constraint in table.constraints}
    assert "uq_deliverable_selection_receipts_client" in constraint_names
    assert "ck_deliverable_selection_receipts_actor" in constraint_names


# ─── approvals API seam ─────────────────────────────────────────


def _mock_final_approval_api(monkeypatch, request, execution):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.api import deliverables

    user = SimpleNamespace(id=request.created_by_user_id, tenant_id=request.tenant_id)
    monkeypatch.setattr(deliverables, "_owned_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        deliverables,
        "get_settings",
        lambda: SimpleNamespace(DELIVERABLE_STAGE_APPROVALS_ENABLED=False),
    )
    monkeypatch.setattr(
        deliverables,
        "ensure_execution_shadow",
        AsyncMock(return_value=execution),
    )
    monkeypatch.setattr(deliverables, "project_execution_lifecycle", AsyncMock())
    monkeypatch.setattr(deliverables, "_request_out", AsyncMock(side_effect=lambda _db, req: req))
    calls: dict[str, list[str]] = {"select": [], "rebind": [], "approve": []}

    async def _select(_db, *, request, selected_unit_key, **_kwargs):  # noqa: ANN001
        calls["select"].append(selected_unit_key)

    async def _rebind(_db, *, request, selected_unit_key, **_kwargs):  # noqa: ANN001
        calls["rebind"].append(selected_unit_key)

    async def _approve(_db, *, request):  # noqa: ANN001
        calls["approve"].append("approve")
        return ()

    monkeypatch.setattr(deliverables, "apply_user_selection", _select)
    monkeypatch.setattr(deliverables, "rebind_poster_selection_artifact", _rebind)
    monkeypatch.setattr(deliverables, "approve_deliverable_artifacts", _approve)
    return deliverables, user, calls


async def test_final_approval_with_target_unit_reselects_candidate(monkeypatch) -> None:
    from app.api import deliverables as deliverables_api
    from app.schemas.deliverable import DeliverableApprovalIn

    request = _request()
    execution = _execution(request)
    deliverables, user, calls = _mock_final_approval_api(monkeypatch, request, execution)
    session = _Session(None)
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="final",
        action="approve",
        target_units=["candidate-02"],
    )
    result = await deliverables.record_deliverable_approval(
        request.id,
        data,
        user,
        session,
    )
    assert result is request
    assert calls["select"] == ["candidate-02"]
    assert calls["rebind"] == ["candidate-02"]
    assert calls["approve"] == ["approve"]
    assert request.status == "succeeded"
    assert deliverables_api is deliverables


async def test_final_approval_without_targets_keeps_auto_selection(monkeypatch) -> None:
    from app.schemas.deliverable import DeliverableApprovalIn

    request = _request()
    execution = _execution(request)
    deliverables, user, calls = _mock_final_approval_api(monkeypatch, request, execution)
    session = _Session(None)
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="final",
        action="approve",
    )
    await deliverables.record_deliverable_approval(request.id, data, user, session)
    assert calls["select"] == [] and calls["rebind"] == []
    assert calls["approve"] == ["approve"]


async def test_v1_approval_never_enters_the_selection_branch(monkeypatch) -> None:
    from app.schemas.deliverable import DeliverableApprovalIn

    request = _request(workflow_id="builtin.poster.v1", workflow_version="1.0.0")
    execution = _execution(request)
    deliverables, user, calls = _mock_final_approval_api(monkeypatch, request, execution)
    session = _Session(None)
    data = DeliverableApprovalIn(
        expected_version=request.version,
        client_action_id=uuid.uuid4(),
        stage="final",
        action="approve",
        target_units=["candidate-02"],
    )
    await deliverables.record_deliverable_approval(request.id, data, user, session)
    assert calls["select"] == [] and calls["rebind"] == []
    assert calls["approve"] == ["approve"]
