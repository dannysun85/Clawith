"""Build a bounded, viewer-scoped read model for the workforce topology."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    build_manageable_agents_query,
    build_visible_agents_query,
)
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage
from app.models.org import AgentAgentRelationship
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.workforce_topology import (
    WorkforceTopologyActivityEdgeOut,
    WorkforceTopologyActivityOut,
    WorkforceTopologyNodeOut,
    WorkforceTopologyOut,
    WorkforceTopologyRelationshipEdgeOut,
)
from app.services.product_roles import resolve_agent_product_roles


SAFE_MEMBER_ACTIVITY_SUMMARIES = {
    "heartbeat": "Heartbeat completed",
    "oneshot_task": "One-time task completed",
    "schedule_run": "Scheduled work completed",
    "task_updated": "Task updated",
    "tool_call": "Tool executed",
    "file_written": "Generated file",
}
COMPANY_AUDIT_ROLES = frozenset({"platform_admin", "org_admin"})


def _canonical_pair(
    first: uuid.UUID,
    second: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    return (first, second) if first.int < second.int else (second, first)


def merge_topology_activity_edges(
    rows: Iterable[tuple[uuid.UUID | None, uuid.UUID | None, int, datetime | None]],
    *,
    employee_ids: set[uuid.UUID],
) -> list[WorkforceTopologyActivityEdgeOut]:
    """Merge native-chat and gateway aggregates into undirected recent edges."""

    merged: dict[tuple[uuid.UUID, uuid.UUID], tuple[int, datetime]] = {}
    for first, second, count, last_activity_at in rows:
        if (
            first is None
            or second is None
            or first == second
            or first not in employee_ids
            or second not in employee_ids
            or last_activity_at is None
            or int(count or 0) <= 0
        ):
            continue
        key = _canonical_pair(first, second)
        previous_count, previous_at = merged.get(key, (0, last_activity_at))
        merged[key] = (
            previous_count + int(count),
            max(previous_at, last_activity_at),
        )

    return sorted(
        (
            WorkforceTopologyActivityEdgeOut(
                agent_a_id=first,
                agent_b_id=second,
                interaction_count=count,
                last_activity_at=last_activity_at,
            )
            for (first, second), (count, last_activity_at) in merged.items()
        ),
        key=lambda edge: (edge.last_activity_at, edge.agent_a_id.int, edge.agent_b_id.int),
        reverse=True,
    )


def _project_recent_activities(
    logs: Iterable[AgentActivityLog],
    *,
    auditable_agent_ids: set[uuid.UUID],
    employee_ids: set[uuid.UUID],
) -> list[WorkforceTopologyActivityOut]:
    projected: list[WorkforceTopologyActivityOut] = []
    for log in logs:
        if log.agent_id not in employee_ids or log.created_at is None:
            continue
        can_audit = log.agent_id in auditable_agent_ids
        if not can_audit and log.action_type not in SAFE_MEMBER_ACTIVITY_SUMMARIES:
            continue
        projected.append(
            WorkforceTopologyActivityOut(
                id=log.id,
                agent_id=log.agent_id,
                action_type=log.action_type,
                summary=(
                    log.summary
                    if can_audit
                    else SAFE_MEMBER_ACTIVITY_SUMMARIES[log.action_type]
                ),
                created_at=log.created_at,
            )
        )
    return projected


async def build_workforce_topology(
    db: AsyncSession,
    *,
    user: User,
    window_hours: int,
) -> WorkforceTopologyOut:
    """Return one constant-query topology projection for the current viewer."""

    tenant_id = user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="Company context is required")

    tenant = (
        await db.execute(
            select(Tenant).where(
                Tenant.id == tenant_id,
                Tenant.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Company not found")

    visible_agents = list(
        (
            await db.execute(
                build_visible_agents_query(user, tenant_id=tenant_id).order_by(
                    Agent.created_at.asc(),
                    Agent.id.asc(),
                )
            )
        ).scalars().all()
    )
    product_roles = await resolve_agent_product_roles(
        db,
        viewer_id=user.id,
        tenant_id=tenant_id,
        agents=visible_agents,
    )
    employees = [
        agent
        for agent in visible_agents
        if product_roles.get(agent.id, "agent_employee") == "agent_employee"
    ]
    employee_ids = {agent.id for agent in employees}

    if not employee_ids:
        return WorkforceTopologyOut(
            company_id=tenant.id,
            company_name=tenant.name,
            window_hours=window_hours,
            generated_at=datetime.now(timezone.utc),
        )

    manageable_agents = list(
        (
            await db.execute(
                build_manageable_agents_query(user, tenant_id=tenant_id).where(
                    Agent.id.in_(employee_ids)
                )
            )
        ).scalars().all()
    )
    manageable_ids = {agent.id for agent in manageable_agents}
    company_auditor = user.role in COMPANY_AUDIT_ROLES
    auditable_ids = employee_ids if company_auditor else manageable_ids

    activity_query = select(AgentActivityLog).where(
        AgentActivityLog.agent_id.in_(employee_ids)
    )
    if auditable_ids != employee_ids:
        activity_query = activity_query.where(
            or_(
                AgentActivityLog.agent_id.in_(auditable_ids),
                AgentActivityLog.action_type.in_(SAFE_MEMBER_ACTIVITY_SUMMARIES),
            )
        )
    activity_logs = list(
        (
            await db.execute(
                activity_query.order_by(
                    AgentActivityLog.created_at.desc(),
                    AgentActivityLog.id.desc(),
                ).limit(100)
            )
        ).scalars().all()
    )

    relationship_query = select(AgentAgentRelationship).where(
        AgentAgentRelationship.agent_id.in_(employee_ids),
        AgentAgentRelationship.target_agent_id.in_(employee_ids),
    )
    if not company_auditor:
        relationship_query = relationship_query.where(
            AgentAgentRelationship.agent_id.in_(manageable_ids),
            AgentAgentRelationship.target_agent_id.in_(manageable_ids),
        )
    relationships = list(
        (
            await db.execute(
                relationship_query.order_by(
                    func.coalesce(
                        AgentAgentRelationship.updated_at,
                        AgentAgentRelationship.created_at,
                    ).desc(),
                    AgentAgentRelationship.id.asc(),
                )
            )
        ).scalars().all()
    )

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    chat_edge_query = (
        select(
            ChatSession.agent_id,
            ChatSession.peer_agent_id,
            func.count(ChatMessage.id),
            func.max(ChatMessage.created_at),
        )
        .join(
            ChatMessage,
            ChatMessage.conversation_id == cast(ChatSession.id, String),
        )
        .where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.source_channel == "agent",
            ChatSession.deleted_at.is_(None),
            ChatSession.agent_id.in_(employee_ids),
            ChatSession.peer_agent_id.in_(employee_ids),
            ChatMessage.created_at >= since,
        )
    )
    if not company_auditor:
        chat_edge_query = chat_edge_query.where(
            or_(
                ChatSession.user_id == user.id,
                and_(
                    ChatSession.agent_id.in_(manageable_ids),
                    ChatSession.peer_agent_id.in_(manageable_ids),
                ),
            )
        )
    chat_rows = list(
        (
            await db.execute(
                chat_edge_query.group_by(
                    ChatSession.agent_id,
                    ChatSession.peer_agent_id,
                )
            )
        ).all()
    )

    gateway_edge_query = select(
        GatewayMessage.sender_agent_id,
        GatewayMessage.agent_id,
        func.count(GatewayMessage.id),
        func.max(GatewayMessage.created_at),
    ).where(
        GatewayMessage.sender_agent_id.is_not(None),
        GatewayMessage.sender_agent_id.in_(employee_ids),
        GatewayMessage.agent_id.in_(employee_ids),
        GatewayMessage.created_at >= since,
        GatewayMessage.status != "expired",
    )
    if not company_auditor:
        gateway_edge_query = gateway_edge_query.where(
            or_(
                GatewayMessage.sender_user_id == user.id,
                and_(
                    GatewayMessage.sender_agent_id.in_(manageable_ids),
                    GatewayMessage.agent_id.in_(manageable_ids),
                ),
            )
        )
    gateway_rows = list(
        (
            await db.execute(
                gateway_edge_query.group_by(
                    GatewayMessage.sender_agent_id,
                    GatewayMessage.agent_id,
                )
            )
        ).all()
    )

    return WorkforceTopologyOut(
        company_id=tenant.id,
        company_name=tenant.name,
        window_hours=window_hours,
        generated_at=datetime.now(timezone.utc),
        nodes=[
            WorkforceTopologyNodeOut(
                id=agent.id,
                name=agent.name,
                avatar_url=agent.avatar_url,
                role_description=agent.role_description or "",
                status=agent.status,
                last_active_at=agent.last_active_at,
                tokens_used_today=agent.tokens_used_today or 0,
                cache_read_tokens_today=agent.cache_read_tokens_today or 0,
                max_tokens_per_day=agent.max_tokens_per_day,
                is_expired=bool(agent.is_expired),
                is_system=bool(agent.is_system),
            )
            for agent in employees
        ],
        relationship_edges=[
            WorkforceTopologyRelationshipEdgeOut(
                id=relationship.id,
                source_agent_id=relationship.agent_id,
                target_agent_id=relationship.target_agent_id,
                relation=relationship.relation,
                updated_at=relationship.updated_at or relationship.created_at,
            )
            for relationship in relationships
            if relationship.agent_id in employee_ids
            and relationship.target_agent_id in employee_ids
            and (
                company_auditor
                or (
                    relationship.agent_id in manageable_ids
                    and relationship.target_agent_id in manageable_ids
                )
            )
        ],
        activity_edges=merge_topology_activity_edges(
            [*chat_rows, *gateway_rows],
            employee_ids=employee_ids,
        ),
        recent_activities=_project_recent_activities(
            activity_logs,
            auditable_agent_ids=auditable_ids,
            employee_ids=employee_ids,
        )[:20],
    )


__all__ = [
    "SAFE_MEMBER_ACTIVITY_SUMMARIES",
    "build_workforce_topology",
    "merge_topology_activity_edges",
]
