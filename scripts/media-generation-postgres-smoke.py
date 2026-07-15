#!/usr/bin/env python3
"""PostgreSQL smoke for durable video storage and exactly-once Credit settlement."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

from sqlalchemy import func, select

from app.database import async_session
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.llm import LLMCredential
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.models.subscription import CreditBalance, CreditReservation, CreditTransaction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import agent_tools, media_generation
from app.services.storage import StorageEntry


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def is_file(self, key: str) -> bool:
        return key in self.objects

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.objects[key].decode(encoding, errors=errors)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = f"{key.rstrip('/')}/"
        return [
            StorageEntry(name=object_key[len(prefix):], key=object_key, is_dir=False, size=len(data))
            for object_key, data in self.objects.items()
            if object_key.startswith(prefix) and "/" not in object_key[len(prefix):]
        ]

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        self.objects[key] = content.encode(encoding)

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        assert content_type == "video/mp4"
        self.objects[key] = data


async def main() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    task_id = uuid.uuid4()
    provider_task_id = f"smoke-{uuid.uuid4()}"

    async with async_session() as db:
        db.add(Tenant(id=tenant_id, name="Media Smoke", slug=f"media-smoke-{tenant_id.hex[:12]}"))
        await db.flush()

        db.add(User(id=user_id, tenant_id=tenant_id, display_name="Media Smoke User", role="member"))
        await db.flush()

        db.add(Agent(id=agent_id, tenant_id=tenant_id, creator_id=user_id, name="Media Smoke Agent", status="idle"))
        db.add(LLMCredential(
            id=credential_id,
            provider="minimax",
            label="Media Smoke Credential",
            api_key_encrypted="not-used-by-smoke",
            capabilities=["video"],
            status="healthy",
            enabled=True,
        ))
        await db.flush()

        db.add(CreditBalance(tenant_id=tenant_id, balance=1000, reserved=490))
        db.add(CreditTransaction(
            tenant_id=tenant_id,
            delta=1000,
            balance_after=1000,
            reason="topup",
            ref_type="media_smoke",
            ref_id=task_id,
            user_id=user_id,
        ))
        db.add(CreditReservation(
            id=reservation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="video",
            modality="video",
            tier="pro",
            provider="minimax",
            model="MiniMax-Hailuo-2.3",
            amount=490,
            status="reserved",
        ))
        await db.flush()

        db.add(MediaGenerationTask(
            id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            credential_id=credential_id,
            reservation_id=reservation_id,
            provider="minimax",
            modality="video",
            model="MiniMax-Hailuo-2.3",
            provider_task_id=provider_task_id,
            status="submitted",
            metadata_path="workspace/videos/smoke.json",
            output_path="workspace/videos/smoke.mp4",
            request_metadata={"credit_cost": 490},
        ))
        await db.commit()

    storage = MemoryStorage()
    media_generation.get_storage_backend = lambda: storage

    async def load_credential(_credential_id):
        return SimpleNamespace(api_key="smoke-key", base_url="https://minimax.invalid")

    async def retrieve_url(*_args, **_kwargs):
        return "https://files.invalid/video"

    async def download_file(*_args, **_kwargs):
        return b"\x00\x00\x00\x18ftypmp42durable-video"

    agent_tools._load_minimax_tool_credential_by_id = load_credential
    agent_tools._minimax_retrieve_file_download_url = retrieve_url
    agent_tools._minimax_download_file = download_file

    status = {"status": "Success", "file_id": "smoke-file"}
    first = await media_generation.reconcile_minimax_video_task(task_id, status_data=status)
    second = await media_generation.reconcile_minimax_video_task(task_id, status_data=status)
    assert first.status == "succeeded"
    assert second.status == "succeeded"

    async with async_session() as db:
        balance = await db.get(CreditBalance, tenant_id)
        reservation = await db.get(CreditReservation, reservation_id)
        task = await db.get(MediaGenerationTask, task_id)
        consume_count = await db.scalar(select(func.count()).select_from(CreditTransaction).where(
            CreditTransaction.reason == "consume",
            CreditTransaction.ref_type == "reservation",
            CreditTransaction.ref_id == reservation_id,
        ))
        ledger_total = await db.scalar(select(func.coalesce(func.sum(CreditTransaction.delta), 0)).where(
            CreditTransaction.tenant_id == tenant_id,
        ))
        activity_count = await db.scalar(select(func.count()).select_from(AgentActivityLog).where(
            AgentActivityLog.related_id == task_id,
        ))
        notification_count = await db.scalar(select(func.count()).select_from(Notification).where(
            Notification.ref_id == task_id,
        ))

    assert balance is not None and balance.balance == 510 and balance.reserved == 0
    assert reservation is not None and reservation.status == "finalized"
    assert task is not None and task.status == "succeeded"
    assert consume_count == 1
    assert ledger_total == balance.balance
    assert activity_count == 1
    assert notification_count == 1
    assert storage.objects[f"{agent_id}/workspace/videos/smoke.mp4"].startswith(b"\x00\x00\x00\x18ftyp")

    # Editable legacy metadata cannot authorize a provider request or refund.
    # Prove that two concurrent scans create one durable attention record and
    # retain the hold for operator reconciliation.
    legacy_reservation_id = uuid.uuid4()
    legacy_provider_task_id = f"legacy-{uuid.uuid4()}"
    async with async_session() as db:
        balance = await db.get(CreditBalance, tenant_id, with_for_update=True)
        assert balance is not None
        balance.reserved = 1
        db.add(CreditReservation(
            id=legacy_reservation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="video",
            modality="video",
            tier="lite",
            provider="minimax",
            model="MiniMax-Hailuo-2.3",
            amount=1,
            status="reserved",
        ))
        await db.commit()

    legacy_metadata_key = f"{agent_id}/workspace/videos/legacy.json"
    storage.objects[legacy_metadata_key] = json.dumps({
        "task_id": legacy_provider_task_id,
        "credential_id": str(credential_id),
        "reservation_id": str(legacy_reservation_id),
        "save_path": "workspace/videos/legacy.mp4",
        "status": "Processing",
    }).encode()
    backfill_counts = await asyncio.gather(
        media_generation.backfill_legacy_minimax_video_tasks(),
        media_generation.backfill_legacy_minimax_video_tasks(),
    )
    async with async_session() as db:
        legacy_task_count = await db.scalar(
            select(func.count())
            .select_from(MediaGenerationTask)
            .where(MediaGenerationTask.reservation_id == legacy_reservation_id)
        )
        legacy_task = await db.scalar(
            select(MediaGenerationTask).where(
                MediaGenerationTask.reservation_id == legacy_reservation_id
            )
        )
        legacy_reservation = await db.get(CreditReservation, legacy_reservation_id)
    assert sum(backfill_counts) == 0
    assert legacy_task_count == 1
    assert legacy_task is not None and legacy_task.status == "backfill_attention"
    assert legacy_reservation is not None and legacy_reservation.status == "provider_inflight"

    # A finalized legacy reservation may be imported only when the already-paid
    # artifact exists inside that Agent's workspace. This path performs no
    # provider or credential call and remains exactly once under concurrent scans.
    finalized_reservation_id = uuid.uuid4()
    finalized_provider_task_id = f"legacy-finalized-{uuid.uuid4()}"
    async with async_session() as db:
        db.add(CreditReservation(
            id=finalized_reservation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="video",
            modality="video",
            tier="lite",
            provider="minimax",
            model="MiniMax-Hailuo-2.3",
            amount=1,
            status="finalized",
        ))
        await db.commit()

    finalized_metadata_key = f"{agent_id}/workspace/videos/legacy-finalized.json"
    storage.objects[finalized_metadata_key] = json.dumps({
        "task_id": finalized_provider_task_id,
        "credential_id": str(credential_id),
        "reservation_id": str(finalized_reservation_id),
        "downloaded_path": "workspace/videos/legacy-finalized.mp4",
        "status": "Success",
    }).encode()
    storage.objects[
        f"{agent_id}/workspace/videos/legacy-finalized.mp4"
    ] = b"\x00\x00\x00\x18ftypmp42legacy-video"
    finalized_counts = await asyncio.gather(
        media_generation.backfill_legacy_minimax_video_tasks(),
        media_generation.backfill_legacy_minimax_video_tasks(),
    )
    async with async_session() as db:
        finalized_task = await db.scalar(
            select(MediaGenerationTask).where(
                MediaGenerationTask.reservation_id == finalized_reservation_id
            )
        )
    assert sum(finalized_counts) == 1
    assert finalized_task is not None and finalized_task.status == "succeeded"
    assert finalized_task.credential_id is None
    assert finalized_task.provider_task_id is None

    # Prove the pre-acceptance terminal failure path is exactly once when two
    # workers observe the final allowed recovery error concurrently. Accepted
    # provider tasks intentionally retain their hold and keep recovering.
    failure_reservation_id = uuid.uuid4()
    failure_task_id = uuid.uuid4()
    async with async_session() as db:
        balance = await db.get(CreditBalance, tenant_id, with_for_update=True)
        assert balance is not None
        balance.reserved += 50
        db.add(CreditReservation(
            id=failure_reservation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="video",
            modality="video",
            tier="lite",
            provider="minimax",
            model="MiniMax-Hailuo-2.3",
            amount=50,
            status="reserved",
        ))
        await db.flush()
        db.add(MediaGenerationTask(
            id=failure_task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            credential_id=credential_id,
            reservation_id=failure_reservation_id,
            provider="minimax",
            modality="video",
            model="MiniMax-Hailuo-2.3",
            provider_task_id=None,
            status="retrying",
            metadata_path="workspace/videos/failure.json",
            output_path="workspace/videos/failure.mp4",
            request_metadata={"credit_cost": 50},
            consecutive_error_count=11,
        ))
        await db.commit()

    failure_results = await asyncio.gather(
        media_generation.record_media_generation_retry(
            failure_task_id,
            RuntimeError("MiniMax API error (1000): synthetic transient failure"),
        ),
        media_generation.record_media_generation_retry(
            failure_task_id,
            RuntimeError("MiniMax API error (1000): synthetic transient failure"),
        ),
    )
    assert all(result is not None and result.status == "failed" for result in failure_results)

    async with async_session() as db:
        balance = await db.get(CreditBalance, tenant_id)
        reservation = await db.get(CreditReservation, failure_reservation_id)
        failure_task = await db.get(MediaGenerationTask, failure_task_id)
        failure_consume_count = await db.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.reason == "consume",
                CreditTransaction.ref_type == "reservation",
                CreditTransaction.ref_id == failure_reservation_id,
            )
        )
        failure_notification_count = await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.ref_id == failure_task_id)
        )
        held_total = await db.scalar(
            select(func.coalesce(func.sum(CreditReservation.amount), 0)).where(
                CreditReservation.tenant_id == tenant_id,
                CreditReservation.status.in_((
                    "reserved",
                    "provider_inflight",
                    "settlement_ready",
                )),
            )
        )

    assert balance is not None and balance.balance == 510
    assert balance.reserved == held_total == 1
    assert reservation is not None and reservation.status == "released"
    assert failure_task is not None and failure_task.status == "failed"
    assert failure_task.consecutive_error_count == 12
    assert failure_consume_count == 0
    assert failure_notification_count == 1
    print("Media generation PostgreSQL success/failure exactly-once smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
