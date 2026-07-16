"""Shared provider acceptance and deterministic-rejection contracts."""

from __future__ import annotations

from typing import Any


# MiniMax business codes that prove the request was rejected before generation.
# Transient/internal codes (1000/1001/1013/1024/1033) are intentionally absent:
# their outcome is ambiguous and any provider-inflight Credits hold must remain.
MINIMAX_DETERMINISTIC_REJECTION_CODES = frozenset(
    {
        "1002",  # rate limit
        "1004",  # authentication
        "1008",  # insufficient balance
        "1026",  # input policy
        "1039",  # token limit / validation
        "1041",  # concurrent request limit
        "2013",  # invalid parameter
        "2045",  # request growth limit
        "2049",  # invalid API key
        "2056",  # Token Plan resource exhausted
        "2062",  # Token Plan high-traffic rejection
    }
)


def is_minimax_deterministic_rejection_code(code: Any) -> bool:
    """Return whether a structured MiniMax code proves no generation began."""
    return str(code) in MINIMAX_DETERMINISTIC_REJECTION_CODES
