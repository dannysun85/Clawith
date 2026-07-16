"""AgentBay Take Control API — human-agent collaborative login.

Provides REST endpoints for forwarding mouse/keyboard events to an
AgentBay session and managing the Take Control lock. When locked,
the agent's automatic browser/computer tool execution is paused to
prevent human-agent input collisions.

Credential export is intentionally disabled until site-bound capture can be
proven without exposing another origin's cookies.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import privacy_safe_shape
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.agentbay_control_lock import (
    AgentBayAgentDeleting,
    AgentBayInteractionBusy,
    AgentBayToolExecutionActive,
    TakeControlLock,
    acquire_take_control_lock,
    agentbay_control_interaction_mutex,
    get_take_control_lock,
    release_take_control_lock,
)
from app.services.agentbay_client import _canonical_chat_session_id
from app.services.chat_session_access import (
    ChatSessionAuthorizationError,
    validate_active_user_chat_lane,
)

router = APIRouter(prefix="/agents/{agent_id}/control", tags=["agentbay-control"])


# Cache of sessions that have already had browser initialization called.
# Avoids redundant _ensure_browser_initialized() on every screenshot poll.
_browser_initialized: set[tuple] = set()

async def _require_owned_control_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    current_user: User,
) -> None:
    """Validate one exact active user-owned lane in a short transaction."""

    try:
        session_uuid = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        await validate_active_user_chat_lane(
            db,
            agent_id=agent_id,
            owner_user_id=current_user.id,
            session_id=session_uuid,
            lock_authority=False,
        )
    except ChatSessionAuthorizationError as exc:
        raise HTTPException(
            status_code=403,
            detail="Take Control session authority is no longer active",
        ) from exc
    finally:
        # Provider/Redis operations must never retain a pooled DB connection or
        # authority row locks. Callers revalidate before returning any result.
        await db.rollback()


async def _require_control_lock_holder(
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID,
) -> TakeControlLock:
    """Return and refresh one shared lock only to its exact holder."""

    try:
        entry = await get_take_control_lock(agent_id, session_id)
    except AgentBayToolExecutionActive as exc:
        raise HTTPException(
            status_code=423,
            detail="An AgentBay tool operation is still finishing",
        ) from exc
    except Exception as exc:
        logger.error(
            "[TakeControl] Shared lock lookup failed error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Take Control lock service is unavailable",
        ) from exc
    if entry is None:
        raise HTTPException(status_code=409, detail="Take Control lock is not active")
    if entry.env_type == "code":
        raise HTTPException(
            status_code=409,
            detail="Take Control for Code environments is disabled",
        )
    if entry.user_id != str(user_id):
        raise HTTPException(
            status_code=423,
            detail="This session is controlled by another user",
        )
    try:
        refreshed, holder = await acquire_take_control_lock(
            agent_id,
            session_id,
            user_id=user_id,
            env_type=entry.env_type,
        )
    except AgentBayToolExecutionActive as exc:
        raise HTTPException(
            status_code=423,
            detail="An AgentBay tool operation is still finishing",
        ) from exc
    except Exception as exc:
        logger.error(
            "[TakeControl] Shared lock refresh failed error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Take Control lock service is unavailable",
        ) from exc
    if not refreshed or holder is None:
        if holder is not None and holder.user_id != str(user_id):
            raise HTTPException(
                status_code=423,
                detail="This session is controlled by another user",
            )
        raise HTTPException(status_code=409, detail="Take Control lock is not active")
    return holder


@asynccontextmanager
async def _serialized_control_interaction(
    agent_id: uuid.UUID,
    session_id: str,
):
    """Map the shared control mutex to stable HTTP failure semantics."""

    try:
        async with agentbay_control_interaction_mutex(agent_id, session_id):
            yield
    except AgentBayInteractionBusy as exc:
        raise HTTPException(
            status_code=409,
            detail="Another Take Control interaction is still running",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[TakeControl] Shared interaction mutex failed error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Take Control interaction service is unavailable",
        ) from exc


# ── Request schemas ──


class ClickRequest(BaseModel):
    """Mouse click event forwarding."""
    session_id: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, le=10000)
    y: int = Field(ge=0, le=10000)
    button: Literal["left", "right", "middle"] = "left"


class TypeRequest(BaseModel):
    """Text input event forwarding."""
    session_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)


class PressKeysRequest(BaseModel):
    """Keyboard key press event forwarding."""
    session_id: str = Field(min_length=1, max_length=64)
    keys: list[str] = Field(min_length=1, max_length=4)

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, value: list[str]) -> list[str]:
        """Accept only Playwright key names or one printable character."""

        named_keys = {
            "alt",
            "backspace",
            "control",
            "ctrl",
            "delete",
            "end",
            "enter",
            "escape",
            "esc",
            "home",
            "insert",
            "meta",
            "pagedown",
            "pageup",
            "shift",
            "space",
            "tab",
            "arrowdown",
            "arrowleft",
            "arrowright",
            "arrowup",
        }
        normalized: list[str] = []
        for raw_key in value:
            key = raw_key.strip()
            lowered = key.lower()
            is_function_key = (
                lowered.startswith("f")
                and lowered[1:].isdigit()
                and 1 <= int(lowered[1:]) <= 12
            )
            is_printable_character = len(key) == 1 and key.isprintable()
            if not key or not (
                lowered in named_keys or is_function_key or is_printable_character
            ):
                raise ValueError("Unsupported keyboard key")
            normalized.append(key)
        return normalized


class DragRequest(BaseModel):
    """Mouse drag event forwarding — used for slider CAPTCHAs and drag-and-drop."""
    session_id: str = Field(min_length=1, max_length=64)
    from_x: int = Field(ge=0, le=10000)
    from_y: int = Field(ge=0, le=10000)
    to_x: int = Field(ge=0, le=10000)
    to_y: int = Field(ge=0, le=10000)
    duration_ms: int = Field(default=600, ge=100, le=5000)


class ScreenshotRequest(BaseModel):
    """Request an immediate screenshot."""
    session_id: str = Field(min_length=1, max_length=64)


class LockRequest(BaseModel):
    """Enter Take Control mode."""
    session_id: str = Field(min_length=1, max_length=64)
    platform_hint: Optional[str] = None  # current page domain (for cookie export)
    env_type: Optional[str] = "browser"  # which env the user is controlling: browser | computer | code


class UnlockRequest(BaseModel):
    """Exit Take Control mode."""
    session_id: str = Field(min_length=1, max_length=64)
    export_cookies: bool = True  # whether to export cookies on exit
    platform_hint: Optional[str] = None  # domain to associate cookies with


# ── Helpers ──


async def _get_client(agent_id: uuid.UUID, session_id: str, env_type: str = "browser"):
    """Retrieve the AgentBay client for the given agent + session.

    Search order:
    1. Exact match: (agent_id, session_id, env_type).
    2. Attach the durable provider session for that exact authorized lane.

    Never fall back to another cached session for the same Agent.  A shared
    Agent can have several simultaneous users and a cache hit is not an
    authorization boundary.

    IMPORTANT: For browser sessions, this also calls _ensure_browser_initialized()
    because the browser SDK requires explicit initialization before screenshot/
    interaction APIs will work. Without this, get_browser_snapshot_base64() returns
    None ("Browser not initialized") and all CDP-based interactions fail silently.
    """
    canonical_session_id = _canonical_chat_session_id(session_id)
    if canonical_session_id is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Attach or reuse an existing durable session, never create one. The
    # service revalidates ledger status, binding version, provider identity and
    # expiry even when an in-process cache entry exists.
    # Take Control is an operator surface over an already-created tool lane. A
    # preview/poll request must never allocate a provider sandbox as a side
    # effect, especially while authority locks are held.
    from app.services.agentbay_client import get_existing_agentbay_client_for_agent

    logger.warning(
        f"[TakeControl] Validating durable AgentBay session for agent={agent_id} "
        f"(env_type={env_type})."
    )
    try:
        client = await get_existing_agentbay_client_for_agent(
            agent_id, image_type=env_type, session_id=canonical_session_id
        )
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active {env_type} session found",
            )
        if env_type == "browser":
            try:
                await client._ensure_browser_initialized()
                _browser_initialized.add((agent_id, canonical_session_id, "browser"))
                logger.info(
                    "[TakeControl] Browser initialized for attached session, agent={}",
                    agent_id,
                )
            except Exception as exc:
                logger.warning(
                    "[TakeControl] Browser init on new session failed error_type={}",
                    type(exc).__name__,
                )
        return client
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "[TakeControl] Exact AgentBay session lookup failed error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active {env_type} session found",
        ) from exc



# ── Session-aware input helpers ──
# Browser sessions use CDP (Chrome DevTools Protocol) via Playwright to
# interact directly with Chrome. Desktop sessions use the SDK's computer API.


def _is_browser_session(client) -> bool:
    """Check if the client's active session is a browser image."""
    return getattr(client, "_image_type", "") in ("browser", "browser_latest")


