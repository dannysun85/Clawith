from types import SimpleNamespace
import uuid

from sqlalchemy.dialects import postgresql

from app.scripts.inventory_legacy_media_reservations import (
    _task_binding_inventory_query,
    classify_binding,
    classify_reserved_balances,
)


def _pair(**reservation_overrides):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="processing",
    )
    values = {
        "tenant_id": task.tenant_id,
        "agent_id": task.agent_id,
        "user_id": task.user_id,
        "ref_type": "media_task",
        "ref_id": task.id,
        "status": "provider_inflight",
    }
    values.update(reservation_overrides)
    return task, SimpleNamespace(**values)


def test_inventory_query_is_compatible_with_the_pre_098_media_schema():
    sql = str(
        _task_binding_inventory_query().compile(
            dialect=postgresql.dialect(),
        )
    )

    assert "media_generation_tasks.reservation_id" in sql
    assert "media_generation_tasks.request_metadata" in sql
    for post_098_column in (
        "origin_session_id",
        "completion_message_id",
        "output_size",
        "completion_delivery_status",
        "realtime_attempt_count",
        "realtime_next_attempt_at",
        "realtime_published_at",
        "realtime_last_error",
    ):
        assert post_098_column not in sql


def test_inventory_accepts_only_exact_canonical_or_relinkable_bindings():
    task, canonical = _pair()
    assert classify_binding(task, canonical) == "canonical"

    task, legacy = _pair(ref_type="minimax_task", ref_id=None)
    assert classify_binding(task, legacy) == "relinkable_legacy"

    task, null_legacy = _pair(ref_type=None, ref_id=None)
    assert classify_binding(task, null_legacy) == "relinkable_legacy"


def test_inventory_fails_closed_for_missing_owner_or_reference_evidence():
    task, reservation = _pair(agent_id=uuid.uuid4())
    assert classify_binding(task, reservation) == "owner_mismatch"

    task, reservation = _pair(ref_id=uuid.uuid4())
    assert classify_binding(task, reservation) == "reference_mismatch"

    task, _reservation = _pair()
    task.reservation_id = uuid.uuid4()
    task.request_metadata = {"credit_cost": 5}
    assert classify_binding(task, None) == "missing_reservation"


def test_inventory_distinguishes_zero_cost_from_unheld_positive_or_unknown_cost():
    task, _reservation = _pair()
    task.reservation_id = None
    task.request_metadata = {"credit_cost": 0}
    assert classify_binding(task, None) == "zero_cost_no_reservation"

    task.request_metadata = {"credit_cost": 30}
    assert classify_binding(task, None) == "missing_reservation"

    task.request_metadata = {}
    assert classify_binding(task, None) == "indeterminate_credit_reservation"


def test_inventory_rejects_task_and_reservation_lifecycle_mismatches():
    task, reservation = _pair(status="provider_inflight")
    task.status = "succeeded"
    assert (
        classify_binding(task, reservation)
        == "terminal_task_reservation_state_mismatch"
    )

    task, reservation = _pair(status="finalized")
    task.status = "processing"
    assert (
        classify_binding(task, reservation)
        == "unresolved_task_closed_reservation"
    )

    task, reservation = _pair(status="released")
    task.status = "failed"
    assert classify_binding(task, reservation) == "canonical"


def test_inventory_fails_closed_for_reserved_counter_drift_or_missing_balance():
    tenant_ok = uuid.uuid4()
    tenant_drift = uuid.uuid4()
    tenant_missing = uuid.uuid4()
    findings = classify_reserved_balances(
        [
            SimpleNamespace(tenant_id=tenant_ok, reserved=5),
            SimpleNamespace(tenant_id=tenant_drift, reserved=2),
        ],
        {
            tenant_ok: 5,
            tenant_drift: 9,
            tenant_missing: 3,
        },
    )

    assert {(item.category, item.tenant_id) for item in findings} == {
        ("reserved_balance_mismatch", str(tenant_drift)),
        ("missing_credit_balance", str(tenant_missing)),
    }
