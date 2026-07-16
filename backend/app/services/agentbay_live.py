"""AgentBay live preview helpers.

Provides utility functions for fetching live preview data
(screenshots) from active AgentBay sessions. These are used
by the WebSocket handler to push real-time preview updates
to the frontend.

Note: get_link() (VNC URL) requires a paid AgentBay subscription
(Pro/Ultra), so we use screenshot-based preview for all environments.
"""

import uuid
from typing import Optional

from loguru import logger


async def get_desktop_screenshot(agent_id: uuid.UUID, session_id: str = "") -> Optional[str]:
    """Get a base64-encoded screenshot of an agent's active computer session.

    Uses computer_screenshot() to capture the current desktop state,
    then compresses to JPEG base64 for efficient WebSocket transfer.
    Returns data:image/jpeg;base64,... string or None on failure.

    Only the exact authorized chat lane is eligible. Shared Agents must never
    leak another user's desktop preview through an Agent-wide fallback.
    """
    from app.services.agentbay_client import get_existing_agentbay_client_for_agent

    client = await get_existing_agentbay_client_for_agent(
        agent_id, "computer", session_id
    )
    if client is None:
        logger.info("[AgentBay] No exact computer lane available for live preview")
        return None
    return await client.get_desktop_snapshot_base64()


async def get_browser_snapshot(agent_id: uuid.UUID, session_id: str = "") -> Optional[str]:
    """Get a base64-encoded screenshot of an agent's active browser session.

    Returns data:image/jpeg;base64,... string or None if no browser
    session is active or the screenshot fails.

    Only the exact authorized chat lane is eligible.
    """
    from app.services.agentbay_client import get_existing_agentbay_client_for_agent

    client = await get_existing_agentbay_client_for_agent(
        agent_id, "browser", session_id
    )
    if client is None:
        logger.info("[AgentBay] No exact browser lane available for live preview")
        return None
    return await client.get_browser_snapshot_base64()


def detect_agentbay_env(tool_name: str) -> Optional[str]:
    """Detect which AgentBay environment a tool belongs to.

    Returns 'desktop', 'browser', 'code', or None if not an AgentBay tool.
    """
    if tool_name.startswith("agentbay_computer_"):
        return "desktop"
    if tool_name.startswith("agentbay_browser_"):
        return "browser"
    if tool_name in ("agentbay_code_execute", "agentbay_command_exec"):
        return "code"
    return None