async def _cdp_exec(client, script: str, timeout_ms: int = 15000) -> dict:
    """Execute a Playwright CDP script inside the AgentBay container.

    Uses the AgentBayClient.command_exec wrapper which properly handles
    the SDK call and returns a dict with {success, stdout, stderr, ...}.
    """
    # Write script to temp file inside the container
    write_result = await client.command_exec(
        f"cat > /tmp/_tc_action.js << 'TCEOF'\n{script}\nTCEOF",
        timeout_ms=5000,
    )
    if not write_result.get("success"):
        logger.error(
            "[TakeControl] Failed to write CDP script result_shape={}",
            privacy_safe_shape(write_result),
        )
        return {
            "success": False,
            "output": "AgentBay browser operation failed",
            "stderr": "",
        }

    result = await client.command_exec(
        "node /tmp/_tc_action.js",
        timeout_ms=timeout_ms,
    )
    stdout = result.get("stdout", "") or result.get("output", "") or ""
    stderr = result.get("stderr", "") or result.get("error_message", "") or ""
    cmd_success = result.get("success", False)
    tc_success = "TC_OK" in stdout

    logger.info(
        "[TakeControl] CDP exec cmd_success={} tc_ok={} stdout_chars={} "
        "stderr_chars={} exit_code={}",
        cmd_success,
        bool(tc_success),
        len(stdout),
        len(stderr),
        result.get("exit_code", "N/A"),
    )
    return {
        "success": tc_success,
        "output": stdout[:500] if tc_success else "AgentBay browser operation failed",
        "stderr": "",
    }


