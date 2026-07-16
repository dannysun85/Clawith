#!/usr/bin/env python3
"""Real PostgreSQL claim/CAS/crash smoke for durable approvals."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.database import async_session
from app.models.audit import ApprovalRequest, AuditLog
from app.models.notification import Notification
from app.models.user import User
from app.services import agent_tools, autonomy_service
from app.services.agent_tools import ApprovedToolExecutionOutcome
from app.services.autonomy_service import (
    AutonomyService,
    build_tool_approval_details,
)


USER_ID = uuid.UUID("07500000-0000-4000-8000-000000000060")
AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000061")
SECOND_AGENT_ID = uuid.UUID("07500000-0000-4000-8000-000000000064")


async def main() -> None:
    # Production deliberately defaults this release gate to False.  Enable the
    # dormant execution path only inside this isolated smoke process so its
    # claim/CAS/crash guarantees remain tested without changing runtime policy.
    original_execution_gate = autonomy_service.APPROVAL_AUTOMATIC_EXECUTION_ENABLED
    autonomy_service.APPROVAL_AUTOMATIC_EXECUTION_ENABLED = True
    service = AutonomyService()
    dispatch_count = 0

    async def permit(_agent_id: uuid.UUID, _tool_name: str) -> None:
        return None

    async def execute_once(*_args, **_kwargs) -> ApprovedToolExecutionOutcome:
        nonlocal dispatch_count
        dispatch_count += 1
        await asyncio.sleep(0.05)
        return ApprovedToolExecutionOutcome(
            status="succeeded",
            result={"durable": True, "private_payload": "must-not-be-stored"},
        )

    service._assert_execution_permission = permit  # type: ignore[method-assign]
    original_executor = agent_tools._execute_approved_tool
    approval_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    fairness_ids = [uuid.uuid4() for _ in range(6)]
    try:
        details = build_tool_approval_details(
            AGENT_ID,
            "write_workspace_files",
            "write_file",
            {"path": "workspace/approval-smoke.txt", "content": "private"},
            USER_ID,
        )
        async with async_session() as db:
            user = await db.get(User, USER_ID)
            assert user is not None
            db.add(
                ApprovalRequest(
                    id=approval_id,
                    agent_id=AGENT_ID,
                    action_type="write_workspace_files",
                    details=details,
                    status="pending",
                    request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                    execution_status=None,
                    execution_attempts=0,
                    execution_result_summary={},
                )
            )
            await db.commit()

            # Resolution commits the immutable decision before any side effect.
            await service.resolve_approval(db, approval_id, user, "approve")

        async with async_session() as db:
            resolved = await db.get(ApprovalRequest, approval_id)
            assert resolved is not None
            assert resolved.status == "approved"
            assert resolved.execution_status == "pending"
            assert resolved.execution_attempts == 0

        agent_tools._execute_approved_tool = execute_once
        outcomes = await asyncio.gather(
            service.execute_pending_approval(approval_id),
            service.execute_pending_approval(approval_id),
        )
        assert sorted(outcomes) == [False, True]
        assert dispatch_count == 1

        async with async_session() as db:
            completed = await db.get(ApprovalRequest, approval_id)
            assert completed is not None
            assert completed.execution_status == "succeeded"
            assert completed.execution_attempts == 1
            assert completed.execution_claim_token is not None
            assert completed.execution_claimed_at is not None
            assert completed.execution_finished_at is not None
            assert "private" not in str(completed.execution_result_summary)
            succeeded_audits = await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "approval_execution_succeeded",
                    AuditLog.details["approval_id"].astext == str(approval_id),
                )
            )
            terminal_notifications = await db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.ref_id == approval_id,
                    Notification.type == "approval_execution_terminal",
                )
            )
            assert succeeded_audits == 1
            assert terminal_notifications == 1

        # A stale or forged claim token cannot rewrite a completed outcome.
        changed = await service._finish_approval_execution(
            approval_id,
            uuid.uuid4(),
            status="failed",
            error_code="ForgedClaim",
        )
        assert changed is False

        # The bounded batch must take the first due row from every company
        # before filling remaining slots with a second/third row from one busy
        # company.  Five older requests from tenant A must not hide tenant B.
        second_details = build_tool_approval_details(
            SECOND_AGENT_ID,
            "write_workspace_files",
            "write_file",
            {"path": "workspace/fairness.txt", "content": "private"},
            USER_ID,
        )
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            for index, fairness_id in enumerate(fairness_ids[:5]):
                db.add(
                    ApprovalRequest(
                        id=fairness_id,
                        agent_id=AGENT_ID,
                        action_type="write_workspace_files",
                        details=details,
                        status="approved",
                        resolved_at=now - timedelta(minutes=10 - index),
                        resolved_by=USER_ID,
                        request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                        execution_status="pending",
                        execution_attempts=0,
                        execution_result_summary={},
                    )
                )
            db.add(
                ApprovalRequest(
                    id=fairness_ids[5],
                    agent_id=SECOND_AGENT_ID,
                    action_type="write_workspace_files",
                    details=second_details,
                    status="approved",
                    resolved_at=now,
                    resolved_by=USER_ID,
                    request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                    execution_status="pending",
                    execution_attempts=0,
                    execution_result_summary={},
                )
            )
            await db.commit()

        selected_ids: list[uuid.UUID] = []
        original_pending_executor = service.execute_pending_approval

        async def record_selection(selected_id: uuid.UUID) -> bool:
            selected_ids.append(selected_id)
            return False

        service.execute_pending_approval = record_selection  # type: ignore[method-assign]
        try:
            assert await service.process_pending_approval_batch(limit=4) == 0
        finally:
            service.execute_pending_approval = original_pending_executor  # type: ignore[method-assign]
        assert len(selected_ids) == 4
        assert fairness_ids[5] in selected_ids
        assert len(set(selected_ids).intersection(fairness_ids[:5])) == 3

        async with async_session() as db:
            db.add(
                ApprovalRequest(
                    id=stale_id,
                    agent_id=AGENT_ID,
                    action_type="write_workspace_files",
                    details=details,
                    status="approved",
                    resolved_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    resolved_by=USER_ID,
                    request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
                    execution_status="executing",
                    execution_claim_token=uuid.uuid4(),
                    execution_claimed_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    execution_attempts=1,
                    execution_result_summary={},
                )
            )
            await db.commit()

        reconciled = await service.reconcile_stale_executions()
        assert reconciled >= 1
        assert await service.execute_pending_approval(stale_id) is False
        assert dispatch_count == 1

        async with async_session() as db:
            stale = await db.get(ApprovalRequest, stale_id)
            assert stale is not None
            assert stale.execution_status == "ambiguous"
            assert stale.execution_error_code == "StaleExecutionClaim"
            stale_notifications = await db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.ref_id == stale_id,
                    Notification.type == "approval_execution_terminal",
                )
            )
            assert stale_notifications == 1
    finally:
        autonomy_service.APPROVAL_AUTOMATIC_EXECUTION_ENABLED = original_execution_gate
        agent_tools._execute_approved_tool = original_executor
        async with async_session() as db:
            await db.execute(
                delete(Notification).where(Notification.ref_id.in_((approval_id, stale_id)))
            )
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.details["approval_id"].astext.in_(
                        (str(approval_id), str(stale_id))
                    )
                )
            )
            await db.execute(
                delete(ApprovalRequest).where(
                    ApprovalRequest.id.in_((approval_id, stale_id, *fairness_ids))
                )
            )
            await db.commit()

    print("Approval execution PostgreSQL claim/CAS/crash smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
