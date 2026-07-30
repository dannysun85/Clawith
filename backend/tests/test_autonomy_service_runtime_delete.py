"""Runtime-scoped approval tests for the signed Astra approval boundary."""

import uuid

import pytest

from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.user import User
from app.services import autonomy_service as autonomy_module


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _signed_runtime_details(
    *,
    agent_id: uuid.UUID,
    requested_by: uuid.UUID,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_call_id: str,
) -> dict:
    details = autonomy_module.build_tool_approval_details(
        agent_id,
        "delete_files",
        "delete_file",
        {"path": "workspace/remove-me.md"},
        requested_by,
    )
    details["runtime_scope"] = {
        "tenant_id": str(tenant_id),
        "run_id": str(run_id),
        "session_id": str(uuid.uuid4()),
        "workspace_scope": "agent",
        "tool_call_id": tool_call_id,
    }
    return details


@pytest.mark.asyncio
async def test_runtime_l3_approval_is_reused_as_the_tool_call_decision(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=creator_id,
        name="Approval Agent",
        status="idle",
        is_expired=False,
        access_mode="company",
        autonomy_policy={"delete_files": "L3"},
    )

    class _DB:
        def __init__(self) -> None:
            self.approval = None
            self.added = []

        async def execute(self, _statement):
            return _ScalarResult(self.approval)

        def add(self, value) -> None:
            self.added.append(value)
            if isinstance(value, ApprovalRequest):
                self.approval = value

        async def flush(self) -> None:
            return None

        def begin_nested(self):
            return _NestedTransaction()

    db = _DB()
    requested = []

    async def request_approval(_self, _db, _agent, approval):
        requested.append(approval)

    monkeypatch.setattr(
        autonomy_module.AutonomyService,
        "_request_approval",
        request_approval,
    )
    details = _signed_runtime_details(
        agent_id=agent.id,
        requested_by=creator_id,
        tenant_id=tenant_id,
        run_id=run_id,
        tool_call_id="call-delete",
    )
    service = autonomy_module.AutonomyService()

    pending = await service.check_and_enforce(
        db, agent, "delete_files", details  # type: ignore[arg-type]
    )

    expected_id = uuid.uuid5(
        run_id,
        "runtime-approval:delete_files:call-delete",
    )
    assert pending == {
        "allowed": False,
        "level": "L3",
        "approval_id": str(expected_id),
        "approval_status": "pending",
        "correlation_id": f"approval:{expected_id}",
        "message": "Approval requested from creator",
    }
    assert db.approval.id == expected_id
    assert db.approval.details["runtime_scope"]["approval_correlation_id"] == (
        f"approval:{expected_id}"
    )
    assert requested == [db.approval]

    db.approval.status = "approved"
    approved = await service.check_and_enforce(
        db, agent, "delete_files", details  # type: ignore[arg-type]
    )

    assert approved["allowed"] is True
    assert approved["approval_status"] == "approved"
    assert approved["approval_id"] == str(expected_id)
    assert requested == [db.approval]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_runtime_approval_resolution_resumes_the_original_run(
    monkeypatch,
    action,
) -> None:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    approval_id = uuid.uuid5(
        run_id,
        "runtime-approval:delete_files:call-delete",
    )
    correlation_id = f"approval:{approval_id}"
    details = _signed_runtime_details(
        agent_id=agent_id,
        requested_by=creator_id,
        tenant_id=tenant_id,
        run_id=run_id,
        tool_call_id="call-delete",
    )
    details["runtime_scope"]["approval_correlation_id"] = correlation_id
    approval = ApprovalRequest(
        id=approval_id,
        agent_id=agent_id,
        action_type="delete_files",
        status="pending",
        execution_status="not_required",
        details=details,
    )
    agent = Agent(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=creator_id,
        name="Approval Agent",
        status="idle",
        is_expired=False,
        access_mode="company",
    )
    user = User(
        id=creator_id,
        tenant_id=tenant_id,
        display_name="Creator",
        role="member",
        is_active=True,
    )

    class _DB:
        def __init__(self) -> None:
            self.results = iter((approval, agent))
            self.added = []
            self.flush_count = 0
            self.commit_count = 0

        async def execute(self, _statement):
            return _ScalarResult(next(self.results))

        def add(self, value) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.flush_count += 1

        async def commit(self) -> None:
            self.commit_count += 1

    db = _DB()
    resumed = []
    notifications = []

    class _RuntimeCommandIntake:
        def __init__(self, db_arg) -> None:
            assert db_arg is db

        async def resume_run(self, command):
            resumed.append(command)

    async def send_notification(db_arg, **kwargs):
        assert db_arg is db
        notifications.append(kwargs)

    monkeypatch.setattr(
        "app.services.agent_runtime.adapter.RuntimeCommandIntake",
        _RuntimeCommandIntake,
    )
    monkeypatch.setattr(
        "app.services.notification_service.send_notification",
        send_notification,
    )

    resolved = await autonomy_module.AutonomyService().resolve_approval(
        db, approval_id, user, action  # type: ignore[arg-type]
    )

    expected_status = "approved" if action == "approve" else "rejected"
    assert resolved.status == expected_status
    assert resolved.execution_status == "not_required"
    assert len(resumed) == 1
    command = resumed[0]
    assert command.tenant_id == tenant_id
    assert command.run_id == run_id
    assert command.idempotency_key == (
        f"approval:{approval_id}:{expected_status}"
    )
    assert command.payload["resume_type"] == "user_input"
    assert command.payload["correlation_id"] == correlation_id
    assert command.payload["payload"]["decision"] == expected_status
    assert command.actor_user_id == creator_id
    assert db.flush_count == 2
    assert db.commit_count == 1
    assert notifications
