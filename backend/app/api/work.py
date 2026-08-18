"""Tenant-scoped task workbench without a second Runtime state machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can_use_agent, check_agent_access, is_agent_executable
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.audit import ChatMessage
from app.models.deliverable import (
    DeliverableArtifactRevision,
    DeliverableQualityReview,
    DeliverableRequest,
)
from app.models.onboarding import UserTenantOnboarding
from app.models.group import Group, GroupMember
from app.models.participant import Participant
from app.models.task import Task, TaskLog
from app.models.user import User
from app.schemas.work import (
    WorkArtifactSummary,
    WorkExecutorProposalOut,
    WorkInboxCountOut,
    WorkInboxOut,
    WorkIndexOut,
    WorkItemOut,
    WorkTaskDetailOut,
    WorkTaskCreate,
    WorkTaskCreateOut,
    WorkTaskDraft,
    WorkTaskPreflight,
    WorkTaskPreflightOut,
    WorkTaskRetry,
    WorkTaskRetryOut,
)
from app.services.agent_runtime.model_route import (
    RuntimeModelRouteError,
    resolve_runtime_model_route,
)
from app.services.agent_runtime.model_capabilities import (
    PlatformModelConfigurationError,
    resolve_multi_agent_planning_model,
)
from app.services.group_chat_service import (
    GroupChatServiceError,
    authorize_group_member,
    authorize_group_session,
)
from app.services.task_executor import (
    TaskRuntimeIntakeError,
    enqueue_group_task_runtime,
    enqueue_task_runtime,
)
from app.services.work_projection import (
    TERMINAL_RUN_EVENTS,
    project_execution_status,
    project_user_stage,
)
from app.services.work_deliverable_contract import work_task_deliverable_contract
from app.services.work_detail_projection import (
    collaboration_safe_work_item,
    load_work_inbox,
    load_work_inbox_actions,
    load_work_task_detail,
)
from app.services.work_executor_routing import (
    WORK_ROUTING_POLICY_VERSION,
    WorkExecutorRoutingError,
    candidate_facts_digest,
    route_work_executor,
)


router = APIRouter(prefix="/api/work", tags=["work"])


@dataclass(frozen=True, slots=True)
class _ResolvedExecutor:
    primary_agent: Agent
    agents: tuple[Agent, ...]
    snapshot: dict
    executor_kind: str


@dataclass(frozen=True, slots=True)
class _ExecutorSelection:
    resolved: _ResolvedExecutor
    proposal: WorkExecutorProposalOut
    candidate_facts_hash: str


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Company context is required")
    return user.tenant_id


def _fingerprint(data: WorkTaskDraft) -> str:
    payload = data.model_dump(
        mode="json",
        exclude={
            "client_request_id",
            "confirmation_fingerprint",
            "source_message_cursor",
        },
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _confirmation_fingerprint(
    data: WorkTaskDraft,
    *,
    agent_id: uuid.UUID,
    policy_version: str = WORK_ROUTING_POLICY_VERSION,
    chosen_executor_kind: str | None = None,
    candidate_facts_hash: str = "",
) -> str:
    evidence = {
        "request_fingerprint": _fingerprint(data),
        "policy_version": policy_version,
        "chosen_executor_kind": chosen_executor_kind or data.executor_kind or "personal_assistant",
        "agent_id": str(agent_id),
        "candidate_facts_hash": candidate_facts_hash,
    }
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_EXPECTED_OUTPUT_BY_WORK_TYPE = {
    "general": "task_result",
    "image": "confirmed_image_brief",
    "video": "confirmed_video_brief",
    "presentation": "confirmed_presentation_brief",
    "document": "confirmed_document_brief",
}


def _build_work_statement(
    data: WorkTaskDraft,
    *,
    agent: Agent,
    executor_snapshot: dict,
    resolved_executor_kind: str | None = None,
    capability_status: str = "available",
) -> dict:
    expected_output = _EXPECTED_OUTPUT_BY_WORK_TYPE[data.work_type]
    completion_criteria = [
        "Return the concrete execution result to the task workbench.",
        "Preserve the confirmed objective, executor and output boundary.",
    ]
    if data.work_type in {"image", "video", "presentation"}:
        completion_criteria.append(
            "Do not claim a formal creative artifact until a linked Deliverable passes its own preflight, review and approval gates."
        )
    statement = {
        "version": 1,
        "objective": data.intent.strip(),
        "title": data.title.strip(),
        "work_type": data.work_type,
        "expected_output": expected_output,
        "delivery_mode": "task_only",
        "priority": data.priority,
        "executor": {
            "kind": resolved_executor_kind or data.executor_kind or "personal_assistant",
            "agent_id": str(agent.id),
            "agent_name": agent.name,
            "expert_role": executor_snapshot.get("expert_role"),
            "group_id": executor_snapshot.get("group_id"),
            "group_name": executor_snapshot.get("group_name"),
            "group_session_id": executor_snapshot.get("group_session_id"),
            "group_session_title": executor_snapshot.get("group_session_title"),
            "participants": list(executor_snapshot.get("participants") or []),
        },
        "capability_preflight": {
            "status": capability_status,
            "scope": "task_execution",
            "provider_selection": "platform_managed",
        },
        "cost": {
            "estimated_credits": None,
            "basis": "usage_based_task_execution",
            "formal_media_requires_separate_preflight": data.work_type
            in {"image", "video", "presentation"},
        },
        "approval": {
            "required_to_start": False,
            "runtime_actions_checked_separately": True,
        },
        "completion_criteria": completion_criteria,
    }
    origin = executor_snapshot.get("origin")
    if isinstance(origin, dict):
        statement["origin"] = dict(origin)
    return statement


async def _personal_assistant_id(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID | None:
    return (
        await db.execute(
            select(UserTenantOnboarding.personal_assistant_agent_id).where(
                UserTenantOnboarding.tenant_id == tenant_id,
                UserTenantOnboarding.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def _resolve_executor(
    db: AsyncSession,
    *,
    data: WorkTaskDraft,
    user: User,
    lock_source: bool = False,
) -> _ResolvedExecutor:
    tenant_id = _tenant_id(user)
    if data.executor_kind == "group":
        assert data.group_id is not None
        assert data.group_session_id is not None
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "user",
                    Participant.ref_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if participant is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "group_access_denied", "message": "Group membership is required"},
            )
        try:
            session = await authorize_group_session(
                db,
                tenant_id=tenant_id,
                group_id=data.group_id,
                session_id=data.group_session_id,
                participant_id=participant.id,
                human_only=True,
            )
        except GroupChatServiceError as exc:
            response_status = 404 if exc.code.endswith("not_found") else 403
            raise HTTPException(
                status_code=response_status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        source_origin: dict | None = None
        if data.source_kind == "group_message":
            assert data.source_message_id is not None
            source_query = select(ChatMessage).where(
                ChatMessage.id == data.source_message_id,
                ChatMessage.conversation_id == str(session.id),
            )
            if lock_source:
                source_query = source_query.with_for_update()
            source_message = (await db.execute(source_query)).scalar_one_or_none()
            if source_message is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "group_source_message_not_found",
                        "message": "The source message is not part of this Group session",
                    },
                )
            if source_message.role not in {"user", "assistant"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "group_source_message_not_convertible",
                        "message": "Only persisted human or Agent messages can become formal tasks",
                    },
                )
            source_origin = {
                "kind": "group_message",
                "group_id": str(data.group_id),
                "session_id": str(session.id),
                "message_id": str(source_message.id),
                "message_cursor": (
                    f"{source_message.created_at.isoformat()}|{source_message.id}"
                    if source_message.created_at is not None
                    else str(source_message.id)
                ),
                "message_excerpt": source_message.content[:500],
            }
        group = (
            await db.execute(
                select(Group).where(
                    Group.id == data.group_id,
                    Group.tenant_id == tenant_id,
                    Group.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if group is None:
            raise HTTPException(status_code=404, detail="Group not found")

        memberships = list(
            (
                await db.execute(
                    select(GroupMember, Participant)
                    .join(Participant, Participant.id == GroupMember.participant_id)
                    .where(
                        GroupMember.group_id == group.id,
                        GroupMember.participant_id.in_(data.group_agent_participant_ids),
                        GroupMember.removed_at.is_(None),
                    )
                )
            ).all()
        )
        participant_by_id = {
            member.participant_id: member_participant
            for member, member_participant in memberships
        }
        if set(participant_by_id) != set(data.group_agent_participant_ids):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "group_agent_membership_changed",
                    "message": "One or more selected Agents are no longer Group members",
                },
            )
        ordered_participants = [
            participant_by_id[participant_id]
            for participant_id in data.group_agent_participant_ids
        ]
        if any(member_participant.type != "agent" for member_participant in ordered_participants):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "group_agent_participant_required",
                    "message": "Group task collaborators must be Agent participants",
                },
            )
        agent_ids = [member_participant.ref_id for member_participant in ordered_participants]
        agent_by_id = {
            agent.id: agent
            for agent in (
                await db.execute(
                    select(Agent).where(
                        Agent.tenant_id == tenant_id,
                        Agent.id.in_(agent_ids),
                    )
                )
            ).scalars().all()
        }
        if set(agent_by_id) != set(agent_ids):
            raise HTTPException(status_code=409, detail="A selected Group Agent is unavailable")
        agents = tuple(agent_by_id[agent_id] for agent_id in agent_ids)
        if any(not is_agent_executable(agent) for agent in agents):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "group_agent_unavailable",
                    "message": "A selected Group Agent cannot currently execute tasks",
                },
            )
        for agent in agents:
            if not await can_use_agent(db, user, agent):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "group_agent_access_denied",
                        "message": "A selected Group Agent is not available to the current user",
                    },
                )
        snapshot = {
            "agent_id": str(agents[0].id),
            "agent_name": agents[0].name,
            "group_id": str(group.id),
            "group_name": group.name,
            "group_session_id": str(session.id),
            "group_session_title": session.title,
            "sender_participant_id": str(participant.id),
            "participants": [
                {
                    "participant_id": str(member_participant.id),
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "role_description": agent.role_description or "",
                    "responsibility": (
                        "primary_owner" if index == 0 else "collaborator"
                    ),
                }
                for index, (member_participant, agent) in enumerate(
                    zip(ordered_participants, agents, strict=True)
                )
            ],
        }
        if source_origin is not None:
            snapshot["origin"] = source_origin
        return _ResolvedExecutor(
            primary_agent=agents[0],
            agents=agents,
            snapshot=snapshot,
            executor_kind="group",
        )

    if data.executor_kind is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "manual_executor_required", "message": "Manual routing requires an executor"},
        )
    if data.executor_kind == "agent_employee":
        assert data.agent_id is not None
        agent, _ = await check_agent_access(db, user, data.agent_id)
        if agent.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Agent not found")
    else:
        assistant_id = await _personal_assistant_id(
            db,
            tenant_id=tenant_id,
            user_id=user.id,
        )
        if assistant_id is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "personal_assistant_required", "onboarding_url": "/onboarding?mode=join"},
            )
        agent, _ = await check_agent_access(db, user, assistant_id)
    if not is_agent_executable(agent):
        raise HTTPException(status_code=409, detail="Selected executor is not available")

    snapshot = {
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "role_description": agent.role_description or "",
    }
    if data.executor_kind == "temporary_expert":
        snapshot.update(
            {
                "expert_role": data.expert_role,
                "scope": "task_run_only",
                "inherits_long_term_memory": False,
                "visible_as_employee": False,
            }
        )
    return _ResolvedExecutor(
        primary_agent=agent,
        agents=(agent,),
        snapshot=snapshot,
        executor_kind=data.executor_kind,
    )


def _snapshot_routing_decision(
    *,
    data: WorkTaskDraft,
    proposal: WorkExecutorProposalOut,
    candidate_facts_hash: str,
) -> dict:
    return {
        "routing_mode": data.routing_mode,
        "policy_version": proposal.policy_version,
        "chosen_executor_kind": proposal.chosen_executor_kind,
        "reason_codes": list(proposal.reason_codes),
        "confidence": proposal.confidence,
        "candidate_facts_hash": candidate_facts_hash,
        "fallback": proposal.fallback,
    }


async def _select_executor(
    db: AsyncSession,
    *,
    data: WorkTaskDraft,
    user: User,
    lock_source: bool = False,
) -> _ExecutorSelection:
    if data.routing_mode == "auto":
        try:
            route = await route_work_executor(
                db,
                user=user,
                title=data.title,
                intent=data.intent,
                work_type=data.work_type,
            )
        except WorkExecutorRoutingError as exc:
            detail = {"code": exc.code, "message": str(exc)}
            if exc.code == "personal_assistant_required":
                detail["onboarding_url"] = "/onboarding?mode=join"
            raise HTTPException(status_code=409, detail=detail) from exc

        proposal = WorkExecutorProposalOut(
            policy_version=WORK_ROUTING_POLICY_VERSION,
            chosen_executor_kind=route.chosen_executor_kind,
            agent_id=route.agent.id,
            agent_name=route.agent.name,
            reason_codes=list(route.reason_codes),
            confidence=route.confidence,
            candidates_considered=list(route.candidates_considered),
            capability_snapshot={
                **route.capability_snapshot,
                "candidate_facts_hash": route.candidate_facts_hash,
                "provider_selection": "platform_managed",
            },
            fallback=route.fallback,
        )
        snapshot = {
            "agent_id": str(route.agent.id),
            "agent_name": route.agent.name,
            "role_description": route.agent.role_description or "",
        }
        snapshot["routing_decision"] = _snapshot_routing_decision(
            data=data,
            proposal=proposal,
            candidate_facts_hash=route.candidate_facts_hash,
        )
        return _ExecutorSelection(
            resolved=_ResolvedExecutor(
                primary_agent=route.agent,
                agents=(route.agent,),
                snapshot=snapshot,
                executor_kind=route.chosen_executor_kind,
            ),
            proposal=proposal,
            candidate_facts_hash=route.candidate_facts_hash,
        )

    resolved = await _resolve_executor(
        db,
        data=data,
        user=user,
        lock_source=lock_source,
    )
    facts = [
        {
            "agent_id": str(agent.id),
            "status": str(getattr(agent, "status", "") or ""),
            "template_sync_status": str(
                getattr(agent, "template_sync_status", "current") or "current"
            ),
            "preferred_tier": str(getattr(agent, "preferred_tier", None) or ""),
            "preferred_modality": str(getattr(agent, "preferred_modality", None) or "text"),
            "primary_model_id": str(getattr(agent, "primary_model_id", None) or ""),
            "fallback_model_id": str(getattr(agent, "fallback_model_id", None) or ""),
            "deletion_requested": getattr(agent, "deletion_requested_at", None) is not None,
            "is_expired": bool(getattr(agent, "is_expired", False)),
        }
        for agent in resolved.agents
    ]
    candidate_facts_hash = candidate_facts_digest(facts)
    proposal = WorkExecutorProposalOut(
        policy_version=WORK_ROUTING_POLICY_VERSION,
        chosen_executor_kind=resolved.executor_kind,
        agent_id=resolved.primary_agent.id,
        agent_name=resolved.primary_agent.name,
        reason_codes=["manual_override"],
        confidence=1.0,
        candidates_considered=[
            {
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "status": "selected_by_user",
            }
            for agent in resolved.agents
        ],
        capability_snapshot={
            "agent_executable": True,
            "text_route": "checked_during_preflight",
            "candidate_facts_hash": candidate_facts_hash,
            "provider_selection": "platform_managed",
        },
        fallback=None,
    )
    snapshot = dict(resolved.snapshot)
    snapshot["routing_decision"] = _snapshot_routing_decision(
        data=data,
        proposal=proposal,
        candidate_facts_hash=candidate_facts_hash,
    )
    return _ExecutorSelection(
        resolved=_ResolvedExecutor(
            primary_agent=resolved.primary_agent,
            agents=resolved.agents,
            snapshot=snapshot,
            executor_kind=resolved.executor_kind,
        ),
        proposal=proposal,
        candidate_facts_hash=candidate_facts_hash,
    )


def _proposal_with_capability(
    proposal: WorkExecutorProposalOut,
    *,
    capability_status: str,
    reasons: list[str],
) -> WorkExecutorProposalOut:
    return proposal.model_copy(
        update={
            "capability_snapshot": {
                **proposal.capability_snapshot,
                "overall_status": capability_status,
                "blockers": list(reasons),
            }
        }
    )


async def _executor_capability(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    executor: _ResolvedExecutor,
) -> tuple[str, list[str], str | None]:
    reasons: list[str] = []
    for agent in executor.agents:
        try:
            await resolve_runtime_model_route(agent)
        except RuntimeModelRouteError:
            reasons.append(f"text_route_unavailable:{agent.id}")
    if len(executor.agents) > 1:
        try:
            await resolve_multi_agent_planning_model(db, tenant_id=tenant_id)
        except PlatformModelConfigurationError:
            reasons.append("group_planning_route_unavailable")
    if reasons:
        return (
            "unavailable",
            reasons,
            "ask_company_admin_to_configure_available_execution_routes",
        )
    return "available", [], None


async def _work_items(
    db: AsyncSession,
    *,
    user: User,
    limit: int,
    task_id: uuid.UUID | None = None,
    include_authorized_task: bool = False,
) -> WorkIndexOut:
    tenant_id = _tenant_id(user)
    task_query = select(Task).where(Task.tenant_id == tenant_id)
    if not (task_id is not None and include_authorized_task):
        task_query = task_query.where(Task.created_by == user.id)
    if task_id is not None:
        task_query = task_query.where(Task.id == task_id)
    tasks = list(
        (
            await db.execute(
                task_query.order_by(Task.updated_at.desc(), Task.id.desc()).limit(limit)
            )
        ).scalars().all()
    )
    task_ids = [task.id for task in tasks]
    runs = []
    if task_ids:
        task_source_ids = [str(candidate_id) for candidate_id in task_ids]
        task_correlation_ids = [f"work-task:{candidate_id}" for candidate_id in task_ids]
        runs = list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.tenant_id == tenant_id,
                        or_(
                            (
                                (AgentRun.source_type == "task")
                                & AgentRun.source_id.in_(task_source_ids)
                            ),
                            AgentRun.correlation_id.in_(task_correlation_ids),
                        ),
                    )
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                )
            ).scalars().all()
        )
    run_by_task: dict[uuid.UUID, AgentRun] = {}
    for run in runs:
        try:
            projected_task_id = (
                uuid.UUID(run.correlation_id.removeprefix("work-task:"))
                if run.correlation_id and run.correlation_id.startswith("work-task:")
                else uuid.UUID(run.source_id or "")
            )
        except ValueError:
            continue
        run_by_task.setdefault(projected_task_id, run)

    terminal_event_by_run: dict[uuid.UUID, str] = {}
    run_ids = [run.id for run in run_by_task.values()]
    if run_ids:
        events = list(
            (
                await db.execute(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.tenant_id == tenant_id,
                        AgentRunEvent.run_id.in_(run_ids),
                        AgentRunEvent.event_type.in_(tuple(TERMINAL_RUN_EVENTS)),
                    )
                    .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
                )
            ).scalars().all()
        )
        for event in events:
            terminal_event_by_run.setdefault(event.run_id, event.event_type)

    latest_log_by_task: dict[uuid.UUID, TaskLog] = {}
    if task_ids:
        logs = list(
            (
                await db.execute(
                    select(TaskLog)
                    .where(TaskLog.task_id.in_(task_ids))
                    .order_by(TaskLog.created_at.desc(), TaskLog.id.desc())
                )
            ).scalars().all()
        )
        for log in logs:
            latest_log_by_task.setdefault(log.task_id, log)

    deliverable_query = select(DeliverableRequest).where(
        DeliverableRequest.tenant_id == tenant_id,
    )
    if not (task_id is not None and include_authorized_task):
        deliverable_query = deliverable_query.where(
            DeliverableRequest.created_by_user_id == user.id,
        )
    if task_id is not None:
        deliverable_query = deliverable_query.where(DeliverableRequest.task_id == task_id)
    deliverables = list(
        (
            await db.execute(
                deliverable_query.order_by(
                    DeliverableRequest.updated_at.desc(),
                    DeliverableRequest.id.desc(),
                ).limit(limit)
            )
        ).scalars().all()
    )
    deliverable_by_task: dict[uuid.UUID, DeliverableRequest] = {}
    standalone_deliverables: list[DeliverableRequest] = []
    for request in deliverables:
        if request.task_id is not None and request.task_id in task_ids:
            deliverable_by_task.setdefault(request.task_id, request)
        else:
            standalone_deliverables.append(request)

    request_ids = [request.id for request in deliverables]
    artifacts_by_request: dict[uuid.UUID, list[DeliverableArtifactRevision]] = {}
    review_by_request: dict[uuid.UUID, DeliverableQualityReview] = {}
    if request_ids:
        artifacts = list(
            (
                await db.execute(
                    select(DeliverableArtifactRevision)
                    .where(
                        DeliverableArtifactRevision.tenant_id == tenant_id,
                        DeliverableArtifactRevision.request_id.in_(request_ids),
                    )
                    .order_by(
                        DeliverableArtifactRevision.created_at.desc(),
                        DeliverableArtifactRevision.id.desc(),
                    )
                )
            ).scalars().all()
        )
        for artifact in artifacts:
            artifacts_by_request.setdefault(artifact.request_id, []).append(artifact)
        reviews = list(
            (
                await db.execute(
                    select(DeliverableQualityReview)
                    .where(
                        DeliverableQualityReview.tenant_id == tenant_id,
                        DeliverableQualityReview.request_id.in_(request_ids),
                    )
                    .order_by(
                        DeliverableQualityReview.created_at.desc(),
                        DeliverableQualityReview.id.desc(),
                    )
                )
            ).scalars().all()
        )
        for review in reviews:
            review_by_request.setdefault(review.request_id, review)

    agent_ids = {task.agent_id for task in tasks} | {
        request.agent_id for request in deliverables
    }
    agents = {}
    if agent_ids:
        agents = {
            agent.id: agent
            for agent in (
                await db.execute(
                    select(Agent).where(
                        Agent.tenant_id == tenant_id,
                        Agent.id.in_(agent_ids),
                    )
                )
            ).scalars().all()
        }

    def artifact_summaries(request: DeliverableRequest | None) -> list[WorkArtifactSummary]:
        if request is None:
            return []
        return [
            WorkArtifactSummary.model_validate(artifact)
            for artifact in artifacts_by_request.get(request.id, [])
        ]

    def deliverable_facts(request: DeliverableRequest | None) -> tuple[str | None, str | None, str]:
        if request is None:
            return None, None, "not_requested"
        summaries = artifacts_by_request.get(request.id, [])
        artifact_status = summaries[0].status if summaries else None
        review = review_by_request.get(request.id)
        delivery_status = (
            "delivered"
            if request.status == "succeeded" and artifact_status == "approved"
            else "pending"
        )
        return artifact_status, review.status if review else None, delivery_status

    items: list[WorkItemOut] = []
    for task in tasks:
        agent = agents.get(task.agent_id)
        if agent is None:
            continue
        run = run_by_task.get(task.id)
        execution_status = project_execution_status(
            task_status=task.status,
            terminal_run_event=terminal_event_by_run.get(run.id) if run else None,
        )
        request = deliverable_by_task.get(task.id)
        latest_log = latest_log_by_task.get(task.id)
        artifact_status, review_status, delivery_status = deliverable_facts(request)
        formal_delivery_contract = work_task_deliverable_contract(task)
        items.append(
            WorkItemOut(
                id=task.id,
                kind="task",
                title=task.title,
                intent=task.intent,
                origin_type=task.origin_type,
                executor_kind=task.executor_kind,
                executor_snapshot=dict(task.executor_snapshot or {}),
                work_statement=dict(task.work_statement or {}),
                formal_delivery_spec=(
                    dict(formal_delivery_contract.spec)
                    if formal_delivery_contract is not None
                    else {}
                ),
                confirmed_at=task.confirmed_at,
                agent_id=agent.id,
                agent_name=agent.name,
                task_id=task.id,
                task_status=task.status,
                priority=task.priority,
                run_id=run.id if run else None,
                execution_status=execution_status,
                deliverable_id=request.id if request else None,
                work_type=request.work_type if request else task.work_type,
                deliverable_status=request.status if request else None,
                artifact_status=artifact_status,
                review_status=review_status,
                approval_status=(
                    "pending" if request and request.status == "waiting_approval" else None
                ),
                delivery_status=delivery_status,
                delivery_mode="formal_deliverable" if request else "task_only",
                user_stage=project_user_stage(
                    task_status=task.status,
                    execution_status=execution_status,
                    deliverable_status=request.status if request else None,
                    artifact_status=artifact_status,
                    review_status=review_status,
                ),
                artifacts=artifact_summaries(request),
                latest_update=latest_log.content if latest_log else None,
                latest_update_at=latest_log.created_at if latest_log else None,
                deep_link=(
                    f"/groups/{task.group_id}/{task.executor_snapshot.get('group_session_id')}"
                    if task.executor_kind == "group" and task.group_id is not None
                    else (
                        f"/agents/{agent.id}/chat?session_id={request.session_id}&task_id={task.id}"
                        if request
                        else f"/agents/{agent.id}/chat?task_id={task.id}"
                    )
                ),
                formal_delivery_link=(
                    f"/agents/{agent.id}/chat?task_id={task.id}"
                    if task.executor_kind == "group"
                    else (
                        f"/agents/{agent.id}/chat?session_id={request.session_id}&task_id={task.id}"
                        if request
                        else f"/agents/{agent.id}/chat?task_id={task.id}"
                    )
                ),
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )

    for request in standalone_deliverables:
        agent = agents.get(request.agent_id)
        if agent is None:
            continue
        artifact_status, review_status, delivery_status = deliverable_facts(request)
        execution_status = (
            "failed"
            if request.status == "failed"
            else "completed"
            if request.status in {"succeeded", "waiting_approval"}
            else "running"
            if request.status == "running"
            else "queued"
        )
        items.append(
            WorkItemOut(
                id=request.id,
                kind="deliverable",
                title=request.goal[:500],
                intent=request.goal,
                origin_type="agent_chat",
                executor_kind="agent_employee",
                executor_snapshot={"agent_id": str(agent.id), "agent_name": agent.name},
                work_statement={},
                formal_delivery_spec={},
                confirmed_at=None,
                agent_id=agent.id,
                agent_name=agent.name,
                priority=None,
                run_id=request.agent_run_id,
                execution_status=execution_status,
                deliverable_id=request.id,
                work_type=request.work_type,
                deliverable_status=request.status,
                artifact_status=artifact_status,
                review_status=review_status,
                approval_status="pending" if request.status == "waiting_approval" else None,
                delivery_status=delivery_status,
                delivery_mode="formal_deliverable",
                user_stage=project_user_stage(
                    task_status=None,
                    execution_status=execution_status,
                    deliverable_status=request.status,
                    artifact_status=artifact_status,
                    review_status=review_status,
                ),
                artifacts=artifact_summaries(request),
                deep_link=f"/agents/{agent.id}/chat?session_id={request.session_id}",
                formal_delivery_link=None,
                created_at=request.created_at,
                updated_at=request.updated_at,
            )
        )

    items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
    assistant_id = await _personal_assistant_id(
        db,
        tenant_id=tenant_id,
        user_id=user.id,
    )
    return WorkIndexOut(
        items=items[:limit],
        personal_assistant_agent_id=assistant_id,
    )


@router.get("", response_model=WorkIndexOut)
async def list_work(
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _work_items(db, user=current_user, limit=limit)


_WORK_INBOX_KINDS = {
    "quality_review",
    "runtime_approval",
    "delivery_approval",
    "task_recovery",
    "delivery_recovery",
}


@router.get("/inbox", response_model=WorkInboxOut)
async def get_work_inbox(
    response: Response,
    kind: str | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return unresolved actions assigned to the current human from domain facts."""

    _tenant_id(current_user)
    if kind is not None and kind not in _WORK_INBOX_KINDS:
        raise HTTPException(status_code=422, detail="Unsupported Work inbox kind")
    response.headers["Cache-Control"] = "no-store"
    return await load_work_inbox(
        db,
        user=current_user,
        limit=limit,
        cursor=cursor,
        kind=kind,
    )


