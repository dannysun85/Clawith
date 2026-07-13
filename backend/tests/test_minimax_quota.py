"""MiniMax Token Plan evidence and scoped-circuit regressions."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm import minimax_quota


def _payload(rows):
    return {
        "base_resp": {"status_code": 0},
        "model_remains": rows,
    }


def test_parse_quota_observations_separates_general_and_media_models():
    observations = minimax_quota._parse_quota_observations(
        _payload([
            {
                "model_name": "general",
                "current_interval_status": 1,
                "current_interval_remaining_percent": 0,
                "current_weekly_status": 1,
                "current_weekly_remaining_percent": 80,
            },
            {
                "model_name": "image-01",
                "current_daily_status": 1,
                "current_daily_remaining_percent": 35,
            },
            {
                "model_name": "MiniMax-Hailuo-02",
                "current_daily_status": 2,
                "current_daily_remaining_percent": 15,
            },
            {
                "model_name": "speech-2.8-hd",
                "current_daily_status": 1,
                "current_daily_remaining_percent": 45,
            },
            {
                "model_name": "music-2.0",
                "current_daily_status": 3,
                "current_daily_remaining_percent": 100,
            },
        ])
    )

    assert {
        (observation.modality, observation.model): observation.depleted
        for observation in observations
    } == {
        ("plan", None): True,
        ("image", "image-01"): False,
        ("video", "MiniMax-Hailuo-02"): True,
        ("audio", "speech-2.8-hd"): False,
        ("music", "music-2.0"): False,
    }


def test_parse_quota_observations_does_not_treat_unknown_legacy_status_as_depletion():
    observations = minimax_quota._parse_quota_observations(
        _payload([
            {
                "model_name": "general",
                "current_interval_status": 0,
            },
        ])
    )
    assert observations == []


def test_parse_quota_observations_uses_official_count_and_unlimited_contract():
    observations = minimax_quota._parse_quota_observations(
        _payload([
            {
                "model_name": "general",
                "current_interval_status": 1,
                "current_interval_total_count": 100,
                "current_interval_usage_count": 100,
                "current_weekly_status": 1,
                "current_weekly_total_count": 1000,
                "current_weekly_usage_count": 200,
            },
            {
                "model_name": "MiniMax-M3",
                "current_interval_status": 3,
                "current_interval_total_count": 0,
                "current_interval_usage_count": 0,
                "current_weekly_status": 3,
            },
        ])
    )

    # Duplicate rows fail closed for the shared plan: an exhausted limited row
    # cannot be hidden by a separate unlimited row.
    assert observations == [
        minimax_quota.MiniMaxQuotaObservation("plan", None, True),
    ]


def test_status_three_is_unlimited_not_exhausted():
    observations = minimax_quota._parse_quota_observations(
        _payload([
            {
                "model_name": "general",
                "current_interval_status": 3,
                "current_weekly_status": 3,
            },
        ])
    )

    assert observations == [
        minimax_quota.MiniMaxQuotaObservation("plan", None, False),
    ]


@pytest.mark.parametrize("status_code", [1004, 2049])
def test_authentication_errors_are_not_misclassified_as_quota(status_code):
    with pytest.raises(minimax_quota.MiniMaxQuotaPollIndeterminate):
        minimax_quota._parse_quota_observations(
            {
                "base_resp": {"status_code": status_code},
                "model_remains": [],
            }
        )


@pytest.mark.asyncio
async def test_poller_marks_and_clears_exact_resources_without_reenabling_global_status(monkeypatch):
    credential = SimpleNamespace(
        id=uuid.uuid4(),
        provider="minimax",
        label="pool-a",
        base_url="https://api.minimax.io/v1",
        status="degraded",
        enabled=True,
    )
    fake_db = MagicMock()
    fake_result = MagicMock()
    fake_result.scalars.return_value.all.return_value = [credential]
    fake_db.execute = AsyncMock(return_value=fake_result)
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_db)
    fake_session.__aexit__ = AsyncMock(return_value=None)

    check = AsyncMock(return_value=[
        minimax_quota.MiniMaxQuotaObservation("plan", None, True),
        minimax_quota.MiniMaxQuotaObservation("video", "MiniMax-Hailuo-2.3", False),
    ])
    mark = AsyncMock()
    clear = AsyncMock()
    monkeypatch.setattr(minimax_quota, "get_credential_api_key", lambda _credential: "secret")
    monkeypatch.setattr(minimax_quota, "_check_credential_quota", check)
    monkeypatch.setattr(minimax_quota, "mark_credential_modality_quota_exceeded", mark)
    monkeypatch.setattr(minimax_quota, "clear_credential_modality_quota", clear)

    with patch.object(minimax_quota, "async_session", return_value=fake_session):
        depleted = await minimax_quota.poll_minimax_quota()

    assert depleted == 1
    check.assert_awaited_once_with(
        "secret",
        remains_url=minimax_quota.GLOBAL_REMAINS_URL,
    )
    mark.assert_awaited_once_with(
        credential.id,
        "plan",
        error_code="2056",
    )
    clear.assert_awaited_once_with(
        credential.id,
        "video",
        model="MiniMax-Hailuo-2.3",
    )
    query = str(fake_db.execute.await_args.args[0])
    assert "llm_credentials.tenant_id IS NULL" in query
    assert "llm_credentials.status IN" in query