async def _eval_cdp_script(
    client,
    script_body: str,
    *,
    timeout_ms: int = 15000,
) -> dict:
    """Evaluate a Node.js Playwright CDP script in the browser container."""
    import base64
    try:
        # Base64 encode the script to avoid shell escaping issues inside the container
        script_b64 = base64.b64encode(script_body.encode('utf-8')).decode('ascii')
        
        # Write base64 to file and decode it to tc_action.js (in current working
        # directory, since /tmp might be restricted). AgentBay's command API
        # receives an explicit provider deadline for both steps.
        cmd_write = f"echo '{script_b64}' | /usr/bin/base64 -d > tc_action.js"
        write_result = await client.command_exec(cmd_write, timeout_ms=5000)
        if not write_result.get("success"):
            return {"success": False, "output": "AgentBay browser operation failed"}
        
        # Execute the script
        result = await client.command_exec(
            "node tc_action.js",
            timeout_ms=timeout_ms,
        )
        success = result.get("success", False)
        output = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        
        if not success:
            logger.error(
                "[TakeControl] CDP execution failed output_chars={} stderr_chars={}",
                len(output),
                len(stderr),
            )
            return {"success": False, "output": "AgentBay browser operation failed"}
            
        return {"success": True, "output": output}
    except Exception as exc:
        logger.error(
            "[TakeControl] CDP exception error_type={}",
            type(exc).__name__,
        )
        return {"success": False, "output": "AgentBay browser operation failed"}


