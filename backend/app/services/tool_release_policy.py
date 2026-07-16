"""Release-scoped tool availability fences.

These names stay registered so historical rows can be identified and
quarantined, but they must never be exposed to an LLM or executed in RC5.
"""

RELEASE_DISABLED_TOOL_NAMES = frozenset(
    {
        # The legacy contract asks the model to supply provider credentials in
        # ``login_config``. Re-enable only after login uses a server-side,
        # owner-scoped credential reference and a separately reviewed approval
        # flow.
        "agentbay_browser_login",
        # The current SDK download API has no proven streaming/remote-size
        # hard cap, and outbound transfers do not yet have a durable approval
        # replay contract. The implementation is hardened for future work, but
        # the product surface remains fail-closed for RC5.
        "agentbay_file_transfer",
    }
)


def release_tool_denial_reason(tool_name: str) -> str | None:
    if tool_name in RELEASE_DISABLED_TOOL_NAMES:
        return (
            "This tool is disabled by the release safety policy. "
            "Use an administrator-managed, server-side credential flow instead."
        )
    return None
