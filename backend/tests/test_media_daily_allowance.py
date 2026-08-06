from __future__ import annotations

from datetime import datetime, timezone
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.llm import LLMCredential, MediaProviderDailyAllowanceClaim
from app.services import media_daily_allowance, media_provider_routing
from app.services.media_daily_allowance import (
    DailyAllowanceReceipt,
    DailyMediaAllowanceExhausted,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.value)


class _Session:
    def __init__(self, *values):
        self.values = list(values)
        self.statements = []
        self.added = []
        self.commit = AsyncMock()
        self.flush = AsyncMock(side_effect=self._flush)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.values.pop(0))

    def add(self, value):
        self.added.append(value)

    def _flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()


def _credential(*, label: str = "MiniMax Plan") -> LLMCredential:
    return LLMCredential(
        id=uuid.uuid4(),
        provider="minimax",
        label=label,
        api_key_encrypted="encrypted",
        base_url="https://api.minimaxi.com",
        capabilities=["video"],
        modality_status={},
        status="healthy",
        enabled=True,
    )


def _verified_credential(
    *,
    label: str = "MiniMax Plan",
    capabilities: list[str] | None = None,
    status: str = "healthy",
    enabled: bool = True,
) -> LLMCredential:
    credential = _credential(label=label)
    credential.capabilities = capabilities if capabilities is not None else ["video"]
    credential.status = status
    credential.enabled = enabled
    credential.tenant_id = None
    credential.daily_quota = None
    credential.used_today = 0
    verified_at = datetime.now(timezone.utc)
    credential.last_verification_at = verified_at
    credential.verification_receipt = {
        "kind": "credential_auth_probe",
        "credential_id": str(credential.id),
        "provider": "minimax",
        "checked_at": verified_at.isoformat(),
        "ok": True,
    }
    return credential


def test_allowance_date_uses_asia_shanghai_calendar_day():
    assert media_daily_allowance.current_allowance_date(
        datetime(2026, 8, 5, 16, 30, tzinfo=timezone.utc)
    ).isoformat() == "2026-08-06"


@pytest.mark.asyncio
async def test_claim_reserves_third_slot_under_credential_row_lock(monkeypatch):
    credential = _credential()
    session = _Session(credential, 2)
    monkeypatch.setattr(media_daily_allowance, "async_session", lambda: session)

    receipt = await media_daily_allowance.claim_minimax_video_allowance(
        credential.id,
        now=datetime(2026, 8, 6, 1, tzinfo=timezone.utc),
    )

    assert receipt.quota == 3
    assert receipt.used == 3
    assert receipt.remaining == 0
    assert len(session.added) == 1
    claim = session.added[0]
    assert isinstance(claim, MediaProviderDailyAllowanceClaim)
    assert claim.status == "claimed"
    lock_sql = str(
        session.statements[0].compile(dialect=postgresql.dialect())
    ).upper()
    assert "FOR UPDATE" in lock_sql
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_rejects_fourth_slot_without_writing(monkeypatch):
    credential = _credential()
    session = _Session(credential, 3)
    monkeypatch.setattr(media_daily_allowance, "async_session", lambda: session)

    with pytest.raises(DailyMediaAllowanceExhausted, match="3/3"):
        await media_daily_allowance.claim_minimax_video_allowance(credential.id)

    assert session.added == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepted_allowance_is_never_released(monkeypatch):
    claim = MediaProviderDailyAllowanceClaim(
        id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        provider="minimax",
        modality="video",
        allowance_date=media_daily_allowance.current_allowance_date(),
        quota_snapshot=3,
        status="claimed",
    )
    task_record_id = uuid.uuid4()
    accept_session = _Session(claim)
    release_session = _Session(claim)
    sessions = iter((accept_session, release_session))
    monkeypatch.setattr(
        media_daily_allowance,
        "async_session",
        lambda: next(sessions),
    )

    await media_daily_allowance.accept_daily_allowance_claim(
        claim.id,
        task_record_id=task_record_id,
        provider_task_id="provider-task-1",
    )
    released = await media_daily_allowance.release_daily_allowance_claim(
        claim.id,
        reason="must-not-release-accepted-work",
    )

    assert claim.status == "accepted"
    assert claim.task_record_id == task_record_id
    assert claim.provider_task_id == "provider-task-1"
    assert released is False
    accept_session.commit.assert_awaited_once()
    release_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_selection_tries_next_minimax_account_after_allowance_exhaustion(
    monkeypatch,
):
    exhausted = _credential(label="exhausted")
    available = _credential(label="available")
    pick = AsyncMock(side_effect=[exhausted, available])
    claim = AsyncMock(
        side_effect=[
            DailyMediaAllowanceExhausted("3/3"),
            DailyAllowanceReceipt(
                claim_id=uuid.uuid4(),
                credential_id=available.id,
                allowance_date=media_daily_allowance.current_allowance_date(),
                quota=3,
                used=1,
                remaining=2,
            ),
        ]
    )
    monkeypatch.setattr(media_provider_routing.load_balancer, "pick_credential", pick)
    monkeypatch.setattr(media_provider_routing, "claim_minimax_video_allowance", claim)
    monkeypatch.setattr(
        media_provider_routing.llm_utils,
        "get_credential_api_key",
        lambda credential: f"key-{credential.label}",
    )

    prepared = await media_provider_routing.prepare_media_provider(
        "minimax",
        modality="video",
        saas_tier="pro",
        minimax_model="MiniMax-Hailuo-2.3",
        reserve_daily_video_allowance=True,
    )

    assert prepared.credential_id == available.id
    assert prepared.daily_allowance_used == 1
    assert prepared.daily_allowance_remaining == 2
    assert pick.await_count == 2
    assert "exclude_credential_ids" not in pick.await_args_list[0].kwargs
    assert pick.await_args_list[1].kwargs["exclude_credential_ids"] == {
        exhausted.id
    }


@pytest.mark.asyncio
async def test_allowance_summary_keeps_blocked_account_in_quota_denominator():
    eligible = _verified_credential(label="eligible")
    text_only = _verified_credential(label="text-only", capabilities=["text"])
    unverified = _verified_credential(label="unverified")
    unverified.verification_receipt = None
    blocked = _verified_credential(label="blocked")
    blocked.modality_status = {"video": {"status": "quota_exceeded"}}
    session = _Session([eligible, text_only, unverified, blocked], 1, 3)

    summary = await media_daily_allowance.minimax_video_allowance_summary(session)

    assert summary["quota"] == 6
    assert summary["used"] == 4
    assert summary["remaining"] == 2
    assert summary["tracked_accounts"] == 2
    assert summary["eligible_accounts"] == 1
    assert summary["excluded_accounts"] == 3
    assert [item["label"] for item in summary["accounts"]] == [
        "eligible",
        "blocked",
    ]
    assert [item["eligible"] for item in summary["accounts"]] == [True, False]
