"""Agent collaboration service — Agent-to-Agent communication."""

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    build_visible_agents_query,
    evaluate_agent_relationship_status,
    get_agent_access_level_for_user_id,
    is_agent_expired,
)
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.org import AgentAgentRelationship
from app.models.user import User
from app.services.storage import store_agent_bytes


class CollaborationService:
    """Enable digital employees to collaborate with each other.

    Collaboration patterns:
    1. Delegate — Agent A sends a task to Agent B
    2. Consult — Agent A asks Agent B a question and waits for response
    3. Notify — Agent A sends information to Agent B (fire-and-forget)
    """

    async def delegate_task(
        self, db: AsyncSession, from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID, task_title: str, task_description: str,
        *, requester: User,
    ) -> dict:
        """Agent A delegates a task to Agent B."""
        from app.models.task import Task

        from_agent, to_agent = await self._load_authorized_pair(
            db,
            from_agent_id,
            to_agent_id,
            requester=requester,
        )
        if to_agent.status != "running":
            raise ValueError(f"Target agent '{to_agent.name}' is not running")

        # Create task for target agent
        task = Task(
            tenant_id=to_agent.tenant_id,
            agent_id=to_agent_id,
            title=f"[委托自 {from_agent.name}] {task_title}",
            description=task_description,
            intent=task_description.strip() or task_title.strip(),
            origin_type="agent_chat",
            executor_kind="agent_employee",
            executor_snapshot={
                "agent_id": str(to_agent.id),
                "agent_name": to_agent.name,
                "role_description": to_agent.role_description or "",
                "delegated_by_agent_id": str(from_agent.id),
            },
            type="todo",
            priority="medium",
            created_by=requester.id,
            assignee="self",
        )
        db.add(task)

        # Audit log
        db.add(AuditLog(
            user_id=requester.id,
            agent_id=from_agent_id,
            action="collaboration:delegate",
            details={
                "from_agent": str(from_agent_id),
                "to_agent": str(to_agent_id),
                "task_title": task_title,
            },
        ))
        await db.flush()

        logger.info(
            "Agent {} delegated task to {} title_chars={}",
            from_agent.id,
            to_agent.id,
            len(task_title),
        )
        return {
            "task_id": str(task.id),
            "from_agent": from_agent.name,
            "to_agent": to_agent.name,
            "status": "delegated",
        }

    async def list_collaborators(
        self,
        db: AsyncSession,
        agent: Agent,
        *,
        requester: User,
    ) -> list[dict]:
        """List agents that can collaborate with the given agent.

        Returns agents from the same enterprise (same creator's org).
        """
        collaborators_result = await db.execute(
            build_visible_agents_query(
                requester,
                tenant_id=agent.tenant_id,
            ).where(
                Agent.id != agent.id,
                Agent.status.in_(["running", "stopped"]),
                Agent.deleted_at.is_(None),
            ).order_by(Agent.name)
        )
        agents = collaborators_result.scalars().all()

        return [
            {
                "id": str(a.id),
                "name": a.name,
                "role": a.role_description,
                "status": a.status,
            }
            for a in agents
        ]

    async def send_message_between_agents(
        self, db: AsyncSession, from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID, message: str, msg_type: str = "notify",
        *, requester: User,
    ) -> dict:
        """Send an inter-agent message.

        msg_type: 'notify' (fire-and-forget) or 'consult' (expects reply)
        """
        from_agent, _to_agent = await self._load_authorized_pair(
            db,
            from_agent_id,
            to_agent_id,
            requester=requester,
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        rel_path = (
            f"workspace/inbox/{timestamp}_{str(from_agent_id)[:8]}_"
            f"{uuid.uuid4().hex[:12]}.md"
        )
        await store_agent_bytes(
            to_agent_id,
            rel_path,
            f"# 来自 {from_agent.name} 的消息\n"
            f"- 类型: {msg_type}\n"
            f"- 时间: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"{message}\n".encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )

        db.add(AuditLog(
            user_id=requester.id,
            agent_id=from_agent_id,
            action=f"collaboration:{msg_type}",
            details={"to_agent": str(to_agent_id), "message_preview": message[:100]},
        ))
        await db.flush()

        return {"status": "sent", "type": msg_type}

    async def _load_authorized_pair(
        self,
        db: AsyncSession,
        from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID,
        *,
        requester: User,
    ) -> tuple[Agent, Agent]:
        """Lock and authorize one same-tenant collaboration lane."""
        if from_agent_id == to_agent_id:
            raise ValueError("An agent cannot collaborate with itself")

        result = await db.execute(
            select(Agent)
            .where(Agent.id.in_([from_agent_id, to_agent_id]))
            .order_by(Agent.id)
            .with_for_update()
        )
        agents = {agent.id: agent for agent in result.scalars().all()}
        from_agent = agents.get(from_agent_id)
        to_agent = agents.get(to_agent_id)
        if from_agent is None or to_agent is None:
            raise ValueError("Agent not found")
        if (
            requester.tenant_id is None
            or from_agent.tenant_id != requester.tenant_id
            or to_agent.tenant_id != requester.tenant_id
            or from_agent.tenant_id != to_agent.tenant_id
        ):
            raise PermissionError("Target agent is not available for collaboration")
        if (
            from_agent.status in {"stopped", "error"}
            or to_agent.status in {"stopped", "error"}
            or is_agent_expired(from_agent)
            or is_agent_expired(to_agent)
        ):
            raise ValueError("Source or target agent is unavailable")

        source_access = await get_agent_access_level_for_user_id(
            db,
            requester.id,
            from_agent,
        )
        target_access = await get_agent_access_level_for_user_id(
            db,
            requester.id,
            to_agent,
        )
        if source_access and target_access:
            return from_agent, to_agent

        relationship = (
            await db.execute(
                select(AgentAgentRelationship).where(
                    AgentAgentRelationship.agent_id == from_agent_id,
                    AgentAgentRelationship.target_agent_id == to_agent_id,
                )
            )
        ).scalar_one_or_none()
        if relationship is not None:
            relationship_status = await evaluate_agent_relationship_status(
                db,
                relationship,
                current_user_id=requester.id,
            )
            if relationship_status.get("access_allowed"):
                return from_agent, to_agent

        raise PermissionError("Target agent is not available for collaboration")


collaboration_service = CollaborationService()