async def _tc_browser_cleanup(agent_id: uuid.UUID, session_id: str) -> bool:
    """Best-effort cleanup immediately after Take Control exits.

    Uses the AgentBay SDK's own browser.operator.navigate() to navigate to
    about:blank. This goes through the SERVICE'S Playwright instance (not a
    new connectOverCDP connection), so there's no competing CDP session,
    no Target.attachToTarget/detachFromTarget events, and no risk of confusing
    the service's internal page state.

    IMPORTANT: Previous approaches that used connectOverCDP + browser.close()
    for cleanup were sending Target.detachFromTarget events to Chrome while
    navigation was in progress. The AgentBay service's Playwright received
    these detach events mid-navigation, which put its internal state machine
    into a 60-second recovery loop before it could accept the next page.goto().
    """
    from app.services.agentbay_client import _agentbay_sessions

    canonical_session_id = _canonical_chat_session_id(session_id)
    if canonical_session_id is None:
        return False
    cleanup_client = None
    for img_type in ("browser", "browser_latest"):
        ck = (agent_id, canonical_session_id, img_type)
        if ck in _agentbay_sessions:
            cleanup_client = _agentbay_sessions[ck][0]
            break
    if not cleanup_client:
        return True

    try:
        # Cleanup strategy: stop all in-flight page navigations, then navigate
        # the active content page to about:blank.
        #
        # WHY multi-step:
        # 1. stopLoading on all pages: a TC click may have opened a NEW TAB
        #    (target=_blank link on baidu) that is still loading a heavy article.
        #    Page.stopLoading kills that load immediately so Chrome's DevTools
        #    is no longer blocked draining a multi-MB response.
        # 2. Page.navigate to about:blank on the active page: gives the AgentBay
        #    service's page.goto() a clean starting point. about:blank commits in
        #    <10ms; the service no longer has to wait for tieba/zhihu/baidu to drain.
        # 3. Wait for Page.loadEventFired before process.exit(): ensures Chrome has
        #    fully settled at about:blank before we disconnect. This means Chrome
        #    emits Target.detachedFromTarget (from our WebSocket close) while the
        #    page is in a stable, loaded state — not mid-navigation — so the
        #    service's Playwright state machine doesn't enter a 60-second recovery.
        # 4. No browser.close(): we let Node.js exit naturally. Chrome handles
        #    the WebSocket close without an explicit Target.detachFromTarget CDP
        #    command that races with other async CDP events.
        cleanup_script = """
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    try {
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const allPages = context.pages();

        // Stop all loading pages so Chrome is not draining heavy responses.
        // tc clicks frequently open new tabs (target=_blank) that stay loading
        // for 20-40s; stopping them is critical for fast post-TC recovery.
        for (const p of allPages) {
            try {
                const cdp = await context.newCDPSession(p);
                await cdp.send('Page.stopLoading');
                await cdp.detach();
            } catch(_) {}
        }

        // Navigate the active content page (last non-blank) to about:blank.
        // Use raw CDP Page.navigate — the AgentBay SDK rejects about:blank
        // ("must start with http or https") but Chrome's CDP has no such rule.
        const contentPage = allPages.slice().reverse().find(p => p.url() !== 'about:blank')
                            || allPages[allPages.length - 1];
        const cdp = await context.newCDPSession(contentPage);

        // Navigate and wait for loadEventFired so about:blank is fully settled.
        await new Promise((resolve) => {
            cdp.on('Page.loadEventFired', () => resolve());
            cdp.send('Page.navigate', { url: 'about:blank' }).catch(() => resolve());
            setTimeout(resolve, 800);  // Fallback: about:blank always loads in <100ms
        });

        console.log('CLEANUP_OK');
    } catch(e) {
        console.error('CLEANUP_FAIL: ' + e.message);
    }
    // No browser.close() — let Chrome handle WebSocket close gracefully after
    // the page is in a stable loaded state (about:blank).
    process.exit(0);
})();
"""
        res = await _eval_cdp_script(cleanup_client, cleanup_script)
        logger.info(
            "[TakeControl] Cleanup completed agent={} output_chars={}",
            agent_id,
            len(res.get("output") or ""),
        )
        return bool(res.get("success")) and "CLEANUP_OK" in (res.get("output") or "")
    except Exception as exc:
        logger.warning(
            "[TakeControl] Cleanup failed (non-fatal) error_type={}",
            type(exc).__name__,
        )
        return False


async def _perform_click(client, x: int, y: int, button: str = "left"):
    """Click at (x, y) on the remote session.

    Browser sessions use connectOverCDP because the Computer API's click_mouse
    tool is only available in the computer image type, not browser_latest.
    Each CDP script uses try/catch/finally with browser.close() to ensure a
    graceful disconnect so Chrome's DevTools session does not leak.
    """
    image_type = getattr(client, '_image_type', 'unknown')
    logger.info(f"[TakeControl] Click at ({x}, {y}), button={button}, image_type={image_type}")

    if _is_browser_session(client):
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();

        // Page selection: prefer the last page with a committed non-blank URL.
        // When a tc click opens a new tab (target=_blank), the new tab briefly
        // has url() === 'about:blank' before its navigation commits. During that
        // window, we correctly target the ORIGINAL content page (the one the user
        // sees in the TC screenshot). The NEXT click, after the new tab has settled,
        // will naturally pick the new tab because its URL will be non-blank by then.
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank')
                     || pages[pages.length - 1];
        const initialUrl = page.url();
        const initialPageCount = pages.length;
        console.log('TARGET_PAGE:' + initialUrl);

        await page.mouse.click({x}, {y}, {{ button: {json.dumps(button)} }});
        console.log('CLICK_OK');
        ok = true;

        // Wait 2 seconds for any triggered navigation to commit before releasing
        // the interaction lock. This covers both cases:
        //   A) Same-tab navigation: URL commits in ~0.5-1s
        //   B) New-tab navigation (target=_blank): new tab URL transitions from
        //      about:blank to the target URL in ~1-2s
        //
        // WHY a fixed sleep instead of polling context.pages() every 200ms:
        // Polling makes ~20 CDP calls while Chrome is loading a heavy new tab.
        // Under that combined load, Chrome's DevTools HTTP server stops responding,
        // causing the NEXT connectOverCDP to time out with a 30-second error.
        // A passive sleep has zero CDP overhead and achieves the same goal.
        await new Promise(r => setTimeout(r, 2000));
    }} catch (e) {{
        console.error('CLICK_FAIL:' + e.message);
    }}
    // No browser.close() — avoid explicit Target.detachFromTarget.
    // Chrome handles the WebSocket close gracefully.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {"success": res.get("success", False) and "CLICK_OK" in res.get("output", ""), "method": "cdp_click", "output": "Clicked" if "CLICK_OK" in res.get("output", "") else res.get("output", "Unknown error")}

    # Desktop session — use Computer API
    try:
        result = await asyncio.to_thread(
            client._session.computer.click_mouse, x, y, button
        )
        success = getattr(result, 'success', False)
        logger.info(
            "[TakeControl] Computer click completed success={}",
            bool(success),
        )
        return {"success": success, "method": "computer_click", "output": f"Clicked at ({x}, {y})"}
    except Exception as exc:
        logger.warning(
            "[TakeControl] Computer click failed error_type={}",
            type(exc).__name__,
        )
        return {"success": False, "output": "Click operation failed"}




