"""Customer-facing projections for private Runtime-only fields."""

from __future__ import annotations


PRIVATE_REASONING_MARKER = (
    "Internal reasoning is private. Tool execution records remain available."
)


def project_private_reasoning(value: object) -> str:
    """Return a stable marker without crossing the private reasoning boundary."""

    return PRIVATE_REASONING_MARKER if isinstance(value, str) and value.strip() else ""
