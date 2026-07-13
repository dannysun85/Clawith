"""Auditable, idempotent repair for production product incidents.

The command is dry-run by default and never prints tenant display values or
credential material.  It is intentionally generic so an incident refund and a
secret-like display-field cleanup can be replayed safely after a deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.core.secret_detection import looks_like_secret
from app.database import async_session
from app.models.agent import Agent
from app.models.llm import LLMCredential, LLMModel
from app.models.subscription import CreditTransaction
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.services.credit_service import grant_credits_in_session
from app.services.llm.utils import get_credential_api_key, get_model_api_key


INCIDENT_NAMESPACE = uuid.UUID("faad6d69-6931-47dd-a975-88091091d927")


@dataclass
class RemediationResult:
    applied: bool
    refund_created: bool = False
    refund_credits: int = 0
    trigger_disabled: bool = False
    sanitized_tenants: int = 0
    disabled_credentials: int = 0
    disabled_models: int = 0


def incident_reference(incident_key: str) -> uuid.UUID:
    normalized = incident_key.strip()
    if not normalized:
        raise ValueError("incident_key is required for a refund")
    return uuid.uuid5(INCIDENT_NAMESPACE, normalized)


def _matches_any_secret(candidates: list[str], secret: str) -> bool:
    return bool(secret) and any(
        hmac.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8"))
        for candidate in candidates
    )


async def remediate(
    *,
    tenant_id: uuid.UUID | None = None,
    refund_credits: int = 0,
    incident_key: str = "",
    trigger_agent_id: uuid.UUID | None = None,
    trigger_name: str = "",
    sanitize_tenant_ids: tuple[uuid.UUID, ...] = (),
    apply: bool = False,
) -> RemediationResult:
    if refund_credits < 0:
        raise ValueError("refund_credits cannot be negative")
    if refund_credits and not tenant_id:
        raise ValueError("tenant_id is required for a refund")
    if bool(trigger_agent_id) != bool(trigger_name.strip()):
        raise ValueError("trigger_agent_id and trigger_name must be provided together")
    if trigger_agent_id and not tenant_id:
        raise ValueError("tenant_id is required when disabling a trigger")

    result = RemediationResult(applied=apply, refund_credits=refund_credits)
    refund_ref = incident_reference(incident_key) if refund_credits else None

    async with async_session() as db:
        if tenant_id:
            tenant = await db.get(Tenant, tenant_id, with_for_update=apply)
            if not tenant:
                raise ValueError("target tenant not found")

        if refund_ref and tenant_id:
            existing_refund = (
                await db.execute(
                    select(CreditTransaction.id).where(
                        CreditTransaction.tenant_id == tenant_id,
                        CreditTransaction.reason == "refund",
                        CreditTransaction.ref_type == "product_incident",
                        CreditTransaction.ref_id == refund_ref,
                    )
                )
            ).scalar_one_or_none()
            result.refund_created = existing_refund is None
            if apply and existing_refund is None:
                await grant_credits_in_session(
                    db,
                    tenant_id=tenant_id,
                    amount=refund_credits,
                    reason="refund",
                    ref_type="product_incident",
                    ref_id=refund_ref,
                )

        if trigger_agent_id and tenant_id:
            trigger = (
                await db.execute(
                    select(AgentTrigger)
                    .join(Agent, Agent.id == AgentTrigger.agent_id)
                    .where(
                        AgentTrigger.agent_id == trigger_agent_id,
                        AgentTrigger.name == trigger_name.strip(),
                        Agent.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not trigger:
                raise ValueError("target trigger not found in tenant")
            result.trigger_disabled = bool(trigger.is_enabled)
            if apply:
                trigger.is_enabled = False

        if sanitize_tenant_ids:
            requested = set(sanitize_tenant_ids)
            tenants = list(
                (
                    await db.execute(
                        select(Tenant).where(Tenant.id.in_(requested)).with_for_update()
                    )
                ).scalars().all()
            )
            if {tenant.id for tenant in tenants} != requested:
                raise ValueError("one or more sanitize tenant targets were not found")

            credentials = list((await db.execute(select(LLMCredential))).scalars().all())
            models = list((await db.execute(select(LLMModel))).scalars().all())
            disabled_credential_ids: set[uuid.UUID] = set()
            disabled_model_ids: set[uuid.UUID] = set()

            for tenant in tenants:
                unsafe_values = [
                    value.strip()
                    for value in (tenant.name, tenant.slug)
                    if looks_like_secret(value)
                ]
                if not unsafe_values:
                    continue
                result.sanitized_tenants += 1

                for credential in credentials:
                    if credential.id in disabled_credential_ids:
                        continue
                    if _matches_any_secret(unsafe_values, get_credential_api_key(credential)):
                        disabled_credential_ids.add(credential.id)
                        if apply:
                            credential.enabled = False
                            credential.status = "disabled"

                for model in models:
                    if model.id in disabled_model_ids:
                        continue
                    if _matches_any_secret(unsafe_values, get_model_api_key(model)):
                        disabled_model_ids.add(model.id)
                        if apply:
                            model.enabled = False

                if apply:
                    tenant.name = f"未命名企业-{tenant.id.hex[:8]}"
                    tenant.slug = f"tenant-{tenant.id.hex[:12]}"

            result.disabled_credentials = len(disabled_credential_ids)
            result.disabled_models = len(disabled_model_ids)

        if apply:
            await db.commit()
        else:
            await db.rollback()
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or apply a production incident remediation")
    parser.add_argument("--tenant-id", type=uuid.UUID)
    parser.add_argument("--refund-credits", type=int, default=0)
    parser.add_argument("--incident-key", default="")
    parser.add_argument("--trigger-agent-id", type=uuid.UUID)
    parser.add_argument("--trigger-name", default="")
    parser.add_argument("--sanitize-tenant-id", action="append", type=uuid.UUID, default=[])
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    result = await remediate(
        tenant_id=args.tenant_id,
        refund_credits=args.refund_credits,
        incident_key=args.incident_key,
        trigger_agent_id=args.trigger_agent_id,
        trigger_name=args.trigger_name,
        sanitize_tenant_ids=tuple(args.sanitize_tenant_id),
        apply=args.apply,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
