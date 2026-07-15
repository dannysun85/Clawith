"""User-visible and internal trigger configuration boundaries."""

from __future__ import annotations


TRUSTED_EXECUTION_PAYLOAD_KEYS = frozenset(
    {
        "from_agent_name",
        "okr_member_id",
        "okr_member_type",
        "okr_report_date",
        "_a2a_kind",
        "_a2a_session_id",
        "_matched_from",
        "_matched_from_agent_id",
        "_matched_message",
        "_matched_message_id",
        "_notification_summary",
        "_origin_session_id",
        "_origin_source_channel",
        "_origin_user_id",
        "_source_message_id",
    }
)


def reserved_trigger_config_keys(config: dict | None) -> list[str]:
    """Return service-owned trigger keys that users must not read or mutate."""

    return sorted(
        key
        for key in (config or {})
        if isinstance(key, str) and key.startswith("_")
    )


def without_reserved_trigger_config(config: dict | None) -> dict:
    """Return a copy containing only user-managed trigger configuration."""

    return {
        key: value
        for key, value in (config or {}).items()
        if not (isinstance(key, str) and key.startswith("_"))
    }


def agent_visible_trigger_config(config: dict | None) -> dict:
    """Return config safe to expose to an Agent/LLM prompt or tool result."""

    return {
        key: value
        for key, value in without_reserved_trigger_config(config).items()
        if key not in {"secret", "token"}
    }


def trusted_execution_runtime_payload(source: str, payload: dict | None) -> dict:
    """Admit only service-generated execution context into runtime config.

    Webhook JSON is external data even after signature verification. It remains
    available only through the bounded payload text and can never become routing
    or lease metadata.
    """

    if source == "webhook":
        return {}
    return {
        key: value
        for key, value in (payload or {}).items()
        if key in TRUSTED_EXECUTION_PAYLOAD_KEYS
    }


def trigger_delivery_identity(config: dict | None) -> tuple[str, str, str]:
    """Return the server-owned principal used to isolate trigger invocations."""

    cfg = config or {}
    return (
        str(cfg.get("_origin_session_id") or ""),
        str(cfg.get("_origin_user_id") or ""),
        str(cfg.get("_origin_source_channel") or ""),
    )
