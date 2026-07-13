"""MiniMax Token Plan quota polling.

Periodically queries MiniMax's /v1/token_plan/remains endpoint for each MiniMax
subscription credential and marks credentials quota_exceeded when the 5-hour or
weekly window is depleted. This is an active supplement to the passive Redis
window_5h_limit counter in the load balancer.

The remains API returns per-model-class windows:
  model_remains: [{model_name: "general" | "video", current_interval_status,
                   current_interval_remaining_percent, current_weekly_status,
                   current_weekly_remaining_percent, ...}]
status=1 means active/available; remaining_percent=0 means depleted.
"""

from __future__ import annotations

import httpx
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.llm import LLMCredential
from app.services.llm.load_balancer import mark_credential_quota_exceeded
from app.services.llm.utils import get_credential_api_key

# MiniMax remains API lives on the www host (not api.minimaxi.com).
REMAINS_URL = "https://www.minimaxi.com/v1/token_plan/remains"
POLL_TIMEOUT = 15.0


async def poll_minimax_quota() -> int:
    """Poll all enabled MiniMax credentials and mark depleted ones.

    Returns the number of credentials marked quota_exceeded this cycle.
    """
    checked = 0
    depleted = 0
    async with async_session() as db:
        result = await db.execute(
            select(LLMCredential).where(
                LLMCredential.provider == "minimax",
                LLMCredential.enabled == True,  # noqa: E712
                LLMCredential.status == "healthy",
            )
        )
        creds = result.scalars().all()

    for cred in creds:
        api_key = get_credential_api_key(cred)
        if not api_key:
            continue
        try:
            is_depleted = await _check_credential_depleted(api_key)
            checked += 1
            if is_depleted:
                await mark_credential_quota_exceeded(cred.id)
                depleted += 1
                logger.warning(
                    f"[minimax_quota] credential {cred.id} ({cred.label}) "
                    "Token Plan window depleted -> quota_exceeded"
                )
        except Exception as e:
            logger.debug(f"[minimax_quota] poll failed for {cred.id}: {e}")

    if checked:
        logger.info(f"[minimax_quota] polled {checked} credential(s), {depleted} depleted")
    return depleted


async def _check_credential_depleted(api_key: str) -> bool:
    """Return True if the 5h interval OR weekly window is depleted for text (general)."""
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT) as client:
        resp = await client.get(REMAINS_URL, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            return False  # can't determine; don't mark based on HTTP error
        data = resp.json()

    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code", 0) != 0:
        # 1004/2049 = invalid key; treat as depleted so it gets pulled (reset_daily restores)
        code = base_resp.get("status_code")
        if code in (1004, 2049):
            return True
        return False

    model_remains = data.get("model_remains", [])
    if not model_remains:
        return False

    # "general" covers text models; "video" covers video. We care about text here
    # since that's the primary use. If the general window is depleted, the key
    # can't serve text calls.
    general = next((m for m in model_remains if m.get("model_name") == "general"), None)
    if not general:
        return False

    interval_pct = int(general.get("current_interval_remaining_percent", 100))
    interval_status = int(general.get("current_interval_status", 1))
    weekly_pct = int(general.get("current_weekly_remaining_percent", 100))
    weekly_status = int(general.get("current_weekly_status", 1))

    # Depleted if either window is at 0% or status indicates inactive (0)
    if interval_pct <= 0 or interval_status == 0:
        return True
    if weekly_pct <= 0 or weekly_status == 0:
        return True
    return False
