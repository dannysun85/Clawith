"""Privacy-safe production issue reporting for external channel connectors."""

from __future__ import annotations

import uuid

from app.services.production_issue_monitor import record_production_issue


async def record_channel_issue(
    *,
    channel: str,
    operation: str,
    agent_id: uuid.UUID,
    error_code: str,
    severity: str = "error",
) -> uuid.UUID | None:
    """Record a connector failure without persisting credentials or provider text."""

    safe_channel = channel.strip().lower()[:40] or "unknown"
    safe_operation = operation.strip().lower()[:60] or "operation"
    safe_error_code = error_code.strip()[:100] or "unknown"
    return await record_production_issue(
        source="channel_connector",
        category="channel",
        summary=f"{safe_channel.title()} channel {safe_operation} failed",
        severity=severity,
        error_code=safe_error_code,
        operation=f"{safe_channel}.{safe_operation}",
        agent_id=agent_id,
        metadata={
            "provider": safe_channel,
            "component": f"{safe_channel}_connector",
            "error_type": safe_error_code,
        },
    )
