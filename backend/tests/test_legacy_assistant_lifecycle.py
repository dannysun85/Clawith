from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.core.permissions import is_agent_executable
from app.models.agent import AgentPermission
from app.models.audit import AuditLog
from app.schemas.schemas import LegacyAssistantDispositionUpdate
from app.services.quota_guard import QuotaExceeded


def _agent(**overrides):
    values = {
        "id": uuid.uuid4(),
        "creator_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "template_id": uuid.uuid4(),
        "is_system": False,
        "legacy_assistant_state": None,
        "status": "idle",
        "heartbeat_enabled": True,
        "access_mode": "private",
        "company_access_level": "manage",
        "deleted_at": None,
        "deletion_requested_at": None,
        "is_expired": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _db():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


def _user(agent):
    return SimpleNamespace(id=agent.creator_id, tenant_id=agent.tenant_id, role="member")


def _common_patches(agent, response):
    return (
        patch.object(agents_api, "check_agent_access", AsyncMock(return_value=(agent, "manage"))),
        patch.object(agents_api, "_legacy_assistant_origin", AsyncMock(return_value=(False, True))),
        patch.object(
            agents_api,
            "resolve_agent_product_roles",
            AsyncMock(return_value={agent.id: "legacy_personal_assistant"}),
        ),
        patch.object(agents_api, "_agent_to_out", AsyncMock(return_value=response)),
    )


def test_transition_contract_is_stale_safe_and_idempotent() -> None:
    assert agents_api._resolve_legacy_assistant_transition(
        action="archive",
        expected_disposition="active",
        current_disposition="active",
    ) == ("archived", False)
    assert agents_api._resolve_legacy_assistant_transition(
        action="archive",
        expected_disposition="active",
        current_disposition="archived",
    ) == ("archived", True)

    with pytest.raises(HTTPException) as stale:
        agents_api._resolve_legacy_assistant_transition(
            action="archive",
            expected_disposition="active",
            current_disposition="converted",
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "legacy_assistant_state_changed"

    with pytest.raises(HTTPException) as invalid:
        agents_api._resolve_legacy_assistant_transition(
            action="archive",
            expected_disposition="converted",
            current_disposition="converted",
        )
    assert invalid.value.detail["code"] == "legacy_assistant_transition_invalid"


@pytest.mark.asyncio
async def test_archive_stops_runtime_disables_execution_and_records_audit() -> None:
    agent = _agent()
    db = _db()
    response = object()
    common = _common_patches(agent, response)
    with (
        common[0], common[1], common[2], common[3],
        patch.object(agents_api.agent_manager, "stop_container", AsyncMock(return_value=True)) as stop,
    ):
        result = await agents_api.update_legacy_assistant_disposition(
            agent.id,
            LegacyAssistantDispositionUpdate(
                action="archive",
                expected_disposition="active",
            ),
            current_user=_user(agent),
            db=db,
        )

    assert result is response
    stop.assert_awaited_once_with(agent)
    assert agent.legacy_assistant_state == "archived"
    assert agent.status == "stopped"
    assert agent.heartbeat_enabled is False
    assert is_agent_executable(agent) is False
    audit = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AuditLog))
    assert audit.action == "legacy_assistant_disposition_changed"
    assert audit.details["previous_disposition"] == "active"
    assert audit.details["disposition"] == "archived"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_convert_checks_quota_preserves_private_access_and_reserves_seat() -> None:
    agent = _agent(status="stopped", heartbeat_enabled=False)
    db = _db()
    response = object()
    common = _common_patches(agent, response)
    with (
        common[0], common[1], common[2], common[3],
        patch.object(agents_api, "check_agent_creation_quota", AsyncMock()) as quota,
    ):
        await agents_api.update_legacy_assistant_disposition(
            agent.id,
            LegacyAssistantDispositionUpdate(
                action="convert_to_employee",
                expected_disposition="active",
            ),
            current_user=_user(agent),
            db=db,
        )

    quota.assert_awaited_once_with(
        agent.creator_id,
        tenant_id=agent.tenant_id,
        db=db,
    )
    assert agent.legacy_assistant_state == "converted"
    assert agent.access_mode == "private"
    audit = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AuditLog))
    assert audit.details["employee_seat_reserved"] is True


@pytest.mark.asyncio
async def test_convert_fails_closed_at_employee_plan_limit() -> None:
    agent = _agent()
    db = _db()
    response = object()
    common = _common_patches(agent, response)
    with (
        common[0], common[1], common[2], common[3],
        patch.object(
            agents_api,
            "check_agent_creation_quota",
            AsyncMock(side_effect=QuotaExceeded("full", quota_type="max_agents")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.update_legacy_assistant_disposition(
                agent.id,
                LegacyAssistantDispositionUpdate(
                    action="convert_to_employee",
                    expected_disposition="active",
                ),
                current_user=_user(agent),
                db=db,
            )

    assert exc.value.status_code == 402
    assert exc.value.detail["quota_type"] == "max_agents"
    assert agent.legacy_assistant_state is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_from_employee_closes_shared_access_and_keeps_history_id() -> None:
    agent = _agent(
        legacy_assistant_state="converted",
        access_mode="company",
        company_access_level="use",
        status="stopped",
    )
    original_id = agent.id
    db = _db()
    response = object()
    common = _common_patches(agent, response)
    with common[0], common[1], common[2], common[3]:
        await agents_api.update_legacy_assistant_disposition(
            agent.id,
            LegacyAssistantDispositionUpdate(
                action="restore_history",
                expected_disposition="converted",
            ),
            current_user=_user(agent),
            db=db,
        )

    assert agent.id == original_id
    assert agent.legacy_assistant_state is None
    assert agent.access_mode == "private"
    assert agent.company_access_level == "manage"
    db.execute.assert_awaited_once()
    permission = next(
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], AgentPermission)
    )
    assert permission.scope_id == agent.creator_id
    assert permission.access_level == "manage"


@pytest.mark.asyncio
async def test_non_creator_cannot_change_private_history_even_with_manage_access() -> None:
    agent = _agent()
    actor = SimpleNamespace(id=uuid.uuid4(), tenant_id=agent.tenant_id, role="org_admin")
    db = _db()
    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    ):
        with pytest.raises(HTTPException) as exc:
            await agents_api.update_legacy_assistant_disposition(
                agent.id,
                LegacyAssistantDispositionUpdate(
                    action="archive",
                    expected_disposition="active",
                ),
                current_user=actor,
                db=db,
            )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "legacy_assistant_owner_required"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_archived_assistant_cannot_start_or_recover() -> None:
    agent = _agent(legacy_assistant_state="archived", status="stopped")
    db = _db()
    user = _user(agent)
    with patch.object(
        agents_api,
        "check_agent_access",
        AsyncMock(return_value=(agent, "manage")),
    ):
        with pytest.raises(HTTPException) as start_exc:
            await agents_api.start_agent(agent.id, current_user=user, db=db)
        with pytest.raises(HTTPException) as recover_exc:
            await agents_api.recover_agent(agent.id, current_user=user, db=db)

    assert start_exc.value.detail["code"] == "legacy_assistant_archived"
    assert recover_exc.value.detail["code"] == "legacy_assistant_archived"
