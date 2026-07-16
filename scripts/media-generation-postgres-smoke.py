#!/usr/bin/env python3
"""PostgreSQL smoke for durable video storage and exactly-once Credit settlement."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import func, select

from app.database import async_session
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMCredential
from app.models.media_generation import MediaGenerationTask
from app.models.notification import Notification
from app.models.subscription import CreditBalance, CreditReservation, CreditTransaction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import agent_tools, media_generation
from app.services.media_assets import MediaContractError
from app.services.storage import StorageEntry


def _valid_mp4_fixture() -> bytes:
    """Build a decodable MP4 so the smoke exercises the real media contract."""
    with tempfile.TemporaryDirectory(prefix="astra-media-smoke-") as temp_dir:
        output_path = Path(temp_dir) / "fixture.mp4"
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-y",
                str(output_path),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Unable to create the MP4 smoke fixture: {detail}")
        return output_path.read_bytes()


def _valid_png_fixture(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


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
        assert content_type
        self.objects[key] = data

    async def read_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None


async def main() -> None:
    video_bytes = _valid_mp4_fixture()
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
            ref_type="media_task",
            ref_id=task_id,
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
        return video_bytes

    agent_tools._load_minimax_tool_credential_by_id = load_credential
    agent_tools._minimax_retrieve_file_download_url = retrieve_url
    agent_tools._minimax_download_file = download_file

    status = {"status": "Success", "file_id": "smoke-file"}
    first = await media_generation.reconcile_minimax_video_task(task_id, status_data=status)
    second = await media_generation.reconcile_minimax_video_task(task_id, status_data=status)
    assert first.status == "succeeded", first
    assert second.status == "succeeded", second

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
    assert storage.objects[f"{agent_id}/workspace/videos/smoke.mp4"] == video_bytes

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
    ] = video_bytes
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
            ref_type="media_task",
            ref_id=failure_task_id,
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

    # Exercise the synchronous image state machine against real PostgreSQL.
    # This covers object-first/DB-second restart recovery, stale lease fencing,
    # exactly-once settlement/delivery, and compensating refund semantics.
    sync_tenant_id = uuid.uuid4()
    sync_user_id = uuid.uuid4()
    sync_agent_id = uuid.uuid4()
    sync_session_id = uuid.uuid4()
    sync_topup_ref = uuid.uuid4()
    async with async_session() as db:
        db.add(Tenant(
            id=sync_tenant_id,
            name="Sync Media Smoke",
            slug=f"sync-media-smoke-{sync_tenant_id.hex[:12]}",
        ))
        await db.flush()
        db.add(User(
            id=sync_user_id,
            tenant_id=sync_tenant_id,
            display_name="Sync Media Smoke User",
            role="member",
        ))
        await db.flush()
        db.add(Agent(
            id=sync_agent_id,
            tenant_id=sync_tenant_id,
            creator_id=sync_user_id,
            name="Sync Media Smoke Agent",
            status="idle",
        ))
        await db.flush()
        db.add(ChatSession(
            id=sync_session_id,
            agent_id=sync_agent_id,
            user_id=sync_user_id,
            title="Sync Media Smoke Session",
            source_channel="web",
            is_group=False,
        ))
        db.add(CreditBalance(tenant_id=sync_tenant_id, balance=1000, reserved=0))
        db.add(CreditTransaction(
            tenant_id=sync_tenant_id,
            delta=1000,
            balance_after=1000,
            reason="topup",
            ref_type="media_smoke",
            ref_id=sync_topup_ref,
            user_id=sync_user_id,
        ))
        await db.commit()

    async def no_publish(_record_id):
        return False

    async def no_issue(*_args, **_kwargs):
        return None

    media_generation.publish_media_completion_event = no_publish
    media_generation._record_media_failure_issue = no_issue

    async def create_sync_image_task(
        record_id: uuid.UUID,
        *,
        credit_cost: int,
        output_name: str,
    ) -> MediaGenerationTask:
        return await media_generation.create_minimax_sync_media_task_record(
            record_id=record_id,
            tenant_id=sync_tenant_id,
            agent_id=sync_agent_id,
            user_id=sync_user_id,
            credential_id=credential_id,
            origin_session_id=sync_session_id,
            modality="image",
            tier="pro",
            model="image-01",
            credit_cost=credit_cost,
            output_path=f"workspace/images/{output_name}.png",
            request_metadata={
                "recovery_extension": "bin",
                "output_extension": ".png",
                "output_content_type": "image/png",
                "overlay_text": "",
                "overlay_text_sha256": hashlib.sha256(b"").hexdigest(),
                "overlay_position": "bottom",
                "brand_position": "center",
                "brand_scale": 0.42,
            },
        )

    recovery_task_id = uuid.uuid4()
    recovery_created = await create_sync_image_task(
        recovery_task_id,
        credit_cost=40,
        output_name="restart-recovery",
    )
    recovery_raw_key = str(
        (recovery_created.request_metadata or {})["recovery_asset_storage_key"]
    )
    recovery_png = _valid_png_fixture((12, 120, 220))
    # Provider response/object survived, but the acceptance/raw DB commit did
    # not. The callback's active retry transition makes the daemon pick it up.
    await media_generation.record_minimax_sync_provider_response_retry(
        recovery_task_id,
        RuntimeError("synthetic acceptance commit interruption"),
    )
    storage.objects[recovery_raw_key] = recovery_png
    async with async_session() as db:
        recovery_row = await db.get(
            MediaGenerationTask,
            recovery_task_id,
            with_for_update=True,
        )
        assert recovery_row is not None
        metadata = dict(recovery_row.request_metadata or {})
        metadata["raw_capture_deadline_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        recovery_row.request_metadata = metadata
        await db.commit()
    compensation_race = await media_generation._compensate_unrecoverable_sync_task(
        recovery_task_id,
        "stale no-raw observer",
    )
    assert compensation_race.outcome == "asset_appeared"
    recovery_results = await asyncio.gather(
        media_generation.reconcile_minimax_sync_media_task(recovery_task_id),
        media_generation.reconcile_minimax_sync_media_task(recovery_task_id),
    )
    recovery_final = await media_generation.reconcile_minimax_sync_media_task(
        recovery_task_id
    )
    assert "succeeded" in {result.status for result in recovery_results}
    assert recovery_final.status == "succeeded"

    lease_task_id = uuid.uuid4()
    lease_created = await create_sync_image_task(
        lease_task_id,
        credit_cost=50,
        output_name="lease-fence",
    )
    lease_raw_key = str(
        (lease_created.request_metadata or {})["recovery_asset_storage_key"]
    )
    lease_png_a = _valid_png_fixture((220, 20, 20))
    lease_png_b = _valid_png_fixture((20, 200, 80))
    await media_generation.store_minimax_sync_recovery_asset(
        lease_task_id,
        lease_png_b,
        content_type="image/png",
        expected_key=lease_raw_key,
    )
    claim_a_status, claim_a = await media_generation._claim_sync_local_processing(
        lease_task_id
    )
    assert claim_a_status == "claimed"
    token_a = str((claim_a.request_metadata or {})["processing_lease_token"])
    async with async_session() as db:
        lease_row = await db.get(
            MediaGenerationTask,
            lease_task_id,
            with_for_update=True,
        )
        assert lease_row is not None
        lease_row.next_poll_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    claim_b_status, claim_b = await media_generation._claim_sync_local_processing(
        lease_task_id
    )
    assert claim_b_status == "claimed"
    token_b = str((claim_b.request_metadata or {})["processing_lease_token"])
    assert token_a != token_b

    status_a = {
        "status": "Success",
        "worker": "A",
        "_astra_output_sha256": hashlib.sha256(lease_png_a).hexdigest(),
    }
    status_b = {
        "status": "Success",
        "worker": "B",
        "_astra_output_sha256": hashlib.sha256(lease_png_b).hexdigest(),
    }
    store_results = await asyncio.gather(
        media_generation._store_authoritative_media_output(
            lease_task_id,
            lease_png_a,
            content_type="image/png",
            status_data=status_a,
            expected_working_status="sync_processing",
            processing_lease_token=token_a,
        ),
        media_generation._store_authoritative_media_output(
            lease_task_id,
            lease_png_b,
            content_type="image/png",
            status_data=status_b,
            expected_working_status="sync_processing",
            processing_lease_token=token_b,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, MediaGenerationTask) for result in store_results) == 1
    assert sum(isinstance(result, MediaContractError) for result in store_results) == 1
    stale_failure_applied, _stale_task = await media_generation._record_sync_recovery_retry(
        lease_task_id,
        RuntimeError("worker A completed after its lease expired"),
        expected_working_status="sync_processing",
        processing_lease_token=token_a,
    )
    assert stale_failure_applied is False
    finalize_results = await asyncio.gather(
        media_generation._finalize_verified_success(
            lease_task_id,
            status_b,
            len(lease_png_b),
            deliver_completion=True,
        ),
        media_generation._finalize_verified_success(
            lease_task_id,
            status_b,
            len(lease_png_b),
            deliver_completion=True,
        ),
    )
    assert all(result is not None and result.status == "succeeded" for result in finalize_results)
    assert storage.objects[
        f"{sync_agent_id}/workspace/images/lease-fence.png"
    ] == lease_png_b

    compensation_task_id = uuid.uuid4()
    await create_sync_image_task(
        compensation_task_id,
        credit_cost=30,
        output_name="compensated",
    )
    await media_generation.record_minimax_sync_provider_response_retry(
        compensation_task_id,
        RuntimeError("synthetic raw capture interruption"),
    )
    async with async_session() as db:
        compensation_row = await db.get(
            MediaGenerationTask,
            compensation_task_id,
            with_for_update=True,
        )
        assert compensation_row is not None
        metadata = dict(compensation_row.request_metadata or {})
        metadata["raw_capture_deadline_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        compensation_row.request_metadata = metadata
        await db.commit()
    compensation_results = await asyncio.gather(
        media_generation.reconcile_minimax_sync_media_task(compensation_task_id),
        media_generation.reconcile_minimax_sync_media_task(compensation_task_id),
    )
    compensation_final = await media_generation.reconcile_minimax_sync_media_task(
        compensation_task_id
    )
    assert "compensated" in {result.status for result in compensation_results}
    assert compensation_final.status == "compensated"

    async with async_session() as db:
        sync_balance = await db.get(CreditBalance, sync_tenant_id)
        recovery_task = await db.get(MediaGenerationTask, recovery_task_id)
        lease_task = await db.get(MediaGenerationTask, lease_task_id)
        compensation_task = await db.get(MediaGenerationTask, compensation_task_id)
        sync_consume_count = await db.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.tenant_id == sync_tenant_id,
                CreditTransaction.reason == "consume",
            )
        )
        compensation_consume_count = await db.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.reason == "consume",
                CreditTransaction.ref_type == "reservation",
                CreditTransaction.ref_id == compensation_task.reservation_id,
            )
        )
        compensation_refund_count = await db.scalar(
            select(func.count())
            .select_from(CreditTransaction)
            .where(
                CreditTransaction.reason == "refund",
                CreditTransaction.ref_type == "media_task",
                CreditTransaction.ref_id == compensation_task_id,
            )
        )
        sync_message_count = await db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(
                ChatMessage.id.in_((
                    recovery_task.completion_message_id,
                    lease_task.completion_message_id,
                ))
            )
        )
        recovery_activity_count = await db.scalar(
            select(func.count())
            .select_from(AgentActivityLog)
            .where(AgentActivityLog.related_id == recovery_task_id)
        )
        lease_activity_count = await db.scalar(
            select(func.count())
            .select_from(AgentActivityLog)
            .where(AgentActivityLog.related_id == lease_task_id)
        )

    assert sync_balance is not None
    assert sync_balance.balance == 910 and sync_balance.reserved == 0
    assert recovery_task is not None and recovery_task.status == "succeeded"
    assert lease_task is not None and lease_task.status == "succeeded"
    assert compensation_task is not None and compensation_task.status == "compensated"
    assert sync_consume_count == 3
    assert compensation_consume_count == 1
    assert compensation_refund_count == 1
    assert sync_message_count == 2
    assert recovery_activity_count == 1
    assert lease_activity_count == 1

    # A corrupt cross-tenant binding must fail closed before touching either
    # tenant's balance or reservation state.
    corrupt_task_id = uuid.uuid4()
    corrupt_reservation_id = uuid.uuid4()
    async with async_session() as db:
        source_balance = await db.get(CreditBalance, tenant_id, with_for_update=True)
        assert source_balance is not None
        source_balance.reserved += 1
        db.add(CreditReservation(
            id=corrupt_reservation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            action="image",
            modality="image",
            tier="lite",
            provider="minimax",
            model="image-01",
            amount=1,
            status="provider_inflight",
            ref_type="media_task",
            ref_id=corrupt_task_id,
        ))
        # These fixtures intentionally do not declare an ORM relationship.
        # Flush the referenced reservation before inserting the corrupt task
        # so PostgreSQL can enforce the real foreign-key order deterministically.
        await db.flush()
        db.add(MediaGenerationTask(
            id=corrupt_task_id,
            tenant_id=sync_tenant_id,
            user_id=sync_user_id,
            agent_id=sync_agent_id,
            credential_id=credential_id,
            reservation_id=corrupt_reservation_id,
            provider="minimax",
            modality="image",
            model="image-01",
            status="submitting",
            metadata_path="workspace/media_tasks/corrupt.json",
            output_path="workspace/images/corrupt.png",
            request_metadata={
                "recovery_asset_storage_key": (
                    f"_internal/provider_recovery/minimax/sync/{sync_agent_id}/"
                    f"{corrupt_task_id}/image.bin"
                ),
            },
        ))
        await db.commit()
    try:
        await media_generation.mark_minimax_sync_provider_accepted(corrupt_task_id)
    except MediaContractError as exc:
        assert "ownership" in str(exc)
    else:
        raise AssertionError("Cross-tenant media reservation binding was accepted")
    async with async_session() as db:
        corrupt_task = await db.get(MediaGenerationTask, corrupt_task_id)
        corrupt_reservation = await db.get(CreditReservation, corrupt_reservation_id)
    assert corrupt_task is not None and corrupt_task.status == "submitting"
    assert corrupt_reservation is not None and corrupt_reservation.status == "provider_inflight"

    print("Media generation PostgreSQL video/sync exactly-once smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
