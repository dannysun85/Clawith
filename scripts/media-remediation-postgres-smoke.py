#!/usr/bin/env python3
"""Prove provider-debt remediation against a real PostgreSQL transaction."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, func, select

from app.database import async_session, engine
from app.models.agent import Agent  # noqa: F401 - registers FK target metadata
from app.models.audit import ChatMessage  # noqa: F401 - registers FK target metadata
from app.models.chat_session import ChatSession  # noqa: F401 - registers FK target metadata
from app.models.llm import LLMCredential, LLMModel  # noqa: F401 - registers FK targets
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import (
    CreditBalance,
    CreditReservation,
    CreditTransaction,
)
from app.models.tenant import Tenant
from app.models.user import User  # noqa: F401 - registers FK target metadata
from app.services.media_incident_remediation import resolve_media_provider_debt


async def _load_state(task_id: uuid.UUID, reservation_id: uuid.UUID, tenant_id: uuid.UUID):
    async with async_session() as db:
        task = await db.get(MediaGenerationTask, task_id)
        reservation = await db.get(CreditReservation, reservation_id)
        balance = await db.get(CreditBalance, tenant_id)
        consume_count = (
            await db.execute(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.tenant_id == tenant_id,
                    CreditTransaction.reason == "consume",
                    CreditTransaction.ref_type == "reservation",
                    CreditTransaction.ref_id == reservation_id,
                )
            )
        ).scalar_one()
        refund_count = (
            await db.execute(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.tenant_id == tenant_id,
                    CreditTransaction.reason == "refund",
                    CreditTransaction.ref_type == "media_task",
                    CreditTransaction.ref_id == task_id,
                )
            )
        ).scalar_one()
        return task, reservation, balance, int(consume_count), int(refund_count)


async def main() -> None:
    tenant_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with async_session() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Media remediation PostgreSQL smoke",
                slug=f"media-remediation-{tenant_id.hex[:12]}",
                im_provider="web_only",
                is_active=True,
            )
        )
        # These lightweight models intentionally have no ORM relationships.
        # Flush FK parents explicitly so this smoke test never depends on
        # SQLAlchemy's mapper ordering for unrelated objects.
        await db.flush()
        db.add(CreditBalance(tenant_id=tenant_id, balance=1000, reserved=446))
        db.add(
            CreditReservation(
                id=reservation_id,
                tenant_id=tenant_id,
                action="video_generation",
                modality="video",
                tier="ultra",
                provider="minimax",
                model="MiniMax-Hailuo-2.3",
                amount=446,
                status="provider_inflight",
                ref_type="media_task",
                ref_id=task_id,
            )
        )
        await db.flush()
        db.add(
            MediaGenerationTask(
                id=task_id,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                provider="minimax",
                modality="video",
                model="MiniMax-Hailuo-2.3",
                status="submission_ambiguous",
                metadata_path="workspace/videos/smoke.json",
                output_path="workspace/videos/smoke.mp4",
                request_metadata={},
            )
        )
        await db.commit()

    try:
        # A same-tenant but noncanonical reference must never be settled or
        # refunded through operator remediation.
        async with async_session() as db:
            reservation = await db.get(CreditReservation, reservation_id)
            assert reservation is not None
            reservation.ref_id = uuid.uuid4()
            await db.commit()
        try:
            await resolve_media_provider_debt(
                task_ids=(task_id,),
                expected_tenant_id=tenant_id,
                incident_key="PG-SMOKE-BAD-REFERENCE",
                evidence_ref="provider-ticket:bad-reference",
                resolution="provider_accepted",
                apply=True,
            )
        except ValueError as exc:
            if "reservation ownership is invalid" not in str(exc):
                raise
        else:
            raise AssertionError("non-owned media reservation unexpectedly settled")
        async with async_session() as db:
            reservation = await db.get(CreditReservation, reservation_id)
            assert reservation is not None
            reservation.ref_id = task_id
            await db.commit()

        try:
            await resolve_media_provider_debt(
                task_ids=(task_id,),
                expected_tenant_id=uuid.uuid4(),
                incident_key="PG-SMOKE-WRONG-TENANT",
                evidence_ref="provider-ticket:wrong-tenant",
                resolution="provider_accepted",
                apply=True,
            )
        except ValueError as exc:
            if "outside the expected tenant" not in str(exc):
                raise
        else:
            raise AssertionError("tenant mismatch remediation unexpectedly succeeded")

        task, reservation, balance, consume_count, refund_count = await _load_state(
            task_id, reservation_id, tenant_id
        )
        assert task.status == "submission_ambiguous"
        assert reservation.status == "provider_inflight"
        assert balance.balance == 1000 and balance.reserved == 446
        assert consume_count == 0
        assert refund_count == 0

        async def apply_once(suffix: str):
            return await resolve_media_provider_debt(
                task_ids=(task_id,),
                expected_tenant_id=tenant_id,
                incident_key=f"PG-SMOKE-{suffix}",
                evidence_ref=f"provider-ticket:{suffix}",
                resolution="provider_accepted",
                apply=True,
            )

        results = await asyncio.gather(apply_once("A"), apply_once("B"))
        assert all(result.applied for result in results)

        task, reservation, balance, consume_count, refund_count = await _load_state(
            task_id, reservation_id, tenant_id
        )
        assert task.status == "compensated"
        assert reservation.status == "finalized"
        assert balance.balance == 1000
        assert balance.reserved == 0
        assert consume_count == 1
        assert refund_count == 1
        print("media_remediation_postgres_smoke=ok")
    finally:
        async with async_session() as db:
            await db.execute(
                delete(MediaGenerationTask).where(MediaGenerationTask.id == task_id)
            )
            await db.execute(
                delete(CreditTransaction).where(
                    CreditTransaction.tenant_id == tenant_id
                )
            )
            await db.execute(
                delete(CreditReservation).where(
                    CreditReservation.id == reservation_id
                )
            )
            await db.execute(
                delete(CreditBalance).where(CreditBalance.tenant_id == tenant_id)
            )
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
