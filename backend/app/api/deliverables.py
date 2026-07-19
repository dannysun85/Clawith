"""Authenticated APIs for deliverable briefs, preflight, and approval state."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.models.user import User
from app.schemas.deliverable import (
    DeliverableActionIn,
    DeliverableArtifactOut,
    DeliverablePreflightIn,
    DeliverableRequestCreate,
    DeliverableRequestOut,
    DeliverableRequestUpdate,
)
from app.services.deliverable_workflows import (
    DeliverableWorkflowError,
    list_workflow_manifests,
    preflight_workflow,
    request_fingerprint,
    require_workflow,
    validate_workflow_spec,
)


router = APIRouter(prefix="/api/deliverables", tags=["deliverables"])


def _workflow_error(exc: DeliverableWorkflowError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _require_direct_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    user: User,
) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if (
        session is None
        or session.deleted_at is not None
        or session.tenant_id != user.tenant_id
        or session.agent_id != agent_id
        or session.user_id != user.id
        or session.session_type != "direct"
    ):
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


async def _request_out(db: AsyncSession, request: DeliverableRequest) -> DeliverableRequestOut:
    result = await db.execute(
        select(DeliverableArtifactRevision)
        .where(
            DeliverableArtifactRevision.request_id == request.id,
            DeliverableArtifactRevision.tenant_id == request.tenant_id,
        )
        .order_by(
            DeliverableArtifactRevision.artifact_key,
            DeliverableArtifactRevision.revision_number.desc(),
        )
    )
    artifacts = [
        DeliverableArtifactOut.model_validate(artifact)
        for artifact in result.scalars().all()
    ]
    return DeliverableRequestOut.model_validate(request).model_copy(update={"artifacts": artifacts})


async def _owned_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    user: User,
    lock: bool = False,
) -> DeliverableRequest:
    query = select(DeliverableRequest).where(
        DeliverableRequest.id == request_id,
        DeliverableRequest.tenant_id == user.tenant_id,
        DeliverableRequest.created_by_user_id == user.id,
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Deliverable request not found")
    await check_agent_access(db, user, request.agent_id)
    return request


@router.get("/workflows")
async def list_deliverable_workflows(
    current_user: User = Depends(get_current_user),
):
    del current_user
    return {"workflows": [workflow.model_dump() for workflow in list_workflow_manifests()]}


@router.post("/preflight")
async def preflight_deliverable(
    data: DeliverablePreflightIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, data.agent_id)
    if agent.tenant_id is None or agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="No access to this agent")
    try:
        workflow = require_workflow(data.work_type, data.workflow_id, data.workflow_version)
        result = await preflight_workflow(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            workflow=workflow,
            tier=data.tier,
            spec=data.spec,
        )
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return {"workflow_id": workflow.workflow_id, "workflow_version": workflow.workflow_version, **result}


@router.post("/requests", response_model=DeliverableRequestOut, status_code=status.HTTP_201_CREATED)
async def create_deliverable_request(
    data: DeliverableRequestCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, data.agent_id)
    if agent.tenant_id is None or agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="No access to this agent")
    await _require_direct_session(
        db,
        session_id=data.session_id,
        agent_id=data.agent_id,
        user=current_user,
    )
    try:
        workflow = require_workflow(data.work_type, data.workflow_id, data.workflow_version)
        normalized_spec = validate_workflow_spec(workflow, data.spec)
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc

    approval_policy = data.approval_policy or list(workflow.approval_policy)
    output_contract = data.output_contract or list(workflow.output_contract)
    if approval_policy != list(workflow.approval_policy) or output_contract != list(workflow.output_contract):
        raise HTTPException(
            status_code=422,
            detail={"code": "workflow_contract_mismatch", "message": "Workflow approval/output contract is server-owned"},
        )
    fingerprint_payload = {
        "agent_id": str(data.agent_id),
        "session_id": str(data.session_id),
        "work_type": data.work_type,
        "workflow_id": workflow.workflow_id,
        "workflow_version": workflow.workflow_version,
        "goal": data.goal.strip(),
        "inputs": [item.model_dump(mode="json") for item in data.inputs],
        "spec": normalized_spec,
        "tier": data.tier,
        "approval_policy": approval_policy,
        "output_contract": output_contract,
    }
    fingerprint = request_fingerprint(fingerprint_payload)
    existing_result = await db.execute(
        select(DeliverableRequest).where(
            DeliverableRequest.tenant_id == agent.tenant_id,
            DeliverableRequest.created_by_user_id == current_user.id,
            DeliverableRequest.client_request_id == data.client_request_id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="client_request_id was already used for another brief")
        return await _request_out(db, existing)

    request = DeliverableRequest(
        tenant_id=agent.tenant_id,
        created_by_user_id=current_user.id,
        agent_id=agent.id,
        session_id=data.session_id,
        client_request_id=data.client_request_id,
        request_fingerprint=fingerprint,
        work_type=data.work_type,
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.workflow_version,
        goal=data.goal.strip(),
        inputs=[item.model_dump(mode="json") for item in data.inputs],
        spec=normalized_spec,
        tier=data.tier,
        approval_policy=approval_policy,
        output_contract=output_contract,
        status="ready",
        current_stage="brief_confirmed",
    )
    try:
        async with db.begin_nested():
            db.add(request)
            await db.flush()
    except IntegrityError:
        concurrent_result = await db.execute(
            select(DeliverableRequest).where(
                DeliverableRequest.tenant_id == agent.tenant_id,
                DeliverableRequest.created_by_user_id == current_user.id,
                DeliverableRequest.client_request_id == data.client_request_id,
            )
        )
        concurrent = concurrent_result.scalar_one_or_none()
        if concurrent is None:
            raise
        if concurrent.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another brief",
            )
        request = concurrent
    return await _request_out(db, request)


@router.get("/requests", response_model=list[DeliverableRequestOut])
async def list_deliverable_requests(
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    query = (
        select(DeliverableRequest)
        .where(
            DeliverableRequest.tenant_id == current_user.tenant_id,
            DeliverableRequest.created_by_user_id == current_user.id,
            DeliverableRequest.agent_id == agent_id,
        )
        .order_by(DeliverableRequest.created_at.desc(), DeliverableRequest.id.desc())
        .limit(limit)
    )
    if session_id is not None:
        query = query.where(DeliverableRequest.session_id == session_id)
    result = await db.execute(query)
    return [await _request_out(db, request) for request in result.scalars().all()]


@router.get("/requests/{request_id}", response_model=DeliverableRequestOut)
async def get_deliverable_request(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user)
    return await _request_out(db, request)


@router.patch("/requests/{request_id}", response_model=DeliverableRequestOut)
async def update_deliverable_request(
    request_id: uuid.UUID,
    data: DeliverableRequestUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user, lock=True)
    if request.status not in {"draft", "ready"} or request.agent_run_id is not None:
        raise HTTPException(status_code=409, detail="Launched deliverable requests cannot be edited")
    if request.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Deliverable request changed; reload before editing")
    workflow = require_workflow(request.work_type, request.workflow_id, request.workflow_version)
    next_spec = request.spec if data.spec is None else data.spec
    try:
        next_spec = validate_workflow_spec(workflow, next_spec)
    except DeliverableWorkflowError as exc:
        raise _workflow_error(exc) from exc
    request.goal = data.goal.strip() if data.goal is not None else request.goal
    request.inputs = (
        [item.model_dump(mode="json") for item in data.inputs]
        if data.inputs is not None
        else request.inputs
    )
    request.spec = next_spec
    request.tier = data.tier or request.tier
    request.version += 1
    await db.flush()
    return await _request_out(db, request)


@router.post("/requests/{request_id}/actions", response_model=DeliverableRequestOut)
async def apply_deliverable_action(
    request_id: uuid.UUID,
    data: DeliverableActionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    request = await _owned_request(db, request_id=request_id, user=current_user, lock=True)
    if request.version != data.expected_version:
        raise HTTPException(status_code=409, detail="Deliverable request changed; reload before acting")
    if request.current_stage == "output_review":
        raise HTTPException(
            status_code=409,
            detail="Artifact verification is required before the completed run can be approved",
        )
    transitions = {
        ("draft", "submit"): ("ready", "brief_confirmed"),
        ("waiting_approval", "approve"): ("ready", "approval_granted"),
        ("waiting_approval", "request_changes"): ("draft", "changes_requested"),
        ("draft", "cancel"): ("cancelled", "cancelled"),
        ("ready", "cancel"): ("cancelled", "cancelled"),
    }
    transition = transitions.get((request.status, data.action))
    if transition is None:
        raise HTTPException(status_code=409, detail="Action is not valid for the current request state")
    request.status, request.current_stage = transition
    request.version += 1
    await db.flush()
    return await _request_out(db, request)