async def _perform_type(client, text: str):
    """Type text into the remote session.

    Browser sessions use CDP keyboard API; desktop sessions use computer.input_text.
    """
    image_type = getattr(client, '_image_type', 'unknown')
    logger.info(f"[TakeControl] Type text chars={len(text)} image_type={image_type}")

    if _is_browser_session(client):
        import urllib.parse
        encoded_text = urllib.parse.quote(text)
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];
        const textToType = decodeURIComponent('{encoded_text}');
        await page.keyboard.type(textToType);
        console.log('TYPE_OK');
        ok = true;
    }} catch (e) {{
        console.error('TYPE_FAIL:' + e.message);
    }}
    // No browser.close() — avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {"success": res.get("success", False) and "TYPE_OK" in res.get("output", ""), "method": "cdp_type", "output": "Text typed" if "TYPE_OK" in res.get("output", "") else res.get("output", "Unknown error")}

    try:
        result = await asyncio.to_thread(
            client._session.computer.input_text, text
        )
        success = getattr(result, 'success', False)
        logger.info("[TakeControl] Computer input_text success={}", bool(success))
        return {"success": success, "method": "computer_input", "output": "Text typed"}
    except Exception as exc:
        logger.warning(
            "[TakeControl] Computer input_text failed error_type={}",
            type(exc).__name__,
        )
        return {"success": False, "output": "Type operation failed"}




async def _perform_press_keys(client, keys: list[str]):
    """Press key combination on the remote session.

    Browser sessions use CDP keyboard API; desktop sessions use computer.press_keys.
    """
    key_desc = "+".join(keys)
    logger.info(f"[TakeControl] Press keys count={len(keys)}")

    if _is_browser_session(client):
        # Convert key names to the Playwright format (e.g. 'ctrl' → 'Control')
        key_map = {
            'ctrl': 'Control', 'alt': 'Alt', 'shift': 'Shift', 'meta': 'Meta',
            'enter': 'Enter', 'backspace': 'Backspace', 'esc': 'Escape', 'tab': 'Tab',
        }
        playwright_keys = [key_map.get(k.lower(), k.upper() if len(k) == 1 else k) for k in keys]
        combined = "+".join(playwright_keys)
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];
        await page.keyboard.press({json.dumps(combined)});
        console.log('PRESS_OK');
        ok = true;
    }} catch (e) {{
        console.error('PRESS_FAIL:' + e.message);
    }}
    // No browser.close() — avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {"success": res.get("success", False) and "PRESS_OK" in res.get("output", ""), "method": "cdp_press", "output": f"Pressed {key_desc}" if "PRESS_OK" in res.get("output", "") else res.get("output", "Unknown error")}

    try:
        result = await asyncio.to_thread(
            client._session.computer.press_keys, keys
        )
        success = getattr(result, 'success', False)
        logger.info("[TakeControl] Computer press_keys success={}", bool(success))
        return {"success": success, "method": "computer_keys", "output": f"Pressed {key_desc}"}
    except Exception as exc:
        logger.warning(
            "[TakeControl] Computer press_keys failed error_type={}",
            type(exc).__name__,
        )
        return {"success": False, "output": "Key press operation failed"}




