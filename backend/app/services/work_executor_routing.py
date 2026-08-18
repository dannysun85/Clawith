"""Deterministic, viewer-scoped executor routing for the Work intake surface.

The router selects only from the server-produced visible Agent roster.  It
does not create Agents, Groups, model routes, tools, or a second Runtime.  A
low-confidence request falls back to the requester's existing personal
assistant; a Group remains an explicit manual choice.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import build_visible_agents_query, is_agent_executable
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_runtime.model_route import (
    RuntimeModelRouteError,
    resolve_runtime_model_route,
)
from app.services.product_roles import resolve_agent_product_roles


WORK_ROUTING_POLICY_VERSION = "work-router-v1"
MAX_ROUTING_CANDIDATES = 200
MIN_EMPLOYEE_MATCH_SCORE = 30

_WORK_TYPE_TERMS: dict[str, tuple[str, ...]] = {
    "image": ("design", "designer", "creative", "brand", "visual", "设计", "视觉", "创意", "品牌", "美术"),
    "video": ("video", "film", "script", "creative", "视频", "影视", "脚本", "剪辑", "创意"),
    "presentation": ("presentation", "slides", "ppt", "storytelling", "演示", "汇报", "ppt", "故事线"),
    "document": ("writer", "writing", "research", "report", "analysis", "写作", "研究", "报告", "分析"),
    "general": (),
}
_DOMAIN_TERMS = (
    "finance", "financial", "accounting", "legal", "contract", "marketing", "sales",
    "research", "analysis", "writer", "writing", "design", "creative", "recruiting", "hr",
    "财务", "会计", "法务", "法律", "合同", "营销", "市场", "销售", "研究", "分析",
    "写作", "报告", "设计", "创意", "招聘", "人力",
)
_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "task", "agent", "review",
    "please", "prepare", "work", "launch", "current", "一个", "这份", "任务", "工作", "请", "进行",
}


class WorkExecutorRoutingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AutoExecutorRoute:
    agent: Agent
    chosen_executor_kind: str
    reason_codes: tuple[str, ...]
    confidence: float
    candidates_considered: tuple[dict[str, Any], ...]
    capability_snapshot: dict[str, Any]
    fallback: dict[str, Any] | None
    candidate_facts_hash: str


def _words(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in _STOP_WORDS
    }


def _match_candidate(*, title: str, intent: str, work_type: str, agent: Agent) -> tuple[int, list[str]]:
    request_text = f"{title} {intent}".casefold()
    role_text = f"{getattr(agent, 'name', '')} {getattr(agent, 'role_description', '')}".casefold()
    score = 0
    reasons: list[str] = []

    name = str(getattr(agent, "name", "") or "").strip().casefold()
    if len(name) >= 2 and name in request_text:
        score += 100
        reasons.append("explicit_agent_name_match")

    matched_work_terms = [
        term for term in _WORK_TYPE_TERMS.get(work_type, ()) if term in request_text and term in role_text
    ]
    if matched_work_terms:
        score += 45
        reasons.append(f"work_type_role_match:{matched_work_terms[0]}")

    matched_domain_terms = [term for term in _DOMAIN_TERMS if term in request_text and term in role_text]
    if matched_domain_terms:
        score += 35
        reasons.append(f"domain_role_match:{matched_domain_terms[0]}")

    overlap = sorted(_words(request_text) & _words(role_text))
    if overlap:
        score += min(len(overlap), 3) * 15
        reasons.append(f"role_term_overlap:{','.join(overlap[:3])}")

    return score, reasons


def _candidate_fact(agent: Agent, *, product_role: str, auto_ready: bool) -> dict[str, Any]:
    return {
        "agent_id": str(agent.id),
        "product_role": product_role,
        "status": str(getattr(agent, "status", "") or ""),
        "access_mode": str(getattr(agent, "access_mode", "") or ""),
        "is_system": bool(getattr(agent, "is_system", False)),
        "is_expired": bool(getattr(agent, "is_expired", False)),
        "deletion_requested": getattr(agent, "deletion_requested_at", None) is not None,
        "template_id": str(getattr(agent, "template_id", None) or ""),
        "template_sync_status": str(getattr(agent, "template_sync_status", "current") or "current"),
        "preferred_tier": str(getattr(agent, "preferred_tier", None) or ""),
        "preferred_modality": str(getattr(agent, "preferred_modality", None) or "text"),
        "primary_model_id": str(getattr(agent, "primary_model_id", None) or ""),
        "fallback_model_id": str(getattr(agent, "fallback_model_id", None) or ""),
        "auto_ready": auto_ready,
        "name": str(getattr(agent, "name", "") or ""),
        "role_description": str(getattr(agent, "role_description", "") or ""),
    }


def candidate_facts_digest(facts: list[dict[str, Any]]) -> str:
    canonical = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def route_work_executor(
    db: AsyncSession,
    *,
    user: User,
    title: str,
    intent: str,
    work_type: str,
) -> AutoExecutorRoute:
    tenant_id = user.tenant_id
    if tenant_id is None:
        raise WorkExecutorRoutingError("company_context_required", "Company context is required")

    agents = list(
        (
            await db.execute(
                build_visible_agents_query(user, tenant_id=tenant_id)
                .order_by(Agent.id.asc())
            )
        ).scalars().all()
    )
    product_roles = await resolve_agent_product_roles(
        db,
        viewer_id=user.id,
        tenant_id=tenant_id,
        agents=agents,
    )

    personal_assistant: Agent | None = None
    ranked_employees: list[tuple[int, int, Agent, list[str]]] = []
    candidate_rows: list[dict[str, Any]] = []
    candidate_facts: list[dict[str, Any]] = []

    for index, agent in enumerate(agents):
        product_role = product_roles.get(agent.id, "agent_employee")
        executable = is_agent_executable(agent)
        template_status = str(getattr(agent, "template_sync_status", "current") or "current")
        template_ready = getattr(agent, "template_id", None) is None or template_status == "current"
        auto_ready = executable and template_ready and (
            product_role == "personal_assistant" or not bool(getattr(agent, "is_system", False))
        )
        score, match_reasons = _match_candidate(
            title=title,
            intent=intent,
            work_type=work_type,
            agent=agent,
        )
        row = {
            "agent_id": str(agent.id),
            "agent_name": str(getattr(agent, "name", "") or ""),
            "product_role": product_role,
            "match_score": score,
            "status": "eligible" if auto_ready else "not_ready",
            "reason_codes": match_reasons or (["low_confidence_match"] if auto_ready else ["agent_not_ready"]),
        }
        candidate_rows.append(row)
        candidate_facts.append(_candidate_fact(agent, product_role=product_role, auto_ready=auto_ready))

        if product_role == "personal_assistant":
            personal_assistant = agent
        elif product_role == "agent_employee" and auto_ready and score >= MIN_EMPLOYEE_MATCH_SCORE:
            ranked_employees.append((score, -index, agent, match_reasons))

    ranked_employees.sort(key=lambda entry: (entry[0], entry[1], entry[2].id.int), reverse=True)
    presented_candidates = candidate_rows[:MAX_ROUTING_CANDIDATES]
    if len(candidate_rows) > MAX_ROUTING_CANDIDATES:
        presented_candidates.append({
            "status": "truncated",
            "remaining_count": len(candidate_rows) - MAX_ROUTING_CANDIDATES,
            "reason_codes": ["candidate_evidence_bounded"],
        })
    fallback_attempts: list[dict[str, Any]] = []
    for score, _index, agent, match_reasons in ranked_employees:
        try:
            await resolve_runtime_model_route(agent)
        except RuntimeModelRouteError:
            fallback_attempts.append({
                "agent_id": str(agent.id),
                "reason_code": "text_route_unavailable",
            })
            for row in candidate_rows:
                if row["agent_id"] == str(agent.id):
                    row["status"] = "route_unavailable"
                    row["reason_codes"] = [*row["reason_codes"], "text_route_unavailable"]
                    break
            continue
        return AutoExecutorRoute(
            agent=agent,
            chosen_executor_kind="agent_employee",
            reason_codes=tuple(["server_auto_route", *match_reasons]),
            confidence=min(0.98, 0.70 + score / 500),
            candidates_considered=tuple(presented_candidates),
            capability_snapshot={
                "agent_executable": True,
                "text_route": "available",
                "template_sync_status": str(getattr(agent, "template_sync_status", "current") or "current"),
            },
            fallback=(
                {"attempts": fallback_attempts, "fallback_executor_kind": "personal_assistant"}
                if fallback_attempts else None
            ),
            candidate_facts_hash=candidate_facts_digest(candidate_facts),
        )

    if personal_assistant is None:
        raise WorkExecutorRoutingError(
            "personal_assistant_required",
            "Automatic routing needs an existing personal assistant fallback",
        )
    if not is_agent_executable(personal_assistant):
        raise WorkExecutorRoutingError(
            "personal_assistant_unavailable",
            "The personal assistant fallback cannot currently execute tasks",
        )

    return AutoExecutorRoute(
        agent=personal_assistant,
        chosen_executor_kind="personal_assistant",
        reason_codes=("server_auto_route", "low_confidence_personal_assistant_fallback"),
        confidence=0.55,
        candidates_considered=tuple(presented_candidates),
        capability_snapshot={
            "agent_executable": True,
            "text_route": "checked_during_preflight",
            "template_sync_status": str(
                getattr(personal_assistant, "template_sync_status", "current") or "current"
            ),
        },
        fallback={
            "used": True,
            "executor_kind": "personal_assistant",
            "reason_code": "no_high_confidence_employee_match",
            "attempts": fallback_attempts,
        },
        candidate_facts_hash=candidate_facts_digest(candidate_facts),
    )


__all__ = [
    "AutoExecutorRoute",
    "WORK_ROUTING_POLICY_VERSION",
    "WorkExecutorRoutingError",
    "candidate_facts_digest",
    "route_work_executor",
]
