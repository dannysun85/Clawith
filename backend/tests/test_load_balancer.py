"""Unit tests for load_balancer (账号池 pick_credential + usage tracking).

Mock-based (no DB). Verifies priority grouping, weighted pick, NoCredentialAvailable,
increment→quota_exceeded, mark_degraded, reset_daily_usage.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm import load_balancer
from app.services.llm.load_balancer import (
    NoCredentialAvailable,
    increment_credential_usage,
    mark_credential_degraded,
    pick_credential,
    reset_daily_usage,
)


def _cred(
    priority=0,
    weight=1,
    status="healthy",
    daily_quota=None,
    used_today=0,
    error_count=0,
    capabilities=None,
    modality_status=None,
):
    credential_id = uuid.uuid4()
    verified_at = load_balancer.datetime.now(load_balancer.timezone.utc)
    return SimpleNamespace(
        id=credential_id,
        provider="minimax",
        label="c",
        api_key_encrypted="enc",
        base_url=None,
        capabilities=capabilities,
        modality_status=modality_status or {},
        daily_quota=daily_quota,
        used_today=used_today,
        status=status,
        last_verification_at=verified_at,
        verification_receipt={
            "receipt_ref": f"credential-auth:{uuid.uuid4()}",
            "kind": "credential_auth_probe",
            "scope": "account_authentication",
            "evidence_level": "account_verified",
            "credential_id": str(credential_id),
            "provider": "minimax",
            "checked_at": verified_at.isoformat(),
            "ok": True,
        },
        error_count=error_count,
        weight=weight,
        priority=priority,
        last_used_at=None,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_media_pick_requires_current_explicit_account_verification():
    credential = _cred(capabilities=["video"])
    credential.last_verification_at = None
    credential.verification_receipt = None
    sess, _ = _patch_session(execute_result=[credential])

    with sess, pytest.raises(NoCredentialAvailable) as exc:
        await pick_credential("minimax", "video")

    assert exc.value.reason_code == load_balancer.CredentialUnavailableReason.ALL_UNHEALTHY


def _patch_session(execute_result=None, get_value=None):
    """Fake async_session: db.execute → execute_result (a list-like via scalars().all()), db.get → get_value."""
    fake_db = MagicMock()
    fake_result = MagicMock()
    fake_result.scalars.return_value.all.return_value = execute_result or []
    fake_db.execute = AsyncMock(return_value=fake_result)
    fake_db.get = AsyncMock(return_value=get_value)
    fake_db.commit = AsyncMock()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    return patch.object(load_balancer, "async_session", return_value=fake_session), fake_db


@pytest.mark.asyncio
async def test_pick_returns_top_priority_cred():
    """Among creds of different priority, only the top-priority group is considered."""
    low = _cred(priority=0, weight=1)
    high = _cred(priority=10, weight=1)
    sess, _ = _patch_session(execute_result=[high, low])  # ordered by priority desc
    with sess:
        chosen = await pick_credential("minimax", "text")
    assert chosen.priority == 10


@pytest.mark.asyncio
async def test_pick_skips_provider_cooldown_and_uses_independent_credential(monkeypatch):
    blocked = _cred(priority=10)
    fallback = _cred(priority=0)
    redis = AsyncMock()
    redis.exists = AsyncMock(side_effect=lambda key: str(blocked.id) in key)
    monkeypatch.setattr(
        load_balancer,
        "_get_redis_or_none",
        AsyncMock(return_value=redis),
    )
    sess, _ = _patch_session(execute_result=[blocked, fallback])

    with sess:
        chosen = await pick_credential("minimax", "text")

    assert chosen.id == fallback.id


@pytest.mark.asyncio
async def test_provider_cooldown_read_failure_does_not_disable_credential(monkeypatch):
    credential = _cred(priority=10)
    redis = AsyncMock()
    redis.exists = AsyncMock(side_effect=ConnectionError("sensitive redis endpoint"))
    monkeypatch.setattr(
        load_balancer,
        "_get_redis_or_none",
        AsyncMock(return_value=redis),
    )
    sess, _ = _patch_session(execute_result=[credential])

    with sess:
        chosen = await pick_credential("minimax", "text")

    assert chosen.id == credential.id


@pytest.mark.asyncio
async def test_mark_provider_rate_saturated_sets_bounded_redis_cooldown(monkeypatch):
    credential = _cred()
    redis = AsyncMock()
    monkeypatch.setattr(
        load_balancer,
        "_get_redis_or_none",
        AsyncMock(return_value=redis),
    )

    recorded = await load_balancer.mark_credential_rate_saturated(
        credential.id,
        cooldown_seconds=30,
        error_code="2062",
    )

    assert recorded is True
    redis.set.assert_awaited_once_with(
        load_balancer._cred_provider_cooldown_key(credential.id),
        "2062",
        ex=30,
    )


@pytest.mark.asyncio
async def test_mark_provider_rate_saturated_does_not_persist_unknown_error_text(monkeypatch):
    credential = _cred()
    redis = AsyncMock()
    monkeypatch.setattr(
        load_balancer,
        "_get_redis_or_none",
        AsyncMock(return_value=redis),
    )

    await load_balancer.mark_credential_rate_saturated(
        credential.id,
        error_code="provider payload must not enter diagnostics",
    )

    redis.set.assert_awaited_once_with(
        load_balancer._cred_provider_cooldown_key(credential.id),
        "rate_limit",
        ex=load_balancer.PROVIDER_RATE_COOLDOWN_SECONDS,
    )


@pytest.mark.asyncio
async def test_mark_provider_rate_saturated_write_failure_uses_local_backoff(monkeypatch):
    credential = _cred()
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=ConnectionError("sensitive redis endpoint"))
    monkeypatch.setattr(
        load_balancer,
        "_get_redis_or_none",
        AsyncMock(return_value=redis),
    )

    recorded = await load_balancer.mark_credential_rate_saturated(
        credential.id,
        error_code="2062",
    )

    assert recorded is False


@pytest.mark.asyncio
async def test_pick_skips_only_the_quota_blocked_modality():
    video_blocked = _cred(
        priority=10,
        modality_status={"video": {"status": "quota_exceeded"}},
    )
    fallback = _cred(priority=0)
    sess, _ = _patch_session(execute_result=[video_blocked, fallback])
    with sess:
        chosen = await pick_credential("minimax", "video")
    assert chosen.id == fallback.id

    sess, _ = _patch_session(execute_result=[video_blocked, fallback])
    with sess:
        chosen = await pick_credential("minimax", "text")
    assert chosen.id == video_blocked.id


@pytest.mark.asyncio
async def test_pick_scopes_non_text_quota_to_the_concrete_model():
    hailuo_02_blocked = _cred(
        priority=10,
        modality_status={
            "video:minimax-hailuo-02": {"status": "quota_exceeded"},
        },
    )
    fallback = _cred(priority=0)

    sess, _ = _patch_session(execute_result=[hailuo_02_blocked, fallback])
    with sess:
        chosen = await pick_credential(
            "minimax",
            "video",
            quota_modality="video",
            quota_model="MiniMax-Hailuo-02",
        )
    assert chosen.id == fallback.id

    sess, _ = _patch_session(execute_result=[hailuo_02_blocked, fallback])
    with sess:
        chosen = await pick_credential(
            "minimax",
            "video",
            quota_modality="video",
            quota_model="MiniMax-Hailuo-2.3",
        )
    assert chosen.id == hailuo_02_blocked.id


@pytest.mark.asyncio
async def test_pick_scopes_variable_cost_video_quota_to_resolution():
    seedance_1080p_blocked = _cred(
        priority=10,
        modality_status={
            "video:doubao-seedance-2.0@1080p": {"status": "quota_exceeded"},
        },
    )
    fallback = _cred(priority=0)

    sess, _ = _patch_session(execute_result=[seedance_1080p_blocked, fallback])
    with sess:
        chosen = await pick_credential(
            "minimax",
            "video",
            quota_modality="video",
            quota_model="doubao-seedance-2.0@1080p",
        )
    assert chosen.id == fallback.id

    sess, _ = _patch_session(execute_result=[seedance_1080p_blocked, fallback])
    with sess:
        chosen = await pick_credential(
            "minimax",
            "video",
            quota_modality="video",
            quota_model="doubao-seedance-2.0@480p",
        )
    assert chosen.id == seedance_1080p_blocked.id


@pytest.mark.asyncio
async def test_understanding_route_uses_shared_plan_without_media_generation_circuit():
    credential = _cred(
        capabilities=["text", "image", "video"],
        modality_status={
            "image:image-01": {"status": "quota_exceeded"},
        },
    )
    sess, _ = _patch_session(execute_result=[credential])
    with sess:
        chosen = await pick_credential(
            "minimax",
            "image",
            quota_modality="plan",
        )
    assert chosen.id == credential.id


@pytest.mark.asyncio
async def test_shared_plan_circuit_blocks_every_capability():
    plan_blocked = _cred(
        priority=10,
        capabilities=["text", "image", "video"],
        modality_status={"plan": {"status": "quota_exceeded"}},
    )
    fallback = _cred(priority=0, capabilities=["text", "image", "video"])

    sess, _ = _patch_session(execute_result=[plan_blocked, fallback])
    with sess:
        chosen = await pick_credential(
            "minimax",
            "video",
            quota_modality="video",
            quota_model="MiniMax-Hailuo-2.3",
        )
    assert chosen.id == fallback.id


@pytest.mark.asyncio
async def test_pick_uses_only_the_centrally_funded_platform_pool():
    cred = _cred()
    sess, fake_db = _patch_session(execute_result=[cred])
    with sess:
        await pick_credential("minimax", "text")

    query = str(fake_db.execute.await_args.args[0])
    assert "llm_credentials.tenant_id IS NULL" in query


@pytest.mark.asyncio
async def test_pick_casts_capabilities_for_json_and_jsonb_schemas():
    """Fresh bootstrap DBs use JSON while upgraded DBs historically use JSONB."""

    credential = _cred(capabilities=["text"])
    session, fake_db = _patch_session(execute_result=[credential])

    with session:
        await pick_credential("minimax", "text")

    query = str(fake_db.execute.await_args.args[0]).lower()
    assert "cast(llm_credentials.capabilities as jsonb)" in query


@pytest.mark.asyncio
async def test_pick_empty_pool_raises():
    sess, _ = _patch_session(execute_result=[])
    with sess:
        with pytest.raises(NoCredentialAvailable):
            await pick_credential("minimax", "text")


@pytest.mark.asyncio
async def test_pick_updates_last_used_and_commits():
    cred = _cred(priority=0, weight=1)
    sess, fake_db = _patch_session(execute_result=[cred])
    with sess:
        await pick_credential("minimax", "text")
    assert cred.last_used_at is not None
    fake_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_increment_marks_quota_exceeded_at_cap():
    cred = _cred(daily_quota=10, used_today=9)
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await increment_credential_usage(cred.id, weight=1)
    assert cred.used_today == 10
    assert cred.status == "quota_exceeded"


@pytest.mark.asyncio
async def test_increment_no_quota_limit_stays_healthy():
    cred = _cred(daily_quota=None, used_today=100)
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await increment_credential_usage(cred.id, weight=1)
    assert cred.status == "healthy"


@pytest.mark.asyncio
async def test_success_resets_prior_error_count():
    """Health degradation is based on consecutive failures, not lifetime errors."""
    cred = _cred(error_count=4)
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await increment_credential_usage(cred.id, weight=1)
    assert cred.error_count == 0
    assert cred.status == "healthy"


@pytest.mark.asyncio
async def test_success_does_not_race_authoritative_quota_circuits():
    cred = _cred(
        modality_status={
            "plan": {"status": "quota_exceeded"},
            "video:minimax-hailuo-02": {"status": "quota_exceeded"},
        },
    )
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await increment_credential_usage(cred.id, weight=1)
    assert set(cred.modality_status) == {"plan", "video:minimax-hailuo-02"}


@pytest.mark.asyncio
async def test_success_does_not_re_admit_explicitly_degraded_credential():
    cred = _cred(status="degraded", error_count=4)
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await increment_credential_usage(cred.id, weight=1)
    assert cred.error_count == 0
    assert cred.status == "degraded"


@pytest.mark.asyncio
async def test_inflight_success_at_daily_cap_does_not_reclassify_degraded_credential():
    cred = _cred(
        status="degraded",
        daily_quota=10,
        used_today=9,
        error_count=4,
    )
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await increment_credential_usage(cred.id, weight=1)

    assert cred.used_today == 10
    assert cred.error_count == 0
    assert cred.status == "degraded"


@pytest.mark.parametrize(
    ("credentials", "modality", "expected"),
    [
        ([], "text", load_balancer.CredentialUnavailableReason.NOT_CONFIGURED),
        ([_cred(status="degraded")], "text", load_balancer.CredentialUnavailableReason.ALL_UNHEALTHY),
        ([_cred(status="quota_exceeded")], "text", load_balancer.CredentialUnavailableReason.QUOTA_EXHAUSTED),
        ([_cred(modality_status={"video": {"status": "quota_exceeded"}})], "video", load_balancer.CredentialUnavailableReason.QUOTA_EXHAUSTED),
        ([_cred(capabilities=["text"])], "video", load_balancer.CredentialUnavailableReason.CAPABILITY_MISMATCH),
    ],
)
def test_base_filter_failure_has_structured_reason(credentials, modality, expected):
    assert load_balancer._diagnose_base_filter_failure(credentials, modality) is expected


def test_empty_capabilities_mean_no_capability_not_all():
    credential = _cred(capabilities=[])

    assert load_balancer._credential_supports_modality(credential, "text") is False
    assert (
        load_balancer._diagnose_base_filter_failure([credential], "text")
        is load_balancer.CredentialUnavailableReason.CAPABILITY_MISMATCH
    )


def test_no_credential_user_message_does_not_expose_pool_internals():
    error = load_balancer.NoCredentialAvailable(
        "minimax",
        "video",
        load_balancer.CredentialUnavailableReason.RATE_SATURATED,
        "credential-id=secret-internal-detail",
    )
    message = load_balancer.no_credential_user_message(error)
    assert "繁忙" in message
    assert "credential-id" not in message


@pytest.mark.asyncio
async def test_mark_degraded_past_threshold():
    cred = _cred()
    cred.error_count = 4
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await mark_credential_degraded(cred.id, threshold=5)
    assert cred.error_count == 5
    assert cred.status == "degraded"


@pytest.mark.asyncio
async def test_reset_daily_resets_all_counters_but_only_restores_local_daily_cap():
    exhausted = _cred(status="quota_exceeded", daily_quota=100, used_today=100, error_count=3)
    degraded = _cred(status="degraded", used_today=50, error_count=5)
    provider_exhausted = _cred(status="quota_exceeded", daily_quota=None, used_today=10, error_count=1)
    healthy = _cred(status="healthy", daily_quota=100, used_today=40, error_count=0)
    healthy.modality_status = {
        "video": {"status": "quota_exceeded", "reset_scope": "daily"},
        "text": {"status": "quota_exceeded", "reset_scope": "rolling_5h"},
    }
    sess, _ = _patch_session(execute_result=[exhausted, degraded, provider_exhausted, healthy])
    with sess:
        count = await reset_daily_usage()
    assert count == 4
    assert exhausted.status == "healthy" and exhausted.used_today == 0
    assert degraded.status == "degraded" and degraded.error_count == 5 and degraded.used_today == 0
    assert provider_exhausted.status == "quota_exceeded" and provider_exhausted.used_today == 0
    assert healthy.status == "healthy" and healthy.used_today == 0
    assert set(healthy.modality_status) == {"text", "video"}


@pytest.mark.asyncio
async def test_modality_quota_mutators_do_not_poison_global_status():
    cred = _cred()
    sess, fake_db = _patch_session(get_value=cred)
    with sess:
        await load_balancer.mark_credential_modality_quota_exceeded(
            cred.id,
            "video",
            error_code="2056",
        )
    fake_db.get.assert_awaited_once_with(
        load_balancer.LLMCredential,
        cred.id,
        with_for_update=True,
    )
    assert cred.status == "healthy"
    assert cred.modality_status["video"]["status"] == "quota_exceeded"
    assert cred.error_count == 1

    sess, _ = _patch_session(get_value=cred)
    with sess:
        await load_balancer.mark_credential_modality_quota_exceeded(
            cred.id,
            "video",
            error_code="2056",
        )
    assert cred.error_count == 1

    sess, fake_db = _patch_session(get_value=cred)
    with sess:
        assert await load_balancer.clear_credential_modality_quota(cred.id, "video") is True
    fake_db.get.assert_awaited_once_with(
        load_balancer.LLMCredential,
        cred.id,
        with_for_update=True,
    )
    assert cred.modality_status == {}


@pytest.mark.asyncio
async def test_model_quota_mutators_preserve_other_models():
    cred = _cred()
    sess, _ = _patch_session(get_value=cred)
    with sess:
        await load_balancer.mark_credential_modality_quota_exceeded(
            cred.id,
            "video",
            error_code="2056",
            model="MiniMax-Hailuo-02",
        )
    assert set(cred.modality_status) == {"video:minimax-hailuo-02"}
    assert cred.modality_status["video:minimax-hailuo-02"]["reset_scope"] == "provider_evidence"

    cred.modality_status["video:minimax-hailuo-2.3"] = {
        "status": "quota_exceeded",
    }
    sess, _ = _patch_session(get_value=cred)
    with sess:
        assert await load_balancer.clear_credential_modality_quota(
            cred.id,
            "video",
            model="MiniMax-Hailuo-02",
        ) is True
    assert set(cred.modality_status) == {"video:minimax-hailuo-2.3"}


@pytest.mark.asyncio
async def test_pick_skips_when_no_modality_filter():
    """modality=None → no capabilities filter (picks any healthy cred)."""
    cred = _cred(priority=0, weight=1, capabilities=None)
    sess, _ = _patch_session(execute_result=[cred])
    with sess:
        chosen = await pick_credential("minimax", None)
    assert chosen.id == cred.id


@pytest.mark.asyncio
async def test_pick_without_capability_still_honors_shared_plan_circuit():
    plan_blocked = _cred(
        priority=10,
        modality_status={"plan": {"status": "quota_exceeded"}},
    )
    fallback = _cred(priority=0)
    sess, _ = _patch_session(execute_result=[plan_blocked, fallback])
    with sess:
        chosen = await pick_credential("minimax", None)
    assert chosen.id == fallback.id
