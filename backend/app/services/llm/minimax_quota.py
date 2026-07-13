"""MiniMax Token Plan quota polling with scoped recovery evidence.

Current Token Plan usage is shared across modalities through the provider's
``general`` allowance and 5-hour/weekly windows. Some plans also expose an
additional model-specific cap (for example a daily video entitlement), and
legacy responses may still contain separate model rows. The poller supports
both shapes and never re-enables a globally degraded credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.llm import LLMCredential
from app.services.llm.load_balancer import (
    clear_credential_modality_quota,
    mark_credential_modality_quota_exceeded,
)
from app.services.llm.utils import get_credential_api_key

CN_REMAINS_URL = "https://www.minimaxi.com/v1/token_plan/remains"
GLOBAL_REMAINS_URL = "https://www.minimax.io/v1/token_plan/remains"
POLL_TIMEOUT = 15.0
_ACTIVE_CREDENTIAL_STATUSES = ("healthy", "degraded", "quota_exceeded")


class MiniMaxQuotaPollIndeterminate(RuntimeError):
    """The provider did not return trustworthy quota evidence."""


@dataclass(frozen=True)
class MiniMaxQuotaObservation:
    modality: str
    model: str | None
    depleted: bool


def _remains_url(base_url: str | None) -> str:
    """Use the global or China remains endpoint that matches the credential."""

    normalized = str(base_url or "").strip().lower()
    if "minimax.io" in normalized and "minimaxi.com" not in normalized:
        return GLOBAL_REMAINS_URL
    return CN_REMAINS_URL


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quota_identity(model_name: Any) -> tuple[str, str | None] | None:
    """Map a provider remains row to a Clawith quota modality/model pair."""

    original = str(model_name or "").strip()
    normalized = original.lower().replace("_", "-")
    if not normalized:
        return None
    if normalized == "general" or normalized.startswith("minimax-m"):
        return "plan", None

    classifications = (
        ("video", ("video", "hailuo")),
        ("image", ("image",)),
        ("audio", ("speech", "tts", "audio")),
        ("music", ("music",)),
    )
    for modality, markers in classifications:
        if any(marker in normalized for marker in markers):
            generic_names = {
                modality,
                f"{modality}-generation",
                "speech" if modality == "audio" else "",
                "tts" if modality == "audio" else "",
            }
            return modality, None if normalized in generic_names else original
    return None


def _row_depleted(row: dict[str, Any]) -> bool | None:
    """Interpret documented status/percentage fields without guessing.

    Current MiniMax semantics use status 1 for active, 2 for exhausted, and 3
    for not subscribed. A legacy/unknown status value alone is insufficient
    evidence; a reported zero percentage is always explicit depletion.
    """

    observed = False
    depleted = False
    for scope in ("current_interval", "current_daily", "current_weekly"):
        remaining = _optional_int(row.get(f"{scope}_remaining_percent"))
        if remaining is not None:
            observed = True
            depleted = depleted or remaining <= 0

        status = _optional_int(row.get(f"{scope}_status"))
        if status in {1, 2, 3}:
            observed = True
            depleted = depleted or status in {2, 3}

    return depleted if observed else None


def _parse_quota_observations(data: Any) -> list[MiniMaxQuotaObservation]:
    """Parse only explicit, recognized provider quota evidence."""

    if not isinstance(data, dict):
        raise MiniMaxQuotaPollIndeterminate("invalid response shape")
    base_resp = data.get("base_resp") or {}
    status_code = _optional_int(base_resp.get("status_code"))
    if status_code not in (None, 0):
        # Authentication and provider errors are credential-health signals,
        # not proof that a quota bucket is depleted or recovered.
        raise MiniMaxQuotaPollIndeterminate(f"provider status {status_code}")

    rows = data.get("model_remains")
    if not isinstance(rows, list):
        raise MiniMaxQuotaPollIndeterminate("missing model_remains")

    merged: dict[tuple[str, str | None], bool] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = _quota_identity(row.get("model_name"))
        depleted = _row_depleted(row)
        if identity is None or depleted is None:
            continue
        # Duplicate rows must fail closed for that resource: one exhausted
        # window is enough to make the provider reject the call.
        merged[identity] = merged.get(identity, False) or depleted

    return [
        MiniMaxQuotaObservation(modality=modality, model=model, depleted=depleted)
        for (modality, model), depleted in merged.items()
    ]


async def _check_credential_quota(
    api_key: str,
    *,
    remains_url: str = CN_REMAINS_URL,
) -> list[MiniMaxQuotaObservation]:
    """Return provider quota observations or raise when evidence is unclear."""

    async with httpx.AsyncClient(timeout=POLL_TIMEOUT) as client:
        response = await client.get(
            remains_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if response.status_code != 200:
        raise MiniMaxQuotaPollIndeterminate(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MiniMaxQuotaPollIndeterminate("invalid JSON") from exc
    return _parse_quota_observations(payload)


async def poll_minimax_quota() -> int:
    """Poll platform MiniMax credentials and update only scoped circuits.

    Returns the number of credentials with at least one explicitly depleted
    resource in this cycle.
    """

    checked = 0
    depleted_credentials = 0
    async with async_session() as db:
        result = await db.execute(
            select(LLMCredential).where(
                LLMCredential.provider == "minimax",
                LLMCredential.tenant_id.is_(None),
                LLMCredential.enabled == True,  # noqa: E712
                LLMCredential.status.in_(_ACTIVE_CREDENTIAL_STATUSES),
            )
        )
        credentials = result.scalars().all()

    for credential in credentials:
        api_key = get_credential_api_key(credential)
        if not api_key:
            continue
        try:
            observations = await _check_credential_quota(
                api_key,
                remains_url=_remains_url(credential.base_url),
            )
            checked += 1
            credential_depleted = False
            for observation in observations:
                model_kwargs = {"model": observation.model} if observation.model else {}
                if observation.depleted:
                    await mark_credential_modality_quota_exceeded(
                        credential.id,
                        observation.modality,
                        error_code="2056",
                        **model_kwargs,
                    )
                    credential_depleted = True
                else:
                    await clear_credential_modality_quota(
                        credential.id,
                        observation.modality,
                        **model_kwargs,
                    )
            if credential_depleted:
                depleted_credentials += 1
                logger.warning(
                    "[minimax_quota] credential {} has a depleted Token Plan resource",
                    credential.id,
                )
        except MiniMaxQuotaPollIndeterminate as exc:
            logger.debug(
                "[minimax_quota] quota evidence unavailable credential={} reason={}",
                credential.id,
                str(exc),
            )
        except Exception as exc:
            logger.debug(
                "[minimax_quota] poll failed credential={} error_type={}",
                credential.id,
                type(exc).__name__,
            )

    if checked:
        logger.info(
            "[minimax_quota] polled {} credential(s), {} with depleted resources",
            checked,
            depleted_credentials,
        )
    return depleted_credentials
