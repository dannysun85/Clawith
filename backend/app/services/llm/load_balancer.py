"""LLM credential pool load balancer (账号池, provider-scoped).

Picks an API-key account from the pool for a (provider, modality) call:
filter capabilities ⊇ modality + enabled + healthy + under daily_quota +
within client-side RPM/TPM/5h-window rate limits, then priority-grouped
weighted pick.

One account serves multiple models/modalities of a provider (e.g. a MiniMax
code-plan account can call text/voice/image/video).

Rate limiting uses Redis sorted-set sliding windows (same pattern as the
webhook endpoint): ZADD + ZREMRANGEBYSCORE + ZCARD with a TTL.
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from loguru import logger
from sqlalchemy import or_, select, text

from app.core.events import get_redis
from app.database import async_session
from app.models.llm import LLMCredential
from app.services.modalities import canonicalize_modalities, modality_match_values


# Redis key prefixes
_KEY_PREFIX = "cred:rate:"
_KEY_RPM = _KEY_PREFIX + "rpm:{cred_id}"        # sorted set of request timestamps (score=ts, member=nonce)
_KEY_TPM = _KEY_PREFIX + "tpm:{cred_id}"        # sorted set of token counts (score=ts, member=f"ts:tokens")
_KEY_PROVIDER_COOLDOWN = _KEY_PREFIX + "cooldown:{cred_id}"
_RPM_WINDOW = 60                                # 60 second window for RPM
_TPM_WINDOW = 60                                # 60 second window for TPM
PROVIDER_RATE_COOLDOWN_SECONDS = 30
_PROVIDER_RATE_CODES = frozenset({"1002", "1041", "2045", "2062", "rate_limit"})
PLAN_QUOTA_RESOURCE = "plan"


class CredentialUnavailableReason(str, Enum):
    NOT_CONFIGURED = "not_configured"
    ALL_UNHEALTHY = "all_unhealthy"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_SATURATED = "rate_saturated"
    CAPABILITY_MISMATCH = "capability_mismatch"


class NoCredentialAvailable(Exception):
    """No healthy, non-exhausted, modality-capable credential in the pool."""

    def __init__(
        self,
        provider: str,
        modality: str | None,
        reason_code: CredentialUnavailableReason = CredentialUnavailableReason.NOT_CONFIGURED,
        reason: str = "",
    ):
        self.provider = provider
        self.modality = modality
        self.reason_code = reason_code
        self.reason = reason
        msg = (
            f"No credential available for provider={provider} modality={modality} "
            f"reason_code={reason_code.value}"
        )
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


def no_credential_user_message(error: NoCredentialAvailable) -> str:
    """Return a stable user-facing explanation without exposing pool details."""

    messages = {
        CredentialUnavailableReason.NOT_CONFIGURED: "平台尚未配置该供应商账号，请联系管理员。",
        CredentialUnavailableReason.ALL_UNHEALTHY: "平台供应商账号正在维护，请稍后重试或联系管理员验证账号。",
        CredentialUnavailableReason.QUOTA_EXHAUSTED: "平台供应商额度暂时不足，请联系管理员补充额度。",
        CredentialUnavailableReason.RATE_SATURATED: "平台供应商当前请求繁忙，请稍后重试。",
        CredentialUnavailableReason.CAPABILITY_MISMATCH: "平台尚未配置当前功能所需的模型能力，请联系管理员。",
    }
    return messages[error.reason_code]


def _credential_supports_modality(credential: LLMCredential, modality: str | None) -> bool:
    if not modality or credential.capabilities is None:
        return True
    if not credential.capabilities:
        return False
    supported = {str(value).strip().lower() for value in credential.capabilities if str(value).strip()}
    return bool(supported.intersection(modality_match_values(modality)))


def _canonical_modality(modality: str) -> str:
    canonical = canonicalize_modalities([modality])
    return canonical[0] if canonical else str(modality).strip().lower()


def _normalize_quota_resource(resource: str) -> str:
    """Normalize a quota resource while preserving an optional model suffix."""

    raw = str(resource or "").strip().lower()
    modality, separator, model = raw.partition(":")
    normalized_modality = _canonical_modality(modality)
    if not separator or not model.strip() or normalized_modality == "text":
        return normalized_modality
    return f"{normalized_modality}:{model.strip()}"


def credential_quota_resource_key(modality: str, model: str | None = None) -> str:
    """Return the provider quota key for a modality and optional concrete model.

    Current MiniMax Token Plan usage is shared across modalities and is stored
    under ``plan``. A concrete media-model circuit remains supported for
    provider responses such as an Ultra tier's additional daily video cap and
    for backward compatibility with legacy plans.
    """

    normalized = _canonical_modality(modality)
    if normalized in {"text", PLAN_QUOTA_RESOURCE} or not str(model or "").strip():
        return normalized
    return _normalize_quota_resource(f"{normalized}:{model}")


def credential_blocked_quota_resources(credential: LLMCredential) -> set[str]:
    """Return provider-quota-blocked resources from a backward-safe JSON map."""

    raw = getattr(credential, "modality_status", None)
    if not isinstance(raw, dict):
        return set()
    blocked: set[str] = set()
    for resource, value in raw.items():
        status = value.get("status") if isinstance(value, dict) else value
        if str(status or "").strip().lower() == "quota_exceeded":
            blocked.add(_normalize_quota_resource(str(resource)))
    return blocked


def credential_blocked_modalities(credential: LLMCredential) -> set[str]:
    """Return legacy modality-wide quota circuits only.

    Model-specific circuits must not make an entire media modality unavailable:
    another configured model may still have quota.
    """

    return {
        resource
        for resource in credential_blocked_quota_resources(credential)
        if ":" not in resource
    }


def credential_modality_is_blocked(credential: LLMCredential, modality: str | None) -> bool:
    blocked = credential_blocked_modalities(credential)
    if PLAN_QUOTA_RESOURCE in blocked:
        return True
    if not modality:
        return False
    requested = {_canonical_modality(value) for value in modality_match_values(modality)}
    return bool(requested.intersection(blocked))


def credential_quota_is_blocked(
    credential: LLMCredential,
    modality: str | None,
    model: str | None = None,
) -> bool:
    """Return whether either the legacy modality or exact model circuit is open."""

    blocked = credential_blocked_quota_resources(credential)
    if PLAN_QUOTA_RESOURCE in blocked:
        return True
    if not modality:
        return False
    requested_modalities = {
        _canonical_modality(value) for value in modality_match_values(modality)
    }
    if requested_modalities.intersection(blocked):
        return True
    if model:
        return credential_quota_resource_key(modality, model) in blocked
    return False


def _diagnose_base_filter_failure(
    credentials: list[LLMCredential],
    modality: str | None,
    *,
    quota_modality: str | None = None,
    quota_model: str | None = None,
) -> CredentialUnavailableReason:
    if not credentials:
        return CredentialUnavailableReason.NOT_CONFIGURED
    capable = [credential for credential in credentials if _credential_supports_modality(credential, modality)]
    if not capable:
        return CredentialUnavailableReason.CAPABILITY_MISMATCH
    enabled = [credential for credential in capable if credential.enabled]
    if not enabled:
        return CredentialUnavailableReason.ALL_UNHEALTHY
    quota_available = [
        credential
        for credential in enabled
        if credential.status != "quota_exceeded"
        and (
            credential.daily_quota is None
            or credential.used_today < credential.daily_quota
        )
    ]
    if not quota_available:
        return CredentialUnavailableReason.QUOTA_EXHAUSTED
    modality_available = [
        credential
        for credential in quota_available
        if not credential_quota_is_blocked(
            credential,
            quota_modality if quota_modality is not None else modality,
            quota_model,
        )
    ]
    if not modality_available:
        return CredentialUnavailableReason.QUOTA_EXHAUSTED
    return CredentialUnavailableReason.ALL_UNHEALTHY


def _cred_rpm_key(cred_id: uuid.UUID) -> str:
    return _KEY_RPM.format(cred_id=cred_id)


def _cred_tpm_key(cred_id: uuid.UUID) -> str:
    return _KEY_TPM.format(cred_id=cred_id)


def _cred_provider_cooldown_key(cred_id: uuid.UUID) -> str:
    return _KEY_PROVIDER_COOLDOWN.format(cred_id=cred_id)


async def _get_redis_or_none():
    """Return Redis client if available, else None (graceful fallback to no rate limiting)."""
    try:
        redis = await get_redis()
        # Verify connection is usable; if Redis is down treat as if no rate limiter.
        await redis.ping()
        return redis
    except Exception as e:
        logger.warning(f"[load_balancer] Redis unavailable, rate limiting disabled: {e}")
        return None


async def _check_rate_window(redis, key: str, window: int, limit: int | None) -> tuple[bool, int]:
    """Return (within_limit, current_count). If limit is None, no cap."""
    if limit is None or redis is None:
        return True, 0
    now = time.time()
    # Trim expired entries and count current
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zcard(key)
        _, count = await pipe.execute()
    count = int(count)
    return count < limit, count


async def _provider_cooldown_active(redis, credential_id: uuid.UUID) -> bool:
    if redis is None:
        return False
    try:
        return bool(await redis.exists(_cred_provider_cooldown_key(credential_id)))
    except Exception as exc:
        logger.warning(
            "[load_balancer] provider cooldown read unavailable error_type={}",
            type(exc).__name__,
        )
        return False


async def mark_credential_rate_saturated(
    credential_id: uuid.UUID,
    *,
    cooldown_seconds: int = PROVIDER_RATE_COOLDOWN_SECONDS,
    error_code: str = "rate_limit",
) -> bool:
    """Temporarily remove one provider account from immediate retry rotation.

    MiniMax 2062 is a deterministic high-traffic rejection from a Token Plan,
    not proof that the key is invalid or that the rolling plan allowance is
    exhausted. A short Redis-backed cooldown lets a different independent
    account serve failover without poisoning the shared credential row.
    """

    bounded_seconds = max(1, min(int(cooldown_seconds), 300))
    redis = await _get_redis_or_none()
    if redis is None:
        return False
    candidate_code = str(error_code or "")
    stable_error_code = (
        candidate_code if candidate_code in _PROVIDER_RATE_CODES else "rate_limit"
    )
    try:
        await redis.set(
            _cred_provider_cooldown_key(credential_id),
            stable_error_code,
            ex=bounded_seconds,
        )
    except Exception as exc:
        logger.warning(
            "[load_balancer] provider cooldown write unavailable error_type={}",
            type(exc).__name__,
        )
        return False
    logger.warning(
        "[load_balancer] credential provider cooldown opened seconds={}",
        bounded_seconds,
    )
    return True


async def _record_request(redis, cred_id: uuid.UUID, tokens_used: int = 0) -> None:
    """Record a request in the sliding windows after a successful dispatch."""
    if redis is None:
        return
    now = time.time()
    nonce = hashlib.sha1(f"{cred_id}:{now}:{time.time_ns()}".encode()).hexdigest()[:12]
    async with redis.pipeline(transaction=True) as pipe:
        # RPM
        pipe.zadd(_cred_rpm_key(cred_id), {nonce: now})
        pipe.expire(_cred_rpm_key(cred_id), _RPM_WINDOW * 2)
        # TPM (tokens as score prefix so we can sum later; member encodes ts:tokens:nonce)
        if tokens_used > 0:
            member = f"{now}:{tokens_used}:{nonce}"
            pipe.zadd(_cred_tpm_key(cred_id), {member: now})
            pipe.expire(_cred_tpm_key(cred_id), _TPM_WINDOW * 2)
        await pipe.execute()


async def _get_current_tpm(redis, cred_id: uuid.UUID) -> int:
    """Return total tokens in the TPM window."""
    if redis is None:
        return 0
    key = _cred_tpm_key(cred_id)
    now = time.time()
    # Remove expired entries first
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, now - _TPM_WINDOW)
        pipe.zrange(key, 0, -1)
        _, members = await pipe.execute()
    total = 0
    for m in members:
        try:
            # member format: "ts:tokens:nonce"
            parts = m.split(":")
            if len(parts) >= 2:
                total += int(parts[1])
        except (ValueError, IndexError):
            continue
    return total


async def pick_credential(
    provider: str,
    modality: str | None = None,
    estimated_tokens: int = 0,
    *,
    quota_modality: str | None = None,
    quota_model: str | None = None,
) -> LLMCredential:
    """Pick a credential from the pool for (provider, modality).

    Filters: provider + enabled + healthy + (daily_quota ok) + (RPM not saturated)
          + (TPM not saturated for estimated tokens) + (capabilities ⊇ modality)
          + (provider quota resource available).
    Priority group (top) → weighted pick within group.

    Args:
        provider: Provider name (e.g. "minimax").
        modality: Modality tag used only for capability filtering.
        estimated_tokens: Pre-call token estimate for TPM pre-check (prompt+max_tokens
            estimate). 0 = skip TPM pre-check (only enforced post-call for RPM).
        quota_modality: Provider allowance consumed by this call. Defaults to
            ``modality``. Multimodal understanding should pass ``text`` while
            retaining image/video as the capability modality.
        quota_model: Optional concrete non-text provider model. A depleted media
            model then does not poison other models in the same modality.
    """
    redis = await _get_redis_or_none()
    effective_quota_modality = quota_modality if quota_modality is not None else modality

    async with async_session() as db:
        conditions = [
            LLMCredential.provider == provider,
            # Clawith currently uses one centrally funded platform pool. A
            # future tenant-owned credential must never be selected for a
            # different tenant merely because it is healthy.
            LLMCredential.tenant_id.is_(None),
            LLMCredential.enabled == True,  # noqa: E712
            LLMCredential.status == "healthy",
            or_(
                LLMCredential.daily_quota.is_(None),
                LLMCredential.used_today < LLMCredential.daily_quota,
            ),
        ]
        if modality:
            capability_values = modality_match_values(modality)
            capability_conditions = [LLMCredential.capabilities.is_(None)]
            for idx, value in enumerate(capability_values):
                capability_conditions.append(
                    text(
                        f"cast(llm_credentials.capabilities as jsonb) "
                        f"@> cast(:cap_{idx} as jsonb)"
                    ).bindparams(**{f"cap_{idx}": json.dumps([value])})
                )
            conditions.append(
                or_(*capability_conditions)
            )

        # Order by priority DESC, weight DESC for weighted pick
        query = select(LLMCredential).where(*conditions).order_by(
            LLMCredential.priority.desc(), LLMCredential.weight.desc()
        )

        result = await db.execute(query)
        all_creds = [
            credential
            for credential in result.scalars().all()
            if not credential_quota_is_blocked(
                credential,
                effective_quota_modality,
                quota_model,
            )
        ]
        if not all_creds:
            diagnostic_result = await db.execute(
                select(LLMCredential).where(
                    LLMCredential.provider == provider,
                    LLMCredential.tenant_id.is_(None),
                )
            )
            pool = list(diagnostic_result.scalars().all())
            reason_code = _diagnose_base_filter_failure(
                pool,
                modality,
                quota_modality=effective_quota_modality,
                quota_model=quota_model,
            )
            logger.warning(
                "[load_balancer] no credential provider={} modality={} quota_resource={} reason_code={}",
                provider,
                modality,
                credential_quota_resource_key(effective_quota_modality, quota_model)
                if effective_quota_modality
                else None,
                reason_code.value,
            )
            raise NoCredentialAvailable(
                provider,
                modality,
                reason_code,
                "no credentials match base filters",
            )

        # Check rate limits and filter within the top priority group
        top_priority = all_creds[0].priority
        top_group = [c for c in all_creds if c.priority == top_priority]

        # Filter by rate limits
        eligible = []
        skip_reasons: dict[str, str] = {}
        for c in top_group:
            if await _provider_cooldown_active(redis, c.id):
                skip_reasons[str(c.id)] = "provider_cooldown"
                continue
            rpm_limit = getattr(c, 'rpm_limit', None)
            tpm_limit = getattr(c, 'tpm_limit', None)
            ok, rpm_count = await _check_rate_window(redis, _cred_rpm_key(c.id), _RPM_WINDOW, rpm_limit)
            if not ok:
                skip_reasons[str(c.id)] = f"rpm={rpm_count}>={rpm_limit}"
                continue

            # TPM pre-check — if we can estimate token usage for this call,
            # verify the current TPM + estimate won't exceed
            if estimated_tokens > 0 and tpm_limit is not None:
                cur_tpm = await _get_current_tpm(redis, c.id)
                if cur_tpm + estimated_tokens > tpm_limit:
                    skip_reasons[str(c.id)] = f"tpm={cur_tpm}+{estimated_tokens}>={tpm_limit}"
                    continue

            eligible.append(c)

        # If no eligible in top priority, try lower priority groups
        if not eligible:
            lower_groups = [c for c in all_creds if c.priority < top_priority]
            # Group by priority descending
            priorities = sorted({c.priority for c in lower_groups}, reverse=True)
            for p in priorities:
                grp = [c for c in lower_groups if c.priority == p]
                for c in grp:
                    if await _provider_cooldown_active(redis, c.id):
                        skip_reasons[str(c.id)] = "provider_cooldown"
                        continue
                    rpm_limit = getattr(c, 'rpm_limit', None)
                    tpm_limit = getattr(c, 'tpm_limit', None)
                    ok, rpm_count = await _check_rate_window(redis, _cred_rpm_key(c.id), _RPM_WINDOW, rpm_limit)
                    if not ok:
                        skip_reasons[str(c.id)] = f"rpm={rpm_count}>={rpm_limit}"
                        continue
                    if estimated_tokens > 0 and tpm_limit is not None:
                        cur_tpm = await _get_current_tpm(redis, c.id)
                        if cur_tpm + estimated_tokens > tpm_limit:
                            skip_reasons[str(c.id)] = f"tpm={cur_tpm}+{estimated_tokens}>={tpm_limit}"
                            continue
                    eligible.append(c)
                if eligible:
                    break

        if not eligible:
            reason = f"all rate-saturated; skipped={skip_reasons}" if skip_reasons else "pool exhausted"
            raise NoCredentialAvailable(
                provider,
                modality,
                CredentialUnavailableReason.RATE_SATURATED,
                reason,
            )

        chosen = _weighted_pick(eligible)
        chosen.last_used_at = datetime.now(timezone.utc)
        await db.commit()

        # Atomically pre-claim an RPM slot to close the check-then-act race:
        # without this, N concurrent requests all see rpm_current < limit and
        # all pass. ZADD+ZCARD in one pipeline is atomic w.r.t. other clients.
        chosen_rpm_limit = getattr(chosen, 'rpm_limit', None)
        if redis is not None and chosen_rpm_limit is not None:
            rpm_key = _cred_rpm_key(chosen.id)
            now = time.time()
            nonce = hashlib.sha1(f"{chosen.id}:{now}:{time.time_ns()}".encode()).hexdigest()[:12]
            async with redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(rpm_key, 0, now - _RPM_WINDOW)
                pipe.zadd(rpm_key, {nonce: now})
                pipe.zcard(rpm_key)
                pipe.expire(rpm_key, _RPM_WINDOW * 2)
                _, _, count, _ = await pipe.execute()
            if int(count) > chosen_rpm_limit:
                # Over limit after atomic claim - rollback this claim and reject.
                await redis.zrem(rpm_key, nonce)
                raise NoCredentialAvailable(
                    provider,
                    modality,
                    CredentialUnavailableReason.RATE_SATURATED,
                    f"rpm atomically saturated on {chosen.id} ({count}/{chosen_rpm_limit})",
                )
            # Stash the nonce so record_credential_call won't double-count RPM.
            chosen._rpm_claim_nonce = nonce  # type: ignore[attr-defined]

        return chosen


def _weighted_pick(creds: list[LLMCredential]) -> LLMCredential:
    """Weighted pick proportional to weight. Falls back to first if all zero."""
    total = sum(c.weight for c in creds)
    if total <= 0:
        return creds[0]
    seed = hashlib.md5(str(datetime.now(timezone.utc).timestamp()).encode()).hexdigest()
    r = int(seed, 16) % total
    cum = 0
    for c in creds:
        cum += c.weight
        if r < cum:
            return c
    return creds[-1]


async def record_credential_call(
    credential_id: uuid.UUID,
    tokens_used: int = 0,
    weight_daily: int = 1,
    weight: int | None = None,  # backward-compatible alias for weight_daily
) -> None:
    """Record a successful call: bump used_today (daily quota), RPM/TPM windows,
    Clear the consecutive-failure counter and mark quota_exceeded if at the
    optional local daily cap.

    Call after a successful LLM invocation to update both DB and Redis counters.
    """
    w = weight if weight is not None else weight_daily
    # DB: daily quota (legacy)
    async with async_session() as db:
        cred = await db.get(LLMCredential, credential_id)
        if not cred:
            return
        was_healthy = cred.status == "healthy"
        cred.used_today += w
        # ``mark_credential_degraded`` is a consecutive-failure circuit
        # breaker.  A successful provider call proves the credential recovered,
        # so old transient errors must not accumulate forever and eventually
        # remove an otherwise healthy shared account from every modality.
        cred.error_count = 0
        # Do not clear provider quota circuits from an ordinary success. A
        # request selected just before another request observes exhaustion may
        # complete afterwards and would otherwise race the newer circuit open.
        # Only the authoritative quota poller closes provider-evidence circuits.
        if was_healthy and cred.daily_quota and cred.used_today >= cred.daily_quota:
            cred.status = "quota_exceeded"
        await db.commit()

    # Redis: TPM + 5h windows only. RPM is pre-claimed atomically in
    # pick_credential (ZADD+ZCARD), so we must NOT re-add it here or we'd
    # double-count every call.
    redis = await _get_redis_or_none()
    if redis is not None and tokens_used > 0:
        now = time.time()
        nonce = hashlib.sha1(f"{credential_id}:{now}:{time.time_ns()}".encode()).hexdigest()[:12]
        async with redis.pipeline(transaction=True) as pipe:
            # TPM (token-weighted, score=timestamp)
            tpm_key = _cred_tpm_key(credential_id)
            pipe.zremrangebyscore(tpm_key, 0, now - _TPM_WINDOW)
            pipe.zadd(tpm_key, {f"{now}:{tokens_used}:{nonce}": now})
            pipe.expire(tpm_key, _TPM_WINDOW * 2)
            await pipe.execute()


# Backward-compatible alias: callers using increment_credential_usage still work
increment_credential_usage = record_credential_call


async def mark_credential_degraded(credential_id: uuid.UUID, threshold: int = 5, immediate: bool = False) -> None:
    """Increment error count; mark degraded past threshold (after a failed call).

    Args:
        credential_id: Credential to mark.
        threshold: Error count before marking degraded (used when immediate=False).
        immediate: If True, mark degraded immediately regardless of threshold (for
            fatal errors like invalid API keys where retrying is pointless).
    """
    async with async_session() as db:
        cred = await db.get(LLMCredential, credential_id)
        if not cred:
            return
        cred.error_count += 1
        if immediate or cred.error_count >= threshold:
            cred.status = "degraded"
            reason = "immediate" if immediate else f"errors={cred.error_count}"
            logger.warning(f"[load_balancer] credential {credential_id} marked degraded ({reason})")
        await db.commit()


async def mark_credential_quota_exceeded(credential_id: uuid.UUID) -> None:
    """Immediately mark credential as quota_exceeded (billing/plan exhaustion).

    Use this for non-retryable billing errors (insufficient balance, plan quota
    exhausted) where the credential will remain unusable until external action
    followed by an explicit verification succeeds.
    """
    async with async_session() as db:
        cred = await db.get(LLMCredential, credential_id)
        if not cred:
            return
        cred.status = "quota_exceeded"
        cred.error_count += 1
        logger.warning(f"[load_balancer] credential {credential_id} marked quota_exceeded")
        await db.commit()


async def mark_credential_modality_quota_exceeded(
    credential_id: uuid.UUID,
    modality: str,
    *,
    error_code: str = "2056",
    model: str | None = None,
) -> None:
    """Open one provider allowance circuit without poisoning the shared key."""

    normalized = _canonical_modality(modality)
    resource = credential_quota_resource_key(normalized, model)
    async with async_session() as db:
        # ``modality_status`` is one shared JSON document.  Serialize every
        # read/modify/write operation on the credential row so two concurrent
        # provider observations cannot silently overwrite each other.
        cred = await db.get(LLMCredential, credential_id, with_for_update=True)
        if not cred:
            return
        statuses = dict(getattr(cred, "modality_status", None) or {})
        existing = statuses.get(resource)
        if (
            isinstance(existing, dict)
            and existing.get("status") == "quota_exceeded"
            and str(existing.get("error_code") or "") == str(error_code)
        ):
            return
        status_entry = {
            "status": "quota_exceeded",
            "error_code": str(error_code),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "reset_scope": "provider_evidence",
        }
        if model:
            status_entry["model"] = str(model).strip()
        statuses[resource] = status_entry
        cred.modality_status = statuses
        cred.error_count += 1
        logger.warning(
            "[load_balancer] credential {} quota_resource={} marked quota_exceeded",
            credential_id,
            resource,
        )
        await db.commit()


async def clear_credential_modality_quota(
    credential_id: uuid.UUID,
    modality: str,
    *,
    model: str | None = None,
) -> bool:
    """Close a scoped quota circuit after explicit provider recovery evidence."""

    normalized = _canonical_modality(modality)
    resource = credential_quota_resource_key(normalized, model)
    async with async_session() as db:
        cred = await db.get(LLMCredential, credential_id, with_for_update=True)
        if not cred:
            return False
        statuses = dict(getattr(cred, "modality_status", None) or {})
        removed = False
        # Exact success/provider evidence retires the exact circuit. Also
        # remove the old modality-wide circuit so deployments upgraded from
        # the legacy schema are not permanently over-blocked.
        removal_keys = {resource, normalized}
        for key in list(statuses):
            if _normalize_quota_resource(str(key)) in removal_keys:
                statuses.pop(key, None)
                removed = True
        if removed:
            cred.modality_status = statuses
            await db.commit()
        return removed


async def reset_daily_usage() -> int:
    """Reset the local daily counter without re-admitting broken credentials.

    Every credential's ``used_today`` belongs to a calendar day, including a
    healthy credential that did not reach its cap. Only a credential proven to
    have hit the configured local ``daily_quota`` is restored automatically.
    Authentication failures (``degraded``) and provider-side balance/plan
    exhaustion require explicit verification after the external condition is
    fixed; a midnight job must never put them back into the global pool.
    """
    reset_count = 0
    async with async_session() as db:
        result = await db.execute(
            select(LLMCredential).where(
                or_(
                    LLMCredential.reset_at.is_(None),
                    text("reset_at < CURRENT_DATE"),
                ),
            )
        )
        creds = result.scalars().all()
        for c in creds:
            hit_local_daily_cap = bool(
                c.status == "quota_exceeded"
                and c.daily_quota is not None
                and c.used_today >= c.daily_quota
            )
            c.used_today = 0
            c.reset_at = datetime.now(timezone.utc)
            if hit_local_daily_cap:
                c.error_count = 0
                c.status = "healthy"
            # Provider quota circuits are cleared only by an observed success
            # or the remains endpoint. Local midnight may not match the
            # provider's reset boundary and must not re-admit a depleted model.
            reset_count += 1
        if creds:
            await db.commit()

    return reset_count


async def get_credential_health() -> list[dict[str, Any]]:
    """Return current rate-limit counters for health endpoint (used by /credentials/health)."""
    redis = await _get_redis_or_none()
    out: list[dict[str, Any]] = []
    async with async_session() as db:
        result = await db.execute(select(LLMCredential).order_by(LLMCredential.provider, LLMCredential.priority.desc()))
        creds = result.scalars().all()
        for c in creds:
            rpm_current = 0
            tpm_current = 0
            if redis is not None:
                now = time.time()
                # Trim first, then count
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.zremrangebyscore(_cred_rpm_key(c.id), 0, now - _RPM_WINDOW)
                    pipe.zcard(_cred_rpm_key(c.id))
                    _, rpm_current = await pipe.execute()
                tpm_current = await _get_current_tpm(redis, c.id)
            out.append({
                "id": c.id,
                "provider": c.provider,
                "label": c.label,
                "status": c.status,
                "enabled": c.enabled,
                "modality_status": dict(getattr(c, "modality_status", None) or {}),
                "used_today": c.used_today,
                "daily_quota": c.daily_quota,
                "error_count": c.error_count,
                "rpm_limit": c.rpm_limit,
                "tpm_limit": c.tpm_limit,
                "window_5h_limit": c.window_5h_limit,
                "rpm_current": int(rpm_current),
                "tpm_current": int(tpm_current),
                "last_used_at": c.last_used_at,
            })
    return out
