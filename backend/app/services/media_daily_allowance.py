"""Transactional accounting for provider-funded daily media allowances."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.llm import LLMCredential, MediaProviderDailyAllowanceClaim
from app.services.credential_readiness import current_credential_verification_receipt
from app.services.llm.load_balancer import credential_modality_is_blocked
from app.services.modalities import canonicalize_modalities


MINIMAX_VIDEO_DAILY_ALLOWANCE = 3
_ALLOWANCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
_ACTIVE_STATUSES = ("claimed", "accepted")


class DailyMediaAllowanceExhausted(RuntimeError):
    """The selected provider credential has no daily allowance remaining."""


@dataclass(frozen=True, slots=True)
class DailyAllowanceReceipt:
    claim_id: uuid.UUID
    credential_id: uuid.UUID
    allowance_date: date
    quota: int
    used: int
    remaining: int


def current_allowance_date(now: datetime | None = None) -> date:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_ALLOWANCE_TIMEZONE).date()


def minimax_video_allowance_credential_is_eligible(
    credential: LLMCredential,
) -> bool:
    """Mirror the durable runtime gates for one MiniMax video account.

    The control plane must not advertise three free submissions for an account
    that the load balancer would reject before the transactional claim.  This
    helper deliberately covers persisted readiness only; the claim itself
    remains the concurrency-safe source of truth at submission time.
    """

    if (
        str(getattr(credential, "provider", "") or "").strip().lower()
        != "minimax"
        or getattr(credential, "tenant_id", None) is not None
        or not bool(getattr(credential, "enabled", False))
        or str(getattr(credential, "status", "") or "").strip().lower()
        != "healthy"
        or current_credential_verification_receipt(credential) is None
    ):
        return False
    daily_quota = getattr(credential, "daily_quota", None)
    if daily_quota is not None and int(getattr(credential, "used_today", 0) or 0) >= int(
        daily_quota or 0
    ):
        return False
    capabilities = getattr(credential, "capabilities", None)
    if capabilities is not None:
        declared = set(canonicalize_modalities(capabilities))
        if "video" not in declared and "multimodal" not in declared:
            return False
    return not credential_modality_is_blocked(credential, "video")


async def claim_minimax_video_allowance(
    credential_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> DailyAllowanceReceipt:
    """Atomically reserve one of the credential's three daily video uses.

    Locking the credential row serializes contenders even when there are no
    claim rows yet, closing the usual count-then-insert race.
    """

    allowance_date = current_allowance_date(now)
    async with async_session() as db:
        credential_result = await db.execute(
            select(LLMCredential)
            .where(LLMCredential.id == credential_id)
            .with_for_update()
        )
        credential = credential_result.scalar_one_or_none()
        if credential is None or str(credential.provider).lower() != "minimax":
            raise DailyMediaAllowanceExhausted(
                "MiniMax video allowance requires a current MiniMax credential"
            )
        used_result = await db.execute(
            select(func.count(MediaProviderDailyAllowanceClaim.id)).where(
                MediaProviderDailyAllowanceClaim.credential_id == credential_id,
                MediaProviderDailyAllowanceClaim.provider == "minimax",
                MediaProviderDailyAllowanceClaim.modality == "video",
                MediaProviderDailyAllowanceClaim.allowance_date == allowance_date,
                MediaProviderDailyAllowanceClaim.status.in_(_ACTIVE_STATUSES),
            )
        )
        used = int(used_result.scalar_one() or 0)
        if used >= MINIMAX_VIDEO_DAILY_ALLOWANCE:
            raise DailyMediaAllowanceExhausted(
                f"MiniMax video daily allowance exhausted ({used}/{MINIMAX_VIDEO_DAILY_ALLOWANCE})"
            )
        claim = MediaProviderDailyAllowanceClaim(
            credential_id=credential_id,
            provider="minimax",
            modality="video",
            allowance_date=allowance_date,
            quota_snapshot=MINIMAX_VIDEO_DAILY_ALLOWANCE,
            status="claimed",
        )
        db.add(claim)
        await db.flush()
        await db.commit()
        claimed = used + 1
        return DailyAllowanceReceipt(
            claim_id=claim.id,
            credential_id=credential_id,
            allowance_date=allowance_date,
            quota=MINIMAX_VIDEO_DAILY_ALLOWANCE,
            used=claimed,
            remaining=max(MINIMAX_VIDEO_DAILY_ALLOWANCE - claimed, 0),
        )


async def accept_daily_allowance_claim(
    claim_id: uuid.UUID | None,
    *,
    task_record_id: uuid.UUID | None,
    provider_task_id: str,
) -> None:
    """Bind a reserved allowance to the provider task that consumed it."""

    if claim_id is None:
        return
    async with async_session() as db:
        result = await db.execute(
            select(MediaProviderDailyAllowanceClaim)
            .where(MediaProviderDailyAllowanceClaim.id == claim_id)
            .with_for_update()
        )
        claim = result.scalar_one_or_none()
        if claim is None or claim.status != "claimed":
            return
        claim.status = "accepted"
        claim.task_record_id = task_record_id
        claim.provider_task_id = str(provider_task_id)[:160]
        claim.accepted_at = datetime.now(timezone.utc)
        await db.commit()


async def release_daily_allowance_claim(
    claim_id: uuid.UUID | None,
    *,
    reason: str,
) -> bool:
    """Release a pre-accept claim; accepted provider work is never released."""

    if claim_id is None:
        return False
    async with async_session() as db:
        result = await db.execute(
            select(MediaProviderDailyAllowanceClaim)
            .where(MediaProviderDailyAllowanceClaim.id == claim_id)
            .with_for_update()
        )
        claim = result.scalar_one_or_none()
        if claim is None or claim.status != "claimed":
            return False
        claim.status = "released"
        claim.released_at = datetime.now(timezone.utc)
        claim.release_reason = str(reason or "pre_accept_release")[:1000]
        await db.commit()
        return True


async def minimax_video_allowance_summary(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    credentials: list[LLMCredential] | tuple[LLMCredential, ...] | None = None,
) -> dict[str, object]:
    """Return the executable, non-sensitive MiniMax video allowance pool."""

    allowance_date = current_allowance_date(now)
    if credentials is None:
        credential_result = await db.execute(
            select(LLMCredential).where(
                LLMCredential.provider == "minimax",
                LLMCredential.tenant_id.is_(None),
            )
        )
        candidate_credentials = list(credential_result.scalars().all())
    else:
        candidate_credentials = [
            credential
            for credential in credentials
            if str(getattr(credential, "provider", "") or "").strip().lower()
            == "minimax"
            and getattr(credential, "tenant_id", None) is None
        ]
    eligible_credentials = [
        credential
        for credential in candidate_credentials
        if minimax_video_allowance_credential_is_eligible(credential)
    ]
    accounts: list[dict[str, object]] = []
    for credential in eligible_credentials:
        used_result = await db.execute(
            select(func.count(MediaProviderDailyAllowanceClaim.id)).where(
                MediaProviderDailyAllowanceClaim.credential_id == credential.id,
                MediaProviderDailyAllowanceClaim.provider == "minimax",
                MediaProviderDailyAllowanceClaim.modality == "video",
                MediaProviderDailyAllowanceClaim.allowance_date == allowance_date,
                MediaProviderDailyAllowanceClaim.status.in_(_ACTIVE_STATUSES),
            )
        )
        used = int(used_result.scalar_one() or 0)
        accounts.append(
            {
                "credential_id": str(credential.id),
                "label": str(getattr(credential, "label", None) or "MiniMax Plan"),
                "quota": MINIMAX_VIDEO_DAILY_ALLOWANCE,
                "used": used,
                "remaining": max(MINIMAX_VIDEO_DAILY_ALLOWANCE - used, 0),
            }
        )
    return {
        "allowance_date": allowance_date.isoformat(),
        "timezone": str(_ALLOWANCE_TIMEZONE),
        "quota": sum(int(item["quota"]) for item in accounts),
        "used": sum(int(item["used"]) for item in accounts),
        "remaining": sum(int(item["remaining"]) for item in accounts),
        "eligible_accounts": len(accounts),
        "excluded_accounts": len(candidate_credentials) - len(accounts),
        "accounts": accounts,
    }


__all__ = [
    "DailyAllowanceReceipt",
    "DailyMediaAllowanceExhausted",
    "MINIMAX_VIDEO_DAILY_ALLOWANCE",
    "accept_daily_allowance_claim",
    "claim_minimax_video_allowance",
    "current_allowance_date",
    "minimax_video_allowance_credential_is_eligible",
    "minimax_video_allowance_summary",
    "release_daily_allowance_claim",
]
