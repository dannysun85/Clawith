"""Tests for MiniMax-specific error classification and credential failover."""

from types import SimpleNamespace

import pytest

from app.services.llm.caller import _minimax_m3_request_options
from app.services.llm.client import LLMError
from app.services.llm.utils import LLMMessage, create_llm_client
from app.services.llm.failover import (
    CredentialFailureAction,
    FailoverErrorType,
    classify_error,
    credential_failure_action,
    extract_minimax_code,
    is_auth_error,
    is_billing_or_quota_error,
    is_rate_limit_error,
)
from app.services.llm.load_balancer import mark_credential_quota_exceeded


# --- extract_minimax_code ---


class TestExtractMinimaxCode:
    def test_extracts_code_from_parentheses_format(self):
        assert extract_minimax_code("API error (1004): auth failed") == "1004"

    def test_extracts_code_from_equals_format(self):
        assert extract_minimax_code("API error (code=2056): quota") == "2056"

    def test_extracts_code_from_http_body_format(self):
        msg = 'HTTP 401: ...(1004)'
        assert extract_minimax_code(msg) == "1004"

    def test_returns_none_when_no_code(self):
        assert extract_minimax_code("random error without code") is None


# --- classify_error MiniMax codes ---


class TestClassifyMinimaxErrors:
    def test_auth_error_1004_is_non_retryable(self):
        err = LLMError("HTTP 401: login fail (1004)")
        assert classify_error(err) == FailoverErrorType.NON_RETRYABLE

    def test_invalid_key_2049_is_non_retryable(self):
        err = LLMError("API error (code=2049): Invalid API Key")
        assert classify_error(err) == FailoverErrorType.NON_RETRYABLE

    def test_insufficient_balance_1008_is_non_retryable(self):
        err = LLMError("API error (1008): 余额不足")
        assert classify_error(err) == FailoverErrorType.NON_RETRYABLE

    def test_quota_exceeded_2056_is_non_retryable(self):
        err = LLMError("Stream error (code=2056): token plan resource limit exceeded")
        assert classify_error(err) == FailoverErrorType.NON_RETRYABLE

    def test_rate_limit_1002_is_retryable(self):
        err = LLMError("API error (1002): rate limit")
        assert classify_error(err) == FailoverErrorType.RETRYABLE

    def test_rate_growth_2045_is_retryable(self):
        err = LLMError("Stream error (2045): request rate growth exceeded")
        assert classify_error(err) == FailoverErrorType.RETRYABLE

    def test_token_plan_high_traffic_2062_is_retryable(self):
        err = LLMError("Request rejected (429): Token Plan traffic is high (2062)")
        assert classify_error(err) == FailoverErrorType.RETRYABLE
        assert is_rate_limit_error(err)

    def test_timeout_1001_is_retryable(self):
        err = LLMError("API error (code=1001): timeout")
        assert classify_error(err) == FailoverErrorType.RETRYABLE

    def test_internal_error_1013_is_retryable(self):
        err = LLMError("API error (1013): internal error")
        assert classify_error(err) == FailoverErrorType.RETRYABLE

    def test_param_error_2013_is_non_retryable(self):
        err = LLMError("API error (2013): param error")
        assert classify_error(err) == FailoverErrorType.NON_RETRYABLE

    @pytest.mark.parametrize(
        "message",
        [
            "load balancing operation timed out",
            "authoritative upstream response timed out",
            "account balance lookup timed out",
        ],
    )
    def test_incidental_auth_or_balance_substrings_do_not_disable_retry(self, message):
        assert classify_error(LLMError(message)) == FailoverErrorType.RETRYABLE


def test_minimax_payload_uses_only_documented_chat_parameters():
    client = create_llm_client(
        provider="minimax",
        api_key="test-key",
        model="MiniMax-M2.7",
    )

    payload = client._build_payload(
        messages=[LLMMessage(role="system", content="system"), LLMMessage(role="user", content="hello")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": "Finish",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        temperature=0.2,
        max_tokens=321,
    )

    assert payload["max_completion_tokens"] == 321
    assert payload["tool_choice"] == "auto"
    assert "max_tokens" not in payload
    assert "parallel_tool_calls" not in payload
    assert "thinking" not in payload
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("thinking", "service_tier"),
    [("disabled", "standard"), ("adaptive", "standard"), ("adaptive", "priority")],
)
def test_minimax_m3_payload_uses_tier_request_policy(thinking, service_tier):
    model = SimpleNamespace(
        provider="minimax",
        model="MiniMax-M3",
        capabilities={"thinking": thinking, "service_tier": service_tier},
    )
    client = create_llm_client(provider="minimax", api_key="test-key", model=model.model)

    payload = client._build_payload(
        messages=[LLMMessage(role="user", content="hello")],
        tools=None,
        temperature=1,
        max_tokens=321,
        **_minimax_m3_request_options(model),
    )

    assert payload["thinking"] == {"type": thinking}
    assert payload["service_tier"] == service_tier
    assert payload["reasoning_split"] is True


def test_minimax_m2_does_not_receive_m3_request_policy():
    model = SimpleNamespace(provider="minimax", model="MiniMax-M2.7", capabilities={})

    assert _minimax_m3_request_options(model) == {}


# --- is_auth_error / is_billing_or_quota_error ---