async def _perform_drag(
    client, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 600
) -> dict:
    """Simulate a human-like mouse drag using a Bezier curve trajectory.

    Browser sessions use CDP to send precise mouse events with a Bezier
    curve trajectory and sub-pixel jitter for CAPTCHA bypass.
    Desktop sessions use the Computer API move_mouse sequence.
    All CDP scripts use browser.close() for graceful disconnect.
    """
    logger.info(
        f"[TakeControl] Drag: ({from_x},{from_y}) -> ({to_x},{to_y}), "
        f"duration={duration_ms}ms"
    )

    if _is_browser_session(client):
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
let browser;
(async () => {{
    let ok = false;
    try {{
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];

        const steps = 30;
        const duration = {duration_ms};
        const x0 = {from_x}, y0 = {from_y};
        const x3 = {to_x},  y3 = {to_y};
        const dx = x3 - x0, dy = y3 - y0;
        const perpX = -dy * 0.15, perpY = dx * 0.15;
        const x1 = x0 + dx * 0.3 + perpX, y1 = y0 + dy * 0.3 + perpY;
        const x2 = x0 + dx * 0.7 - perpX, y2 = y0 + dy * 0.7 - perpY;
        const bezier = (t) => {{
            const u = 1 - t;
            return {{ x: u*u*u*x0+3*u*u*t*x1+3*u*t*t*x2+t*t*t*x3, y: u*u*u*y0+3*u*u*t*y1+3*u*t*t*y2+t*t*t*y3 }};
        }};
        await page.mouse.move(x0, y0);
        await page.mouse.down();
        for (let i = 1; i <= steps; i++) {{
            const pt = bezier(i / steps);
            const jx = (Math.random() - 0.5) * 2;
            const jy = (Math.random() - 0.5) * 2;
            await page.mouse.move(Math.round(pt.x + jx), Math.round(pt.y + jy));
            await new Promise(r => setTimeout(r, duration / steps));
        }}
        await page.mouse.move(x3, y3);
        await page.mouse.up();
        console.log('TC_OK: drag complete');
        ok = true;
    }} catch (e) {{
        console.error('TC_FAIL: ' + e.message);
    }}
    // No browser.close() — avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {
            "success": res.get("success", False) and "TC_OK" in res.get("output", ""),
            "method": "cdp_drag",
            "output": f"Dragged ({from_x},{from_y}) -> ({to_x},{to_y})" if "TC_OK" in res.get("output", "") else res.get("output", "Unknown error"),
        }




# ── Endpoints ──


@router.post("/click")
async def control_click(
    agent_id: uuid.UUID,
    data: ClickRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Forward a mouse click to the AgentBay session.

    Requires the session to be in Take Control mode (locked).
    Returns {status: 'ok'|'error', detail: str} so the frontend knows if it worked.
    """
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    control_lock = await _require_control_lock_holder(
        agent_id, data.session_id, current_user.id
    )
    # Serialize interactions per-session: rapid clicks would otherwise overwrite
    # tc_action.js concurrently, causing the second script to read wrong content.
    try:
        async with _serialized_control_interaction(agent_id, data.session_id):
            client = await asyncio.wait_for(
                _get_client(
                    agent_id,
                    data.session_id,
                    env_type=control_lock.env_type,
                ),
                timeout=30,
            )
            result = await asyncio.wait_for(
                _perform_click(client, data.x, data.y, data.button),
                timeout=30,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[TakeControl] Click exception error_type={}",
            type(exc).__name__,
        )
        return {"status": "error", "detail": "Click operation failed"}
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    if result.get("success"):
        return {"status": "ok", "detail": f"Clicked at ({data.x}, {data.y})"}
    detail = result.get("stderr") or result.get("output") or "Click operation failed"
    return {"status": "error", "detail": detail[:500]}


@router.post("/type")
async def control_type(
    agent_id: uuid.UUID,
    data: TypeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Forward text input to the AgentBay session."""
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    control_lock = await _require_control_lock_holder(
        agent_id, data.session_id, current_user.id
    )
    try:
        async with _serialized_control_interaction(agent_id, data.session_id):
            client = await asyncio.wait_for(
                _get_client(
                    agent_id,
                    data.session_id,
                    env_type=control_lock.env_type,
                ),
                timeout=30,
            )
            result = await asyncio.wait_for(_perform_type(client, data.text), timeout=30)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[TakeControl] Type exception error_type={}",
            type(exc).__name__,
        )
        return {"status": "error", "detail": "Type operation failed"}
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    if result.get("success"):
        return {"status": "ok", "detail": "Text sent"}
    detail = result.get("stderr") or result.get("output") or "Type operation failed"
    return {"status": "error", "detail": detail[:500]}


@router.post("/press_keys")
async def control_press_keys(
    agent_id: uuid.UUID,
    data: PressKeysRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Forward keyboard key presses to the AgentBay session."""
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    control_lock = await _require_control_lock_holder(
        agent_id, data.session_id, current_user.id
    )
    try:
        async with _serialized_control_interaction(agent_id, data.session_id):
            client = await asyncio.wait_for(
                _get_client(
                    agent_id,
                    data.session_id,
                    env_type=control_lock.env_type,
                ),
                timeout=30,
            )
            result = await asyncio.wait_for(
                _perform_press_keys(client, data.keys),
                timeout=30,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[TakeControl] Press keys exception error_type={}",
            type(exc).__name__,
        )
        return {"status": "error", "detail": "Key press operation failed"}
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    if result.get("success"):
        return {"status": "ok", "detail": f"Pressed: {'+'.join(data.keys)}"}
    detail = result.get("stderr") or result.get("output") or "Key press failed"
    return {"status": "error", "detail": detail[:500]}


@router.post("/drag")
async def control_drag(
    agent_id: uuid.UUID,
    data: DragRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simulate a human-like mouse drag in the AgentBay session.

    Used for slider CAPTCHAs and drag-and-drop interactions.
    The drag follows a Bezier curve trajectory with random jitter to
    mimic natural mouse movement, which is required to bypass bot detection.
    """
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    control_lock = await _require_control_lock_holder(
        agent_id, data.session_id, current_user.id
    )
    try:
        async with _serialized_control_interaction(agent_id, data.session_id):
            client = await asyncio.wait_for(
                _get_client(
                    agent_id,
                    data.session_id,
                    env_type=control_lock.env_type,
                ),
                timeout=30,
            )
            result = await asyncio.wait_for(
                _perform_drag(
                    client,
                    data.from_x, data.from_y,
                    data.to_x, data.to_y,
                    data.duration_ms,
                ),
                timeout=35,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[TakeControl] Drag exception error_type={}",
            type(exc).__name__,
        )
        return {"status": "error", "detail": "Drag operation failed"}
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    if result.get("success"):
        return {"status": "ok", "detail": result.get("output", "Drag complete")}
    return {"status": "error", "detail": result.get("output", "Drag failed")[:500]}


@router.post("/screenshot")
async def control_screenshot(
    agent_id: uuid.UUID,
    data: ScreenshotRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get an immediate screenshot from the AgentBay session.

    Automatically detects the session type (browser/desktop) and uses
    the appropriate snapshot method. Returns a base64 data URI and
    the screen size for coordinate mapping.
    """
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    control_lock = await _require_control_lock_holder(
        agent_id, data.session_id, current_user.id
    )
    async def _capture(client):
        screenshot = await client.get_browser_snapshot_base64()
        if not screenshot:
            screenshot = await client.get_desktop_snapshot_base64()
        screen = None
        try:
            size_result = await asyncio.to_thread(
                client._session.computer.get_screen_size
            )
            if size_result.success and getattr(size_result, "data", None):
                screen = size_result.data
        except Exception:
            pass
        return screenshot, screen

    try:
        async with _serialized_control_interaction(agent_id, data.session_id):
            client = await asyncio.wait_for(
                _get_client(
                    agent_id,
                    data.session_id,
                    env_type=control_lock.env_type,
                ),
                timeout=30,
            )
            screenshot_b64, screen_size = await asyncio.wait_for(
                _capture(client),
                timeout=20,
            )
        if not screenshot_b64:
            logger.warning(f"[TakeControl] Screenshot returned None for agent={agent_id}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "[TakeControl] Screenshot failed error_type={}",
            type(exc).__name__,
        )
        return {"status": "error", "detail": "Screenshot operation failed"}
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    return {
        "status": "ok",
        "screenshot": screenshot_b64,
        "screen_size": screen_size,
    }


@router.post("/lock")
async def control_lock(
    agent_id: uuid.UUID,
    data: LockRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enter Take Control mode — locks the session against automatic tool execution.

    While locked, the agent's execute_tool will return a "waiting for human"
    message instead of executing browser/computer tools.
    """
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    # Allow any user with access (manage or use) — Take Control is part of
    # the normal interaction flow, not an admin-only operation.

    # Sanitize env_type — default to 'browser' if empty or unknown
    env_type = (data.env_type or "browser").lower()
    if env_type == "code":
        raise HTTPException(
            status_code=409,
            detail="Take Control for Code environments is disabled",
        )
    if env_type not in ("browser", "computer"):
        env_type = "browser"

    try:
        acquired, holder = await acquire_take_control_lock(
            agent_id,
            data.session_id,
            user_id=current_user.id,
            env_type=env_type,
        )
    except AgentBayAgentDeleting as exc:
        raise HTTPException(
            status_code=409,
            detail="Agent deletion is in progress",
        ) from exc
    except AgentBayToolExecutionActive as exc:
        raise HTTPException(
            status_code=423,
            detail="An AgentBay tool operation is still finishing",
        ) from exc
    except Exception as exc:
        logger.error(
            "[TakeControl] Shared lock acquire failed error_type={}",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Take Control lock service is unavailable",
        ) from exc
    if not acquired:
        raise HTTPException(
            status_code=423,
            detail="This session is controlled by another user",
        )
    try:
        await _require_owned_control_session(
            db,
            agent_id=agent_id,
            session_id=data.session_id,
            current_user=current_user,
        )
    except (Exception, asyncio.CancelledError):
        try:
            await release_take_control_lock(
                agent_id,
                data.session_id,
                user_id=current_user.id,
            )
        except Exception as exc:
            logger.error(
                "[TakeControl] Failed to release a post-auth rejected lock error_type={}",
                type(exc).__name__,
            )
        raise
    logger.info(
        f"[TakeControl] Lock acquired: agent={agent_id}, "
        f"user={current_user.id}, env_type={env_type}"
    )
    return {
        "status": "locked",
        "locked_by": holder.user_id if holder else str(current_user.id),
    }


@router.post("/unlock")
async def control_unlock(
    agent_id: uuid.UUID,
    data: UnlockRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Exit Take Control after bounded cleanup under the still-held lock.

    Cookie export is deliberately unavailable in this release. A client domain
    label is not a safe origin boundary and an unfiltered browser-context export
    could cross users or sites. The compatibility request fields are accepted,
    but no credential material is read or persisted.
    """
    await _require_owned_control_session(
        db, agent_id=agent_id, session_id=data.session_id, current_user=current_user
    )
    control_lock = await _require_control_lock_holder(
        agent_id, data.session_id, current_user.id
    )

    canonical_session_id = _canonical_chat_session_id(data.session_id)
    if canonical_session_id is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Cleanup and release are one serialized critical section. Releasing first
    # would let the Agent resume while stopLoading/about:blank is still running.
    async with _serialized_control_interaction(agent_id, data.session_id):
        if control_lock.env_type == "browser":
            # Attach only an existing durable lane; never allocate a sandbox from
            # an unlock request. A missing/uncertain provider session fails closed.
            await asyncio.wait_for(
                _get_client(
                    agent_id,
                    data.session_id,
                    env_type=control_lock.env_type,
                ),
                timeout=30,
            )
            try:
                cleanup_confirmed = await asyncio.wait_for(
                    _tc_browser_cleanup(agent_id, data.session_id),
                    timeout=20,
                )
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Take Control cleanup could not be verified",
                ) from exc
            if not cleanup_confirmed:
                raise HTTPException(
                    status_code=503,
                    detail="Take Control cleanup could not be verified",
                )

        # Reset both SDK and control-layer initialization state before the
        # human lock is released, so the next Agent action rebinds cleanly.
        from app.services.agentbay_client import _agentbay_sessions

        for image_type in ("browser", "browser_latest"):
            cache_key = (agent_id, canonical_session_id, image_type)
            cached = _agentbay_sessions.get(cache_key)
            if cached is not None:
                cached[0]._browser_initialized = False
        _browser_initialized.discard((agent_id, canonical_session_id, "browser"))
        _browser_initialized.discard(
            (agent_id, canonical_session_id, "browser_latest")
        )

        # The cleanup result is returned only while the same owner lane remains
        # active. This is a short transaction and never spans provider I/O.
        await _require_owned_control_session(
            db,
            agent_id=agent_id,
            session_id=data.session_id,
            current_user=current_user,
        )

        try:
            await release_take_control_lock(
                agent_id,
                data.session_id,
                user_id=current_user.id,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=423,
                detail="This session is controlled by another user",
            ) from exc
        except Exception as exc:
            logger.error(
                "[TakeControl] Shared lock release failed error_type={}",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Take Control lock service is unavailable",
            ) from exc
        logger.info("[TakeControl] Lock released agent={}", agent_id)

    # Release authority locks before serializing the response.
    await db.rollback()
    if data.export_cookies:
        logger.info(
            "[TakeControl] Cookie export request ignored because the feature is disabled"
        )

    return {
        "status": "unlocked",
        "cookies_exported": False,
        "cookie_count": 0,
        "cookie_export_disabled": True,
    }
