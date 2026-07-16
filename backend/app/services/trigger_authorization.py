"""Revocation-aware authorization for autonomous trigger executions."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import get_agent_access_level_for_user_id, is_agent_expired
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User


ExecutionClaim = tuple[uuid.UUID, str]


class TriggerAuthorizationError(PermissionError):
    """A trigger principal or execution generation is no longer authorized."""


@dataclass(frozen=True)
class AuthorizedTriggerPrincipal:
    agent: Agent
    owner: User
    tenant: Tenant


def _as_uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise TriggerAuthorizationError(f"Invalid {field}") from exc


def _normalize_claims(
    claims: list[tuple[object, object]],
) -> list[ExecutionClaim]:
    normalized: list[ExecutionClaim] = []
    for execution_id, lease_owner in claims:
        token = str(lease_owner or "")
        if not token:
            raise TriggerAuthorizationError("Trigger execution lease is missing")
        normalized.append((_as_uuid(execution_id, "trigger execution"), token))
    return normalized


async def validate_active_trigger_principal(
    db: AsyncSession,
    *,
    agent_id: object,
    owner_user_id: object,
    execution_claims: list[tuple[object, object]],
    lock_authority: bool = False,
) -> AuthorizedTriggerPrincipal:
    """Fence an autonomous run against every mutable authority object.

    The exact execution generation is part of the principal.  A reclaimed
    lease, disabled trigger, paused Agent, disabled account/company, or ACL
    revocation therefore stops tools and final writes from an older worker.
    """

    parsed_agent_id = _as_uuid(agent_id, "Agent")
    parsed_owner_id = _as_uuid(owner_user_id, "owner")
    claims = _normalize_claims(execution_claims)
    if not claims:
        raise TriggerAuthorizationError("Trigger execution claim is required")

    agent_query = select(Agent).where(Agent.id == parsed_agent_id)
    owner_query = select(User).where(User.id == parsed_owner_id)
    if lock_authority:
        agent_query = agent_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
        owner_query = owner_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)

    agent = (await db.execute(agent_query)).scalar_one_or_none()
    owner = (await db.execute(owner_query)).scalar_one_or_none()
    if agent is None or owner is None or not owner.is_active:
        raise TriggerAuthorizationError("Trigger principal is unavailable")
    if agent.status not in {"running", "idle"} or is_agent_expired(agent):
        raise TriggerAuthorizationError("Agent is unavailable")
    if agent.tenant_id is None or owner.tenant_id != agent.tenant_id:
        raise TriggerAuthorizationError("Trigger principal tenant is invalid")

    tenant_query = select(Tenant).where(Tenant.id == agent.tenant_id)
    if lock_authority:
        tenant_query = tenant_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
    tenant = (await db.execute(tenant_query)).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise TriggerAuthorizationError("Company is inactive")
    if not await get_agent_access_level_for_user_id(db, owner.id, agent):
        raise TriggerAuthorizationError("Trigger owner lost Agent access")

    claim_ids = [execution_id for execution_id, _lease_owner in claims]
    execution_trigger_rows = (
        await db.execute(
            select(TriggerExecution.id, TriggerExecution.trigger_id).where(
                TriggerExecution.id.in_(claim_ids)
            )
        )
    ).all()
    if len(execution_trigger_rows) != len(claims):
        raise TriggerAuthorizationError("Trigger execution is unavailable")
    trigger_ids = {trigger_id for _execution_id, trigger_id in execution_trigger_rows}
    trigger_query = (
        select(AgentTrigger)
        .where(
            AgentTrigger.id.in_(trigger_ids),
            AgentTrigger.agent_id == agent.id,
            AgentTrigger.is_enabled.is_(True),
        )
        .order_by(AgentTrigger.id)
    )
    if lock_authority:
        trigger_query = trigger_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
    triggers = list((await db.execute(trigger_query)).scalars().all())
    if len(triggers) != len(trigger_ids):
        raise TriggerAuthorizationError("Trigger is no longer enabled")

    now = datetime.now(timezone.utc)
    claim_predicate = or_(
        *(
            and_(
                TriggerExecution.id == execution_id,
                TriggerExecution.lease_owner == lease_owner,
            )
            for execution_id, lease_owner in claims
        )
    )
    execution_query = select(TriggerExecution).where(
        claim_predicate,
        TriggerExecution.agent_id == agent.id,
        TriggerExecution.status == "processing",
        TriggerExecution.lease_expires_at.is_not(None),
        TriggerExecution.lease_expires_at >= now,
    )
    if lock_authority:
        execution_query = execution_query.execution_options(
            populate_existing=True
        ).with_for_update(read=True)
    executions = list((await db.execute(execution_query)).scalars().all())
    if len(executions) != len(claims):
        raise TriggerAuthorizationError("Trigger execution lease was lost")

    return AuthorizedTriggerPrincipal(agent=agent, owner=owner, tenant=tenant)


def build_trigger_tool_authorization_context(
    *,
    agent_id: object,
    owner_user_id: object,
    execution_claims: list[tuple[object, object]],
):
    """Return short pre/post execution fences for every trigger tool call."""

    async def _validate() -> None:
        from app.database import async_session

        async with async_session() as db:
            try:
                await validate_active_trigger_principal(
                    db,
                    agent_id=agent_id,
                    owner_user_id=owner_user_id,
                    execution_claims=execution_claims,
                    lock_authority=True,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def _authorize(_tool_name: str, _args: dict):
        await _validate()
        yield
        await _validate()

    return _authorize
