"""Unified LLM failover error classification.

Provides error classification for failover decisions across all execution paths.
Also exposes helpers to identify specific error categories (auth, billing, quota)
that the credential pool uses for fast-fail vs degraded-marking decisions.
"""

from __future__ import annotations

import re
from enum import Enum

from .client import LLMError


class FailoverErrorType(Enum):
    """Classification of LLM errors for failover decisions."""

    RETRYABLE = "retryable"  # Network timeout, 429, 5xx, transient errors
    NON_RETRYABLE = "non_retryable"  # Auth, validation, schema, billing errors
    UNKNOWN = "unknown"


class CredentialFailureAction(Enum):
    """Persistent credential-state change justified by one provider error.

    The shared platform pool is global blast-radius infrastructure. Only an
    error that proves the credential itself is unusable may remove it from
    rotation. Request validation, policy, rate-limit, network and provider
    transient failures belong to the individual operation instead.
    """

    NONE = "none"
    DEGRADE = "degrade"
    QUOTA_EXCEEDED = "quota_exceeded"
    MODALITY_QUOTA_EXCEEDED = "modality_quota_exceeded"


# MiniMax-specific error codes (from base_resp.status_code)
# NON_RETRYABLE (fatal, should take credential out of rotation):
MINIMAX_AUTH_CODES = {"1004", "2049"}       # auth failed / invalid api key
MINIMAX_BILLING_CODES = {"1008"}             # insufficient balance
MINIMAX_QUOTA_CODES = {"2056"}               # Token Plan resource limit exceeded
MINIMAX_VALIDATION_CODES = {"2013", "1039"}  # param error / token limit exceeded
MINIMAX_POLICY_CODES = {"1026", "1027"}      # input/output sensitive content
MINIMAX_NOTFOUND_CODES = set()               # model not found (currently returns 2013)

# RETRYABLE (transient, safe to retry or fail over):
MINIMAX_RATELIMIT_CODES = {
    "1002",
    "1041",
    "2045",
    "2062",
}  # rate / connection / Token Plan interactive-traffic limit
MINIMAX_TRANSIENT_CODES = {"1000", "1001", "1013", "1024", "1033"}  # unknown/timeout/internal/downstream

# Pattern to extract MiniMax code from error strings like "(1004)" or "code=1004"
_MINIMAX_CODE_RE = re.compile(r"[(\s=](\d{4})")


def extract_minimax_code(error_msg: str) -> str | None:
    """Extract a MiniMax status_code from an error message if present."""
    m = _MINIMAX_CODE_RE.search(error_msg)
    return m.group(1) if m else None


def _structured_provider_code(error: Exception) -> str | None:
    value = getattr(error, "provider_code", None)
    return str(value).strip() if value is not None and str(value).strip() else None


def _structured_http_status(error: Exception) -> int | None:
    value = getattr(error, "http_status", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _match_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in text for kw in keywords)


def classify_error(error: Exception) -> FailoverErrorType:
    """Classify an exception as retryable or non-retryable.

    Retryable errors:
    - Network timeout / connection errors
    - Provider 429 / rate limit codes
    - Provider 5xx / internal server errors
    - Explicit transient provider errors (MiniMax 1000/1001/1013/1024/1033)

    Non-retryable errors:
    - Auth errors (401, 403, invalid key, MiniMax 1004/2049)
    - Billing / insufficient balance (MiniMax 1008)
    - Quota exhausted / plan limit (MiniMax 2056)
    - Validation / schema / param errors (400, 422, MiniMax 2013/1039)
    - Content policy violations
    """
    error_msg = str(error).lower()

    # Extract MiniMax-specific code if present (checked first — higher signal than keywords)
    mm_code = _structured_provider_code(error) or extract_minimax_code(error_msg)
    if mm_code:
        if mm_code in MINIMAX_AUTH_CODES | MINIMAX_BILLING_CODES | MINIMAX_QUOTA_CODES | MINIMAX_VALIDATION_CODES | MINIMAX_POLICY_CODES:
            return FailoverErrorType.NON_RETRYABLE
        if mm_code in MINIMAX_RATELIMIT_CODES | MINIMAX_TRANSIENT_CODES:
            return FailoverErrorType.RETRYABLE

    # Only precise credential failures are non-retryable. Broad substrings such
    # as ``auth`` and ``balance`` also occur in transient messages like
    # "authoritative upstream timeout" and "load balancing timeout".
    if is_auth_error(error):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: billing / balance / quota
    if is_billing_or_quota_error(error):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: validation and schema
    if _match_any(error_msg, (
        "validation", "invalid request", "schema", "bad request",
        "invalid model", "model not found", "unsupported model",
    )):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: content policy
    if _match_any(error_msg, ("content policy", "content_filter", "safety", "moderation", "sensitive")):
        return FailoverErrorType.NON_RETRYABLE

    http_status = _structured_http_status(error)
    if http_status in {408, 429, 500, 502, 503, 504}:
        return FailoverErrorType.RETRYABLE
    if http_status in {400, 401, 403, 404, 409, 413, 422}:
        return FailoverErrorType.NON_RETRYABLE

    # Retryable: rate limiting
    if _match_any(error_msg, ("rate limit", "429", "too many requests")):
        return FailoverErrorType.RETRYABLE

    # Retryable: server errors
    if _match_any(error_msg, ("500", "502", "503", "504", "server error", "internal error")):
        return FailoverErrorType.RETRYABLE

    # Retryable: network and timeout
    if _match_any(error_msg, (
        "timeout", "timed out", "connection", "network", "unreachable", "refused", "reset", "dns",
    )):
        return FailoverErrorType.RETRYABLE

    # Retryable: transient errors
    if _match_any(error_msg, ("temporary", "transient", "unavailable", "overloaded", "busy")):
        return FailoverErrorType.RETRYABLE

    # Generic LLMError/Exception patterns
    if isinstance(error, (LLMError, Exception)):
        # HTTP status code substrings
        if _match_any(error_msg, ("401", "403", "422")):
            return FailoverErrorType.NON_RETRYABLE
        # 400 is non-retryable (bad request) — but only if it looks like an HTTP code,
        # not a MiniMax business code (already handled above).
        if "http 400" in error_msg or "status 400" in error_msg:
            return FailoverErrorType.NON_RETRYABLE
        if _match_any(error_msg, ("429", "500", "502", "503", "504", "408")):
            return FailoverErrorType.RETRYABLE

        # Error-prefixed strings default to retryable (conservative)
        if error_msg.startswith("[llm error]") or error_msg.startswith("[llm call error]") or error_msg.startswith("[error]"):
            return FailoverErrorType.RETRYABLE

    return FailoverErrorType.UNKNOWN


