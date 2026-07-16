"""Triggers REST API — CRUD endpoints for the Aware page frontend."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import get_current_user
from app.core.permissions import check_agent_access
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.config import (
    AUTOMATIC_TRIGGER_EXECUTION_ENABLED,
    changes_on_message_binding,
    on_message_source_binding_error,
    reserved_trigger_config_keys,
    trusted_persisted_trigger_state,
    without_reserved_trigger_config,
)

router = APIRouter(prefix="/api/agents", tags=["triggers"])
REDACTED_TRIGGER_SECRET = "********"
INTERNAL_A2A_TRIGGER_NAME = "__a2a_wake__"


def _public_trigger_config(config: dict | None) -> dict:
    visible = without_reserved_trigger_config(config)
    if visible.get("secret"):
        visible["secret"] = REDACTED_TRIGGER_SECRET
    return visible


class TriggerResponse(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    reason: str
    focus_ref: str | None = None
    is_enabled: bool
    is_system: bool = False
    fire_count: int
    max_fires: int | None = None
    cooldown_seconds: int
    last_fired_at: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


class TriggerUpdate(BaseModel):
    config: dict | None = None
    reason: str | None = None
    is_enabled: bool | None = None
    max_fires: int | None = None
    cooldown_seconds: int | None = None
    expires_at: str | None = None


@router.get("/{agent_id}/triggers", response_model=list[TriggerResponse])
async def list_agent_triggers(agent_id: uuid.UUID, user=Depends(get_current_user)):
    """List all triggers for an agent."""
    async with async_session() as db:
        _, access_level = await check_agent_access(db, user, agent_id)
        if access_level != "manage":
            raise HTTPException(403, "Manage access required")
        result = await db.execute(
            select(AgentTrigger)
            .where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name != INTERNAL_A2A_TRIGGER_NAME,
            )
            .order_by(AgentTrigger.created_at.desc())
        )
        triggers = result.scalars().all()

    return [
        TriggerResponse(
            id=str(t.id),
            name=t.name,
            type=t.type,
            config=_public_trigger_config(t.config),
            reason=t.reason or "",
            focus_ref=t.focus_ref,
            is_enabled=t.is_enabled,
            is_system=t.is_system,
            fire_count=t.fire_count,
            max_fires=t.max_fires,
            cooldown_seconds=t.cooldown_seconds,
            last_fired_at=t.last_fired_at.isoformat() if t.last_fired_at else None,
            created_at=t.created_at.isoformat() if t.created_at else None,
            expires_at=t.expires_at.isoformat() if t.expires_at else None,
        )
        for t in triggers
    ]


@router.patch("/{agent_id}/triggers/{trigger_id}")
async def update_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    body: TriggerUpdate,
    user=Depends(get_current_user),
):
    """Update a trigger (from frontend management UI)."""
    async with async_session() as db:
        _, access_level = await check_agent_access(db, user, agent_id)
        if access_level != "manage":
            raise HTTPException(403, "Manage access required")
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.id == trigger_id,
                AgentTrigger.agent_id == agent_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if not trigger:
            raise HTTPException(404, "Trigger not found")
        if getattr(trigger, "name", "") == INTERNAL_A2A_TRIGGER_NAME:
            raise HTTPException(403, "Internal delivery triggers cannot be modified")
        if getattr(trigger, "is_system", False) and any(
            value is not None
            for value in (
                body.config,
                body.reason,
                body.max_fires,
                body.cooldown_seconds,
                body.expires_at,
            )
        ):
            raise HTTPException(403, "System triggers only support enable/disable")

        if body.config is not None:
            stored_config = dict(trigger.config or {})
            old_config = {
                **without_reserved_trigger_config(stored_config),
                **trusted_persisted_trigger_state(stored_config),
            }
            incoming_config = dict(body.config)
            reserved_keys = reserved_trigger_config_keys(incoming_config)
            if reserved_keys:
                raise HTTPException(
                    400,
                    "Internal trigger config fields cannot be modified: "
                    + ", ".join(reserved_keys),
                )
            if incoming_config.get("secret") == REDACTED_TRIGGER_SECRET:
                incoming_config.pop("secret")
            if trigger.type == "on_message" and changes_on_message_binding(
                stored_config,
                incoming_config,
            ):
                raise HTTPException(
                    400,
                    "Message-watch identity cannot be retargeted in place; delete and recreate the trigger from the intended conversation",
                )
            if trigger.type == "webhook":
                for protected_key in ("token", "secret"):
                    if not str(incoming_config.get(protected_key) or "").strip():
                        if old_config.get(protected_key):
                            incoming_config[protected_key] = old_config[protected_key]
            merged_config = {**old_config, **incoming_config}
            if trigger.type == "on_message":
                source_error = on_message_source_binding_error(merged_config)
                if source_error:
                    raise HTTPException(400, source_error)
            trigger.config = merged_config
        if body.reason is not None:
            trigger.reason = body.reason
        if body.is_enabled is not None:
            if body.is_enabled:
                if (
                    trigger.type == "webhook"
                    and not str((trigger.config or {}).get("secret") or "").strip()
                ):
                    raise HTTPException(
                        400,
                        "Webhook triggers require an HMAC secret before they can be enabled",
                    )
                if not AUTOMATIC_TRIGGER_EXECUTION_ENABLED:
                    raise HTTPException(
                        409,
                        "Automatic trigger execution is paused in this release",
                    )
            trigger.is_enabled = body.is_enabled
        if body.max_fires is not None:
            trigger.max_fires = body.max_fires
        if body.cooldown_seconds is not None:
            trigger.cooldown_seconds = body.cooldown_seconds
        if body.expires_at is not None:
            from datetime import datetime
            trigger.expires_at = datetime.fromisoformat(body.expires_at)

        if (
            trigger.type == "webhook"
            and trigger.is_enabled
            and not str((trigger.config or {}).get("secret") or "").strip()
        ):
            raise HTTPException(400, "Webhook triggers require an HMAC secret before they can be enabled")

        await db.commit()

    return {"ok": True}


@router.delete("/{agent_id}/triggers/{trigger_id}")
async def delete_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Delete a trigger entirely."""
    async with async_session() as db:
        _, access_level = await check_agent_access(db, user, agent_id)
        if access_level != "manage":
            raise HTTPException(403, "Manage access required")
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.id == trigger_id,
                AgentTrigger.agent_id == agent_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if not trigger:
            raise HTTPException(404, "Trigger not found")
        if getattr(trigger, "is_system", False):
            raise HTTPException(403, "System triggers cannot be deleted")

        await db.delete(trigger)
        await db.commit()

    return {"ok": True}
