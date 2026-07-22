"""Immutable model-route snapshots for durable Agent Runtime intake."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from app.models.agent import Agent


class RuntimeModelRouteError(RuntimeError):
    """The Agent cannot resolve a concrete Runtime model route."""


@dataclass(frozen=True, slots=True)
class RuntimeModelRoute:
    """Concrete primary/fallback identities captured when a Run is accepted."""

    model_id: uuid.UUID
    fallback_model_id: uuid.UUID | None
    saas_tier: str
    modality: str


async def resolve_runtime_model_route(agent: Agent) -> RuntimeModelRoute:
    """Resolve one Agent route without turning model identity into authorization.

    SaaS-tier Agents are resolved through the shared route catalog. Legacy
    Agents retain their configured concrete primary/fallback pair. The result
    is copied into the Run input so later admin edits cannot mutate an already
    accepted execution.
    """

    if agent.preferred_tier:
        from app.services.llm.caller import resolve_agent_model

        primary, fallback, route_meta = await resolve_agent_model(agent)
        if primary is None and fallback is not None:
            primary, fallback = fallback, None
        if primary is None or getattr(primary, "id", None) is None:
            raise RuntimeModelRouteError("Agent has no available SaaS model route")
        fallback_id = getattr(fallback, "id", None)
        return RuntimeModelRoute(
            model_id=primary.id,
            fallback_model_id=(
                fallback_id
                if isinstance(fallback_id, uuid.UUID) and fallback_id != primary.id
                else None
            ),
            saas_tier=(
                route_meta.saas_tier if route_meta is not None else agent.preferred_tier
            ),
            modality=(
                route_meta.modality
                if route_meta is not None
                else (agent.preferred_modality or "text")
            ),
        )

    model_id = agent.primary_model_id or agent.fallback_model_id
    if model_id is None:
        raise RuntimeModelRouteError("Agent has no legacy model route")
    fallback_model_id = (
        agent.fallback_model_id
        if agent.primary_model_id is not None
        and agent.fallback_model_id != agent.primary_model_id
        else None
    )
    return RuntimeModelRoute(
        model_id=model_id,
        fallback_model_id=fallback_model_id,
        saas_tier="",
        modality=agent.preferred_modality or "text",
    )


__all__ = [
    "RuntimeModelRoute",
    "RuntimeModelRouteError",
    "resolve_runtime_model_route",
]