def is_auth_error(error: Exception) -> bool:
    """Return True if the error indicates an invalid/expired/rejected API key."""
    msg = str(error).lower()
    code = _structured_provider_code(error) or extract_minimax_code(msg)
    if code in MINIMAX_AUTH_CODES:
        return True
    return _match_any(msg, (
        "authentication failed",
        "authentication error",
        "authorization failed",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "api key invalid",
        "login fail",
        "invalid api secret",
    ))


def is_billing_or_quota_error(error: Exception) -> bool:
    """Return True if the error indicates insufficient balance or exhausted plan quota."""
    msg = str(error).lower()
    code = _structured_provider_code(error) or extract_minimax_code(msg)
    if code in MINIMAX_BILLING_CODES | MINIMAX_QUOTA_CODES:
        return True
    return _match_any(msg, (
        "insufficient balance",
        "balance insufficient",
        "balance not enough",
        "balance exhausted",
        "余额不足",
        "token plan resource limit",
        "资源耗尽",
        "quota exceeded",
        "quota exhausted",
        "credit exhausted",
    ))


def is_rate_limit_error(error: Exception) -> bool:
    """Return True if the error is a transient rate-limit (429 / MiniMax 1002/2045/2062)
    that may succeed on another credential or after a brief backoff."""
    msg = str(error).lower()
    code = _structured_provider_code(error) or extract_minimax_code(msg)
    if code in MINIMAX_RATELIMIT_CODES:
        return True
    if _structured_http_status(error) == 429:
        return True
    if _match_any(msg, ("rate limit", "429", "too many requests", "request frequency exceeded")):
        return True
    return False


def credential_failure_action(
    error: Exception,
    *,
    modality: str | None = None,
) -> CredentialFailureAction:
    """Return the only safe persistent pool action for ``error``.

    Authentication failures prove that the key is invalid and therefore open
    the circuit immediately. Provider billing/plan exhaustion is represented
    separately so an administrator can distinguish it from a bad key. Every
    other category is operation-scoped and must not poison the shared pool.
    """

    if is_auth_error(error):
        return CredentialFailureAction.DEGRADE
    # MiniMax 2056 identifies provider capacity exhaustion, but the response
    # itself does not identify a concrete model bucket. Callers must pass the
    # allowance resource they actually consumed (normally the shared ``plan``
    # resource). Exact media-model circuits are opened only from the remains
    # endpoint, which names the affected model explicitly.
    if (
        _structured_provider_code(error)
        or extract_minimax_code(str(error).lower())
    ) in MINIMAX_QUOTA_CODES and modality:
        return CredentialFailureAction.MODALITY_QUOTA_EXCEEDED
    if is_billing_or_quota_error(error):
        return CredentialFailureAction.QUOTA_EXCEEDED
    return CredentialFailureAction.NONE


__all__ = [
    "FailoverErrorType",
    "CredentialFailureAction",
    "classify_error",
    "credential_failure_action",
    "extract_minimax_code",
    "is_auth_error",
    "is_billing_or_quota_error",
    "is_rate_limit_error",
    "MINIMAX_AUTH_CODES",
    "MINIMAX_BILLING_CODES",
    "MINIMAX_QUOTA_CODES",
    "MINIMAX_RATELIMIT_CODES",
    "MINIMAX_TRANSIENT_CODES",
]