class TestErrorCategoryPredicates:
    def test_auth_error_keywords(self):
        assert is_auth_error(LLMError("Invalid API key provided"))
        assert is_auth_error(LLMError("Unauthorized (401)"))
        assert is_auth_error(LLMError("login fail (1004)"))
        assert not is_auth_error(LLMError("rate limit exceeded"))

    def test_billing_error_keywords(self):
        assert is_billing_or_quota_error(LLMError("insufficient balance"))
        assert is_billing_or_quota_error(LLMError("余额不足 (1008)"))
        assert is_billing_or_quota_error(LLMError("token plan resource limit (2056)"))
        assert is_billing_or_quota_error(LLMError("quota exceeded"))
        assert not is_billing_or_quota_error(LLMError("timeout 1001"))


class TestCredentialFailurePolicy:
    @pytest.mark.parametrize(
        "message",
        [
            "MiniMax API error (1000): unexpected error",
            "API error (1001): timeout",
            "API error (1002): rate limit",
            "API error (2062): Token Plan traffic is high",
            "API error (2013): param error",
            "API error (1026): sensitive content",
            "connection reset by peer",
            "load balancing operation timed out",
            "authoritative upstream response timed out",
        ],
    )
    def test_operation_scoped_errors_never_poison_shared_pool(self, message):
        assert credential_failure_action(LLMError(message)) is CredentialFailureAction.NONE

    @pytest.mark.asyncio
    async def test_2062_opens_short_cooldown_without_poisoning_pool(self, monkeypatch):
        from unittest.mock import AsyncMock

        from app.services.llm import caller

        cooldown = AsyncMock(return_value=True)
        degrade = AsyncMock()
        quota = AsyncMock()
        monkeypatch.setattr(caller, "mark_credential_rate_saturated", cooldown)
        monkeypatch.setattr(caller, "mark_credential_degraded", degrade)
        monkeypatch.setattr(caller, "mark_credential_quota_exceeded", quota)

        credential_id = "credential-id"
        await caller._apply_credential_failure_policy(
            credential_id,
            LLMError("Request rejected (429): Token Plan traffic is high (2062)"),
            log_context="test",
            modality="plan",
        )

        cooldown.assert_awaited_once_with(credential_id, error_code="2062")
        degrade.assert_not_awaited()
        quota.assert_not_awaited()

    def test_invalid_key_opens_shared_pool_circuit(self):
        assert credential_failure_action(LLMError("login fail (1004)")) is CredentialFailureAction.DEGRADE

    def test_provider_plan_exhaustion_is_separate_from_bad_key(self):
        assert (
            credential_failure_action(LLMError("token plan resource limit (2056)"))
            is CredentialFailureAction.QUOTA_EXCEEDED
        )

    def test_provider_quota_limit_uses_the_explicit_allowance_resource(self):
        assert (
            credential_failure_action(
                LLMError("token plan resource limit (2056)"),
                modality="plan",
            )
            is CredentialFailureAction.MODALITY_QUOTA_EXCEEDED
        )

    @pytest.mark.asyncio
    async def test_media_transient_error_does_not_call_pool_mutators(self, monkeypatch):
        from unittest.mock import AsyncMock

        from app.services import agent_tools
        from app.services.llm import load_balancer

        degrade = AsyncMock()
        quota = AsyncMock()
        monkeypatch.setattr(load_balancer, "mark_credential_degraded", degrade)
        monkeypatch.setattr(load_balancer, "mark_credential_quota_exceeded", quota)

        await agent_tools._mark_minimax_tool_credential_failure(
            "credential-id",
            LLMError("MiniMax API error (1000): unexpected error"),
            modality="video",
        )

        degrade.assert_not_awaited()
        quota.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_media_2056_opens_shared_plan_until_named_evidence_arrives(self, monkeypatch):
        from unittest.mock import AsyncMock

        from app.services import agent_tools
        from app.services.llm import load_balancer

        degrade = AsyncMock()
        global_quota = AsyncMock()
        scoped_quota = AsyncMock()
        monkeypatch.setattr(load_balancer, "mark_credential_degraded", degrade)
        monkeypatch.setattr(load_balancer, "mark_credential_quota_exceeded", global_quota)
        monkeypatch.setattr(load_balancer, "mark_credential_modality_quota_exceeded", scoped_quota)

        await agent_tools._mark_minimax_tool_credential_failure(
            "credential-id",
            LLMError("MiniMax API error (2056): resource limit"),
            modality="video",
        )

        scoped_quota.assert_awaited_once_with(
            "credential-id",
            "plan",
            error_code="2056",
        )
        degrade.assert_not_awaited()
        global_quota.assert_not_awaited()


# --- mark_credential_quota_exceeded ---


class TestMarkCredentialQuotaExceeded:
    """Test quota_exceeded marking via the same session-mocking pattern as test_load_balancer.py."""

    @pytest.mark.asyncio
    async def test_marks_quota_exceeded_immediately(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        cred = SimpleNamespace(
            id="test-id",
            status="healthy",
            error_count=0,
        )

        fake_session = AsyncMock()
        fake_session.get = AsyncMock(return_value=cred)
        fake_session.commit = AsyncMock()

        fake_ctx = AsyncMock()
        fake_ctx.__aenter__ = AsyncMock(return_value=fake_session)
        fake_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.llm.load_balancer.async_session", return_value=fake_ctx):
            await mark_credential_quota_exceeded("test-id")

        assert cred.status == "quota_exceeded"
        assert cred.error_count == 1
        fake_session.commit.assert_awaited_once()
