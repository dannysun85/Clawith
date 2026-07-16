"""User-visible and internal trigger configuration boundaries."""

from __future__ import annotations

from app.config import get_settings


# Explicit user automation is operationally independent from platform-seeded
# heartbeat/OKR work. Every entry point imports this same operator gate.
AUTOMATIC_TRIGGER_EXECUTION_ENABLED = (
    get_settings().USER_AUTOMATION_EXECUTION_ENABLED
)


SERVER_CONTEXT_VERSION_KEY = "_server_context_version"
SERVER_CONTEXT_VERSION = 1
MATCH_CONTEXT_VERSION_KEY = "_match_context_version"
MATCH_CONTEXT_VERSION = 1

SAFE_PERSISTED_TRIGGER_STATE_KEYS = frozenset(
    {
        "_last_value",
        "_since_ts",
    }
)

SERVER_DELIVERY_CONTEXT_KEYS = frozenset(
    {
        "_notification_summary",
        "_origin_session_id",
        "_origin_source_channel",
        "_origin_user_id",
        # Human on_message watches use a distinct, server-resolved source
        # binding.  Origin metadata is only the delivery destination.
        "_watched_session_id",
        "_watched_source_channel",
        "_watched_user_id",
    }
)

SCHEDULED_EXECUTION_PAYLOAD_KEYS = frozenset(
    {
        "from_agent_name",
        "okr_member_id",
        "okr_member_type",
        "okr_report_date",
        "_matched_from",
        "_matched_from_agent_id",
        "_matched_message",
        "_matched_conversation_id",
        "_matched_message_id",
        "_notification_summary",
        "_origin_session_id",
        "_origin_source_channel",
        "_origin_user_id",
        "_source_message_id",
    }
)

A2A_EXECUTION_PAYLOAD_KEYS = frozenset(
    {
        "from_agent_name",
        "_a2a_kind",
        "_a2a_session_id",
        "_matched_from",
        "_matched_from_agent_id",
        "_matched_message",
        "_matched_conversation_id",
        "_matched_message_id",
        "_origin_session_id",
        "_origin_source_channel",
        "_origin_user_id",
        "_source_message_id",
    }
)

TRUSTED_EXECUTION_PAYLOAD_KEYS = (
    SCHEDULED_EXECUTION_PAYLOAD_KEYS | A2A_EXECUTION_PAYLOAD_KEYS
)

ON_MESSAGE_PUBLIC_BINDING_KEYS = frozenset(
    {
        "from_agent_name",
        "from_agent_id",
        "from_user_name",
        "source_channel",
        "source_session_id",
        "expected_conversation_id",
    }
)


def on_message_source_binding_error(config: dict | None) -> str | None:
    """Require exactly one human-or-Agent message source."""

    cfg = config or {}
    has_agent = bool(str(cfg.get("from_agent_name") or "").strip())
    has_human = bool(str(cfg.get("from_user_name") or "").strip())
    if has_agent == has_human:
        return (
            "on_message requires exactly one of config.from_agent_name or "
            "config.from_user_name"
        )
    return None


def changes_on_message_binding(
    stored_config: dict | None,
    incoming_config: dict | None,
) -> bool:
    """Return whether a partial update would retarget a durable message watch."""

    stored = stored_config or {}
    incoming = incoming_config or {}
    return any(
        key in incoming and incoming.get(key) != stored.get(key)
        for key in ON_MESSAGE_PUBLIC_BINDING_KEYS
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


def trusted_persisted_trigger_state(config: dict | None) -> dict:
    """Keep only state that current server code can prove it owns.

    Older releases accepted arbitrary underscore-prefixed values.  A stored
    value therefore does not become trusted merely because it is already in
    the database.  Safe evaluator cursors survive upgrades; routing context is
    preserved only when it carries the server-issued version marker.
    """

    cfg = config or {}
    trusted = {
        key: cfg[key]
        for key in SAFE_PERSISTED_TRIGGER_STATE_KEYS
        if key in cfg
    }
    if cfg.get(SERVER_CONTEXT_VERSION_KEY) == SERVER_CONTEXT_VERSION:
        trusted[SERVER_CONTEXT_VERSION_KEY] = SERVER_CONTEXT_VERSION
        trusted.update(
            {
                key: cfg[key]
                for key in SERVER_DELIVERY_CONTEXT_KEYS
                if key in cfg
            }
        )
    return trusted


def mark_server_owned_trigger_context(config: dict) -> None:
    """Mark delivery metadata added by current trusted application code."""

    config[SERVER_CONTEXT_VERSION_KEY] = SERVER_CONTEXT_VERSION


def mark_verified_message_context(config: dict) -> None:
    """Mark matched-message metadata produced by the current evaluator."""

    config[MATCH_CONTEXT_VERSION_KEY] = MATCH_CONTEXT_VERSION


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
    allowed_keys = (
        A2A_EXECUTION_PAYLOAD_KEYS
        if source == "a2a"
        else SCHEDULED_EXECUTION_PAYLOAD_KEYS
    )
    return {
        key: value
        for key, value in (payload or {}).items()
        if key in allowed_keys
    }


def trigger_delivery_identity(config: dict | None) -> tuple[str, str, str]:
    """Return the server-owned principal used to isolate trigger invocations."""

    cfg = config or {}
    return (
        str(cfg.get("_origin_session_id") or ""),
        str(cfg.get("_origin_user_id") or ""),
        str(cfg.get("_origin_source_channel") or ""),
    )