@router.get("/inbox/count", response_model=WorkInboxCountOut)
async def get_work_inbox_count(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _tenant_id(current_user)
    response.headers["Cache-Control"] = "no-store"
    actions = await load_work_inbox_actions(db, user=current_user)
    return WorkInboxCountOut(count=len(actions))


@router.post("/tasks/preflight", response_model=WorkTaskPreflightOut)
async def preflight_work_task(
    data: WorkTaskPreflight,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    selection = await _select_executor(db, data=data, user=current_user)
    resolved = selection.resolved
    capability_status, reasons, next_action = await _executor_capability(
        db,
        tenant_id=_tenant_id(current_user),
        executor=resolved,
    )
    return WorkTaskPreflightOut(
        confirmation_fingerprint=_confirmation_fingerprint(
            data,
            agent_id=resolved.primary_agent.id,
            policy_version=selection.proposal.policy_version,
            chosen_executor_kind=resolved.executor_kind,
            candidate_facts_hash=selection.candidate_facts_hash,
        ),
        capability_status=capability_status,
        estimated_credits=None,
        cost_note=(
            "Usage-based task execution; formal media generation requires a separate Deliverable preflight."
        ),
        approval_required=False,
        reasons=reasons,
        next_action=next_action,
        executor_proposal=_proposal_with_capability(
            selection.proposal,
            capability_status=capability_status,
            reasons=reasons,
        ),
        work_statement=_build_work_statement(
            data,
            agent=resolved.primary_agent,
            executor_snapshot=resolved.snapshot,
            resolved_executor_kind=resolved.executor_kind,
            capability_status=capability_status,
        ),
    )


async def _work_item_for_task(
    db: AsyncSession,
    *,
    user: User,
    task_id: uuid.UUID,
    authorized_task: Task | None = None,
) -> WorkItemOut:
    if authorized_task is None:
        authorized_task = await _visible_work_task(
            db,
            user=user,
            task_id=task_id,
        )
    index = await _work_items(
        db,
        user=user,
        limit=1,
        task_id=authorized_task.id,
        include_authorized_task=True,
    )
    item = next((candidate for candidate in index.items if candidate.task_id == task_id), None)
    if item is None:
        raise HTTPException(
            status_code=409,
            detail="Task exists but cannot be projected in the current company context",
        )
    return (
        item
        if authorized_task.created_by == user.id
        else collaboration_safe_work_item(item)
    )


async def _visible_work_task(
    db: AsyncSession,
    *,
    user: User,
    task_id: uuid.UUID,
) -> Task:
    """Authorize a detail read without granting company-wide private task access."""

    tenant_id = _tenant_id(user)
    task = (
        await db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Work task not found")
    if task.created_by == user.id:
        return task

    if task.group_id is not None:
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "user",
                    Participant.ref_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if participant is not None:
            try:
                await authorize_group_member(
                    db,
                    tenant_id=tenant_id,
                    group_id=task.group_id,
                    participant_id=participant.id,
                    human_only=True,
                )
            except GroupChatServiceError:
                pass
            else:
                return task

    actions = await load_work_inbox_actions(db, user=user)
    if any(action.task_id == task.id for action in actions):
        return task
    raise HTTPException(status_code=404, detail="Work task not found")


async def _owned_work_task(
    db: AsyncSession,
    *,
    user: User,
    task_id: uuid.UUID,
    lock: bool = False,
) -> Task:
    query = select(Task).where(
        Task.id == task_id,
        Task.tenant_id == _tenant_id(user),
        Task.created_by == user.id,
    )
    if lock:
        query = query.with_for_update()
    task = (await db.execute(query)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Work task not found")
    return task


@router.get("/tasks/{task_id}", response_model=WorkItemOut)
async def get_work_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore a full owner contract or a redacted authorized collaboration summary."""

    return await _work_item_for_task(db, user=current_user, task_id=task_id)


@router.get("/tasks/{task_id}/detail", response_model=WorkTaskDetailOut)
async def get_work_task_detail(
    task_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Project every attempt/revision without changing the legacy getTask envelope."""

    task = await _visible_work_task(db, user=current_user, task_id=task_id)
    summary = await _work_item_for_task(
        db,
        user=current_user,
        task_id=task_id,
        authorized_task=task,
    )
    response.headers["Cache-Control"] = "no-store"
    return await load_work_task_detail(
        db,
        user=current_user,
        task=task,
        summary=summary,
        detail_scope=(
            "full" if getattr(task, "created_by", None) == current_user.id else "collaboration"
        ),
    )


def _group_retry_message_id(task_id: uuid.UUID, client_request_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(task_id, f"group-work-task-message:{client_request_id}")


async def _existing_retry_run(
    db: AsyncSession,
    *,
    task: Task,
    client_request_id: uuid.UUID,
) -> AgentRun | None:
    query = select(AgentRun).where(AgentRun.tenant_id == task.tenant_id)
    if task.executor_kind == "group":
        message_id = _group_retry_message_id(task.id, client_request_id)
        query = query.where(
            AgentRun.correlation_id == f"work-task:{task.id}",
            AgentRun.source_execution_id.like(f"group_mention:{message_id}:%"),
        )
    else:
        query = query.where(
            AgentRun.source_type == "task",
            AgentRun.source_execution_id
            == f"task:{task.id}:attempt:{client_request_id}",
        )
    return (
        await db.execute(query.order_by(AgentRun.created_at, AgentRun.id).limit(1))
    ).scalar_one_or_none()


async def _retry_executor(
    db: AsyncSession,
    *,
    task: Task,
    user: User,
) -> _ResolvedExecutor:
    snapshot = dict(task.executor_snapshot or {})
    if task.executor_kind != "group":
        agent, _ = await check_agent_access(db, user, task.agent_id)
        if not is_agent_executable(agent):
            raise HTTPException(status_code=409, detail="Confirmed executor is not available")
        return _ResolvedExecutor(
            primary_agent=agent,
            agents=(agent,),
            snapshot=snapshot,
            executor_kind=task.executor_kind,
        )

    participant_facts = snapshot.get("participants")
    if not isinstance(participant_facts, list) or not participant_facts:
        raise HTTPException(status_code=409, detail="Confirmed Group executor snapshot is invalid")
    try:
        group_id = task.group_id or uuid.UUID(str(snapshot.get("group_id")))
        origin = snapshot.get("origin") if isinstance(snapshot.get("origin"), dict) else {}
        session_id = uuid.UUID(
            str(snapshot.get("group_session_id") or origin.get("session_id"))
        )
        participant_ids = [
            uuid.UUID(str(participant["participant_id"]))
            for participant in participant_facts
            if isinstance(participant, dict)
        ]
        agent_ids = [
            uuid.UUID(str(participant["agent_id"]))
            for participant in participant_facts
            if isinstance(participant, dict)
        ]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Confirmed Group executor snapshot is invalid",
        ) from exc
    if (
        len(participant_ids) != len(participant_facts)
        or len(agent_ids) != len(participant_facts)
    ):
        raise HTTPException(status_code=409, detail="Confirmed Group executor snapshot is invalid")
    actor = (
        await db.execute(
            select(Participant).where(
                Participant.type == "user",
                Participant.ref_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if actor is None:
        raise HTTPException(status_code=403, detail="Active Group membership is required")
    try:
        await authorize_group_session(
            db,
            tenant_id=task.tenant_id,
            group_id=group_id,
            session_id=session_id,
            participant_id=actor.id,
            human_only=True,
        )
    except GroupChatServiceError as exc:
        response_status = 404 if exc.code.endswith("not_found") else 403
        raise HTTPException(
            status_code=response_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    memberships = list(
        (
            await db.execute(
                select(GroupMember, Participant)
                .join(Participant, Participant.id == GroupMember.participant_id)
                .where(
                    GroupMember.group_id == group_id,
                    GroupMember.participant_id.in_(participant_ids),
                    GroupMember.removed_at.is_(None),
                )
            )
        ).all()
    )
    participant_by_id = {
        member.participant_id: participant
        for member, participant in memberships
    }
    if set(participant_by_id) != set(participant_ids) or any(
        participant_by_id[participant_id].type != "agent"
        or participant_by_id[participant_id].ref_id != agent_id
        for participant_id, agent_id in zip(participant_ids, agent_ids, strict=True)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "group_retry_participant_snapshot_changed",
                "message": "A confirmed Group participant is no longer active",
            },
        )
    agents: list[Agent] = []
    for agent_id in agent_ids:
        agent, _ = await check_agent_access(db, user, agent_id)
        if not is_agent_executable(agent):
            raise HTTPException(status_code=409, detail="A confirmed Group Agent is unavailable")
        agents.append(agent)
    if not agents or agents[0].id != task.agent_id:
        raise HTTPException(status_code=409, detail="Confirmed Group owner changed")
    return _ResolvedExecutor(
        primary_agent=agents[0],
        agents=tuple(agents),
        snapshot=snapshot,
        executor_kind="group",
    )


async def _latest_task_attempt_event(
    db: AsyncSession,
    *,
    task: Task,
) -> AgentRunEvent | None:
    runs = list(
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == task.tenant_id,
                    or_(
                        (AgentRun.source_type == "task")
                        & (AgentRun.source_id == str(task.id)),
                        AgentRun.correlation_id == f"work-task:{task.id}",
                    ),
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            )
        ).scalars().all()
    )
    if not runs:
        return None
    return (
        await db.execute(
            select(AgentRunEvent)
            .where(
                AgentRunEvent.tenant_id == task.tenant_id,
                AgentRunEvent.run_id.in_([run.id for run in runs]),
                AgentRunEvent.event_type.in_(("run_failed", "run_cancelled")),
            )
            .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post("/tasks/{task_id}/retry", response_model=WorkTaskRetryOut)
async def retry_work_task(
    task_id: uuid.UUID,
    data: WorkTaskRetry,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create one idempotent new Runtime attempt for a recoverable Work task."""

    task = await _owned_work_task(
        db,
        user=current_user,
        task_id=task_id,
        lock=True,
    )
    existing_run = await _existing_retry_run(
        db,
        task=task,
        client_request_id=data.client_request_id,
    )
    if existing_run is not None:
        item = await _work_item_for_task(db, user=current_user, task_id=task.id)
        return WorkTaskRetryOut(item=item, run_id=existing_run.id, created=False)

    if task.status not in {"pending", "failed"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_retry_not_recoverable",
                "message": "Only a terminal failed or cancelled attempt can be retried",
            },
        )
    terminal_event = await _latest_task_attempt_event(db, task=task)
    if terminal_event is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_retry_not_recoverable",
                "message": "No failed or cancelled Runtime attempt was found",
            },
        )

    executor = await _retry_executor(db, task=task, user=current_user)
    capability_status, reasons, next_action = await _executor_capability(
        db,
        tenant_id=task.tenant_id,
        executor=executor,
    )
    if capability_status == "unavailable":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_retry_capability_unavailable",
                "message": "The confirmed executor cannot start a new attempt",
                "reasons": reasons,
                "next_action": next_action,
            },
        )
    try:
        if task.executor_kind == "group":
            handle = await enqueue_group_task_runtime(
                db,
                task=task,
                primary_agent=executor.primary_agent,
                execution_id=data.client_request_id,
            )
        else:
            handle = await enqueue_task_runtime(
                db,
                task=task,
                agent=executor.primary_agent,
                execution_id=data.client_request_id,
            )
    except TaskRuntimeIntakeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if handle is None:
        raise HTTPException(
            status_code=503,
            detail="Unified Agent Runtime is not enabled for tasks",
        )
    await db.commit()
    item = await _work_item_for_task(db, user=current_user, task_id=task.id)
    return WorkTaskRetryOut(item=item, run_id=handle.run_id, created=handle.created)


def _source_message_id(task: Task) -> str | None:
    snapshot = task.executor_snapshot if isinstance(task.executor_snapshot, dict) else {}
    origin = snapshot.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "group_message":
        return None
    raw = origin.get("message_id")
    return str(raw) if raw else None


async def _existing_group_source_task(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    source_message_id: uuid.UUID,
) -> Task | None:
    """Find a conversion under the source-message row lock without JSONB-only SQL."""

    candidates = list(
        (
            await db.execute(
                select(Task)
                .where(
                    Task.tenant_id == tenant_id,
                    Task.group_id == group_id,
                    Task.origin_type == "group",
                )
                .order_by(Task.created_at, Task.id)
            )
        ).scalars().all()
    )
    expected = str(source_message_id)
    return next(
        (task for task in candidates if _source_message_id(task) == expected),
        None,
    )


@router.post(
    "/tasks",
    response_model=WorkTaskCreateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_work_task(
    data: WorkTaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    fingerprint = _fingerprint(data)
    selection: _ExecutorSelection | None = None
    if data.source_kind == "group_message":
        # Lock the source message before the idempotency lookup so concurrent
        # conversions of one visible message serialize to one formal Task.
        selection = await _select_executor(
            db,
            data=data,
            user=current_user,
            lock_source=True,
        )
    existing = (
        await db.execute(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.created_by == current_user.id,
                Task.client_request_id == data.client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another task",
            )
        item = await _work_item_for_task(db, user=current_user, task_id=existing.id)
        return WorkTaskCreateOut(item=item, created=False)

    if selection is None:
        selection = await _select_executor(db, data=data, user=current_user)
    resolved = selection.resolved
    if data.source_kind == "group_message":
        assert data.source_group_id is not None
        assert data.source_message_id is not None
        source_task = await _existing_group_source_task(
            db,
            tenant_id=tenant_id,
            group_id=data.source_group_id,
            source_message_id=data.source_message_id,
        )
        if source_task is not None:
            if source_task.request_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "group_message_already_converted",
                        "message": "This Group message is already linked to a formal task",
                        "task_id": str(source_task.id),
                    },
                )
            item = await _work_item_for_task(
                db,
                user=current_user,
                task_id=source_task.id,
                authorized_task=source_task,
            )
            return WorkTaskCreateOut(item=item, created=False)
    capability_status, reasons, next_action = await _executor_capability(
        db,
        tenant_id=tenant_id,
        executor=resolved,
    )
    if capability_status == "unavailable":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_capability_changed",
                "message": "The confirmed executor route is no longer available; run preflight again.",
                "reasons": reasons,
                "next_action": next_action,
            },
        )
    expected_confirmation = _confirmation_fingerprint(
        data,
        agent_id=resolved.primary_agent.id,
        policy_version=selection.proposal.policy_version,
        chosen_executor_kind=resolved.executor_kind,
        candidate_facts_hash=selection.candidate_facts_hash,
    )
    if not hmac.compare_digest(data.confirmation_fingerprint, expected_confirmation):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "work_confirmation_stale",
                "message": "The work statement changed after preflight; review and confirm it again.",
            },
        )
    work_statement = _build_work_statement(
        data,
        agent=resolved.primary_agent,
        executor_snapshot=resolved.snapshot,
        resolved_executor_kind=resolved.executor_kind,
    )
    task = Task(
        tenant_id=tenant_id,
        agent_id=resolved.primary_agent.id,
        title=data.title.strip(),
        description=data.intent.strip(),
        intent=data.intent.strip(),
        origin_type="group" if data.source_kind == "group_message" else "workbench",
        executor_kind=resolved.executor_kind,
        executor_snapshot=resolved.snapshot,
        work_type=data.work_type,
        work_statement=work_statement,
        confirmation_fingerprint=expected_confirmation,
        confirmed_at=datetime.now(UTC),
        client_request_id=data.client_request_id,
        request_fingerprint=fingerprint,
        group_id=data.group_id if resolved.executor_kind == "group" else None,
        type="todo",
        priority=data.priority,
        created_by=current_user.id,
    )
    try:
        async with db.begin_nested():
            db.add(task)
            await db.flush()
            if data.source_kind == "group_message":
                db.add(
                    TaskLog(
                        task_id=task.id,
                        content=(
                            "Created explicitly from Group message "
                            f"{data.source_message_id} in session {data.source_session_id}"
                        ),
                    )
                )
    except IntegrityError:
        concurrent = (
            await db.execute(
                select(Task).where(
                    Task.tenant_id == tenant_id,
                    Task.created_by == current_user.id,
                    Task.client_request_id == data.client_request_id,
                )
            )
        ).scalar_one_or_none()
        if concurrent is None:
            raise
        if concurrent.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another task",
            )
        task = concurrent
        created = False
    else:
        try:
            if resolved.executor_kind == "group":
                runtime_handle = await enqueue_group_task_runtime(
                    db,
                    task=task,
                    primary_agent=resolved.primary_agent,
                )
            else:
                runtime_handle = await enqueue_task_runtime(
                    db,
                    task=task,
                    agent=resolved.primary_agent,
                )
        except TaskRuntimeIntakeError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        if runtime_handle is None:
            raise HTTPException(
                status_code=503,
                detail="Unified Agent Runtime is not enabled for tasks",
            )
        created = True
    await db.commit()

    item = await _work_item_for_task(db, user=current_user, task_id=task.id)
    return WorkTaskCreateOut(item=item, created=created)
