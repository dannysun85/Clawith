"""AgentBay API client using official SDK.

This module provides a client wrapper around the official AgentBay SDK
for browser and code execution operations.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agentbay import AgentBay, Config, CreateSessionParams
from loguru import logger
from pydantic import RootModel

from app.core.logging_config import _disable_agentbay_logger_override, configure_logging


class GenericExtractSchema(RootModel[Any]):
    pass


AGENTBAY_SDK_TIMEOUT_MS = 30_000
_AGENTBAY_MANAGER_CREATION_TOKEN = object()


_disable_agentbay_logger_override()
configure_logging()


@dataclass
class AgentBaySession:
    """AgentBay session info."""
    session_id: str
    image: str
    created_at: datetime
    expires_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class _AgentBayLaneSnapshot:
    id: uuid.UUID
    provider_session_id: str | None
    image_type: str
    chat_session_id: str | None


class AgentBayClient:
    """Client for AgentBay SDK interactions."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._sdk = AgentBay(
            api_key=api_key,
            cfg=Config(timeout_ms=AGENTBAY_SDK_TIMEOUT_MS),
        )
        self._session = None
        self._image_type = None

    def _require_bound_session(
        self,
        operation: str,
        *,
        allowed_image_types: tuple[str, ...] | None = None,
    ) -> None:
        """Require the durable manager to have attached the exact sandbox.

        Operation methods are intentionally unable to allocate provider
        resources. Creation belongs to the tenant/user/chat-lane manager, which
        owns the Redis fence and durable ledger write.
        """

        if self._session is None:
            raise RuntimeError(
                f"AgentBay {operation} requires an existing managed session"
            )
        if allowed_image_types and getattr(self, "_image_type", None) not in allowed_image_types:
            raise RuntimeError(
                f"AgentBay {operation} is not available for this managed session type"
            )

    async def create_session(
        self,
        image: str = "linux_latest",
        *,
        _manager_token: object | None = None,
    ) -> AgentBaySession:
        """Create a new session using SDK.

        Closes any existing session first to prevent leaked sessions
        on the AgentBay API side.
        """
        if _manager_token is not _AGENTBAY_MANAGER_CREATION_TOKEN:
            raise RuntimeError(
                "AgentBay sessions can only be created by the durable lane manager"
            )

        # Close existing session to prevent leaking concurrent sessions
        if self._session:
            logger.info("[AgentBay] Closing existing session before creating new one")
            await self.delete_session_strict()

        image_id_map = {
            "browser_latest": "browser_latest",
            "code_latest": "linux_latest",
            "linux_latest": "linux_latest",
            "windows_latest": "windows_latest",
        }
        image_id = image_id_map.get(image, image)
        self._image_type = image

        result = await asyncio.to_thread(self._sdk.create, CreateSessionParams(image_id=image_id))
        if not result.success:
            raise RuntimeError("AgentBay provider rejected session creation")

        self._session = result.session
        self._browser_initialized = False
        logger.info("[AgentBay] Created session")
        return AgentBaySession(
            session_id=self._session.session_id,
            image=image,
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
        )

    async def attach_session(self, provider_session_id: str, image: str) -> None:
        """Attach this process to an existing provider session by exact ID."""

        result = await asyncio.to_thread(self._sdk.get, provider_session_id)
        if not result.success or not result.session:
            raise RuntimeError("AgentBay provider session is unavailable")
        self._session = result.session
        self._image_type = image
        self._browser_initialized = False

    async def delete_session_strict(self) -> str | None:
        """Delete the provider sandbox, retaining its identity on ambiguity.

        Callers that use deletion as a durability/security boundary must use
        this method.  A provider failure leaves ``_session`` attached so the
        exact provider session id remains available for operator cleanup.
        """
        if not self._session:
            return None
        provider_session_id = str(self._session.session_id)
        try:
            result = await asyncio.to_thread(self._session.delete)
            if getattr(result, "success", True) is False:
                raise RuntimeError("AgentBay provider did not confirm session deletion")
        except BaseException as exc:
            logger.warning(
                "[AgentBay] Provider-session deletion unconfirmed error_type={}",
                type(exc).__name__,
            )
            raise
        else:
            self._session = None
            self._browser_initialized = False
            logger.info("[AgentBay] Closed session")
            return provider_session_id

    async def close_session(self) -> bool:
        """Best-effort compatibility cleanup; return whether deletion was proven."""

        if not self._session:
            return True
        try:
            await self.delete_session_strict()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    # ─── Browser Operations ──────────────────────────

    async def _ensure_browser_initialized(self):
        """Ensure the browser is initialized for the current session."""
        self._require_bound_session(
            "browser operation",
            allowed_image_types=("browser", "browser_latest"),
        )
        if not getattr(self, "_browser_initialized", False):
            from agentbay import BrowserOption
            from agentbay._common.models.browser import BrowserViewport, BrowserScreen
            
            # Use high-res viewport for clearer screenshots and better layout
            options = BrowserOption(
                viewport=BrowserViewport(width=1920, height=1080),
                screen=BrowserScreen(width=1920, height=1080)
            )
            success = await asyncio.to_thread(self._session.browser.initialize, options)
            if success is False:
                raise RuntimeError("SDK failed to initialize browser (returned False).")
            self._browser_initialized = True

    async def browser_navigate(self, url: str, wait_for: str = "", screenshot: bool = False) -> dict:
        """Navigate browser to URL using SDK.

        The AgentBay SDK default navigation timeout is ~60 s. We wrap the call
        with a 40-second asyncio soft-timeout so callers receive an actionable
        error quickly rather than hanging the whole agent loop. The underlying
        SDK thread may continue briefly in the background but its result is
        discarded — the browser will eventually settle on its own.
        """
        self._require_bound_session(
            "browser navigation",
            allowed_image_types=("browser", "browser_latest"),
        )
        await self._ensure_browser_initialized()

        # Navigate to URL with a 40-second soft timeout.
        # asyncio.wait_for cancels the coroutine wrapper; the blocking thread
        # inside asyncio.to_thread keeps running until SDK returns, but we
        # no longer block the agent loop waiting for it.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._session.browser.operator.navigate, url),
                timeout=40.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[AgentBay] navigate timed out after 40 s")
            raise RuntimeError(
                f"Navigation to '{url}' timed out (>40 s). "
                "The browser may be busy or the page is unreachable. "
                "Try calling agentbay_browser_screenshot to check the current "
                "state, or retry the navigation."
            )

        result = {"url": url, "success": True, "title": url}

        if screenshot:
            # Wait for dynamic content and SPA rendering (React/Vue) before screenshotting
            await asyncio.sleep(3)
            screenshot_data = await asyncio.to_thread(
                self._session.browser.operator.screenshot, full_page=False
            )
            result["screenshot"] = screenshot_data

        return result

    async def browser_screenshot(self) -> dict:
        """Take a screenshot of the current browser page without navigating.

        Use this after actions (click, type, form submit) to verify results
        without refreshing the page. Never call browser_navigate just to screenshot.
        """
        await self._ensure_browser_initialized()
        
        # Wait for dynamic content and SPA rendering before screenshotting
        await asyncio.sleep(3)
        
        screenshot_data = await asyncio.to_thread(
            self._session.browser.operator.screenshot, full_page=False
        )
        return {"success": True, "screenshot": screenshot_data}


    async def browser_click(self, selector: str) -> dict:
        """Click element by CSS selector using SDK."""
        await self._ensure_browser_initialized()

        from agentbay import ActOptions
        await asyncio.to_thread(self._session.browser.operator.act, ActOptions(action=f"click on {selector}"))
        return {"success": True, "selector": selector}

    async def browser_type(self, selector: str, text: str) -> dict:
        """Type text into element using SDK."""
        await self._ensure_browser_initialized()

        from agentbay import ActOptions

        # Detect OTP/PIN-style inputs: short digit-only strings (4-8 chars)
        # These use segmented input boxes that auto-advance focus per digit,
        # so character-by-character typing often fails. Use paste strategy instead.
        is_otp = text.isdigit() and 4 <= len(text) <= 8

        if is_otp:
            action_msg = (
                f"The text '{text}' appears to be a verification/OTP code. "
                f"Find the verification code input area near '{selector}'. "
                f"Click on the first input box, then paste or type the full code '{text}'. "
                f"If the input is split into individual digit boxes, click the first box "
                f"and type each digit one at a time: {', '.join(text)}. "
                f"Each box should auto-advance to the next after entering a digit."
            )
        else:
            # Standard input: click to focus, then type character by character
            # to correctly trigger React/Vue input events.
            action_msg = (
                f"Click on the element matching '{selector}' to focus it, "
                f"then use the keyboard to type the text '{text}' character by character. "
                f"This ensures modern web frameworks like React register the input."
            )

        await asyncio.to_thread(self._session.browser.operator.act, ActOptions(action=action_msg))
        return {"success": True, "selector": selector, "text": text}

    async def browser_login(self, url: str, login_config: str) -> dict:
        """Perform an automated login using AgentBay's built-in login skill.

        This leverages AgentBay's AI-driven login capability to handle complex
        login flows including CAPTCHAs, OTP inputs, and multi-step authentication.

        Args:
            url: The login page URL to navigate to first.
            login_config: JSON string with login configuration, e.g.
                          '{"api_key": "xxx", "skill_id": "yyy"}'
        """
        self._require_bound_session(
            "browser login",
            allowed_image_types=("browser", "browser_latest"),
        )
        await self._ensure_browser_initialized()

        # Navigate to the login page first
        await asyncio.to_thread(self._session.browser.operator.navigate, url)

        # Execute the login skill
        result = await asyncio.to_thread(
            self._session.browser.operator.login,
            login_config,
            use_vision=True,
        )
        return {
            "success": result.success,
            "message": result.message or "",
        }

    # ─── Code Operations ──────────────────────────

    async def code_execute(self, language: str, code: str, timeout: int = 30) -> dict:
        """Execute code in code space using SDK."""
        lang_map = {
            "python": "python",
            "bash": "bash",
            "shell": "bash",
            "node": "node",
            "javascript": "node",
        }
        sdk_lang = lang_map.get(language.lower(), "python")

        self._require_bound_session(
            "code execution",
            allowed_image_types=("code", "code_latest"),
        )

        result = await asyncio.to_thread(self._session.code.run_code, code, sdk_lang)

        return {
            "stdout": result.result if result.success else "",
            "stderr": result.error_message if not result.success else "",
            "exit_code": 0 if result.success else 1,
            "success": result.success,
        }

    # ─── Browser: Extract & Observe ───────────────────

    async def browser_extract(self, instruction: str, selector: str = "") -> dict:
        """Extract structured data from current page using natural language instruction."""
        await self._ensure_browser_initialized()
        
        # Wait for dynamic content and SPA rendering before extracting
        await asyncio.sleep(3)

        from agentbay._common.models.browser_operator import ExtractOptions
        # Use a generic RootModel schema since we cannot define a custom Pydantic model at runtime
        options = ExtractOptions(
            instruction=instruction,
            schema=GenericExtractSchema,
            selector=selector or None,
        )
        success, data = await asyncio.to_thread(
            self._session.browser.operator.extract, options
        )
        if success and data:
            if hasattr(data, "model_dump"):
                data = data.model_dump()
        return {"success": success, "data": data}

    async def browser_observe(self, instruction: str, selector: str = "") -> dict:
        """Observe the current page state and return interactive elements."""
        await self._ensure_browser_initialized()
        
        # Wait for dynamic content and SPA rendering before observing
        await asyncio.sleep(3)

        from agentbay._common.models.browser_operator import ObserveOptions
        options = ObserveOptions(
            instruction=instruction,
            selector=selector or None,
        )
        success, results = await asyncio.to_thread(
            self._session.browser.operator.observe, options
        )
        # Convert ObserveResult objects to dicts for serialization
        result_dicts = []
        for r in (results or []):
            result_dicts.append(vars(r) if hasattr(r, "__dict__") else str(r))
        return {"success": success, "elements": result_dicts}

    # ─── Command (Shell) Operations ──────────────────

    async def command_exec(self, command: str, timeout_ms: int = 50000, cwd: str = "") -> dict:
        """Execute a shell command in the AgentBay environment."""
        self._require_bound_session("command execution")

        result = await asyncio.to_thread(
            self._session.command.exec,
            command,
            timeout_ms=timeout_ms,
            cwd=cwd or None,
        )
        return {
            "success": result.success,
            "stdout": getattr(result, "stdout", "") or getattr(result, "output", "") or "",
            "stderr": getattr(result, "stderr", "") or "",
            "exit_code": getattr(result, "exit_code", -1),
            "error_message": "" if result.success else "AgentBay command execution failed",
        }

    # ─── Computer Operations ──────────────────────────

    async def _ensure_computer_session(self):
        """Ensure a computer (linux or windows desktop) session is active."""
        self._require_bound_session(
            "computer operation",
            allowed_image_types=("computer", "linux_latest", "windows_latest"),
        )

    async def computer_screenshot(self) -> dict:
        """Take a screenshot of the desktop.

        Tries the standard screenshot() API first, then falls back to
        beta_take_screenshot() for cloud environments that don't support
        the standard API yet.
        """
        await self._ensure_computer_session()
        
        # Wait briefly for UI animations/rendering to settle
        await asyncio.sleep(2)

        try:
            result = await asyncio.to_thread(self._session.computer.screenshot)
            # Some cloud environments return success=False with a message
            # telling us to use beta_take_screenshot() instead of throwing.
            if not result.success and "beta_take_screenshot" in (result.error_message or ""):
                logger.info("[AgentBay] screenshot() unsupported, falling back to beta_take_screenshot()")
                result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
        except Exception as e:
            # Also handle the case where it raises an exception
            if "beta_take_screenshot" in str(e):
                logger.info("[AgentBay] Falling back to beta_take_screenshot() after exception")
                result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
            else:
                raise
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": "" if result.success else "AgentBay screenshot failed",
        }

    async def computer_click(self, x: int, y: int, button: str = "left") -> dict:
        """Click the mouse at coordinates (x, y)."""
        await self._ensure_computer_session()
        move_result = await asyncio.to_thread(self._session.computer.move_mouse, x, y)
        result = await asyncio.to_thread(self._session.computer.click_mouse, x, y, button)
        return {
            "success": result.success,
            "moved": getattr(move_result, "success", False),
            "x": x,
            "y": y,
            "button": button,
        }

    async def computer_input_text(self, text: str) -> dict:
        """Input text at the current cursor position."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.input_text, text)
        return {"success": result.success, "text": text}

    async def computer_press_keys(self, keys: list, hold: bool = False) -> dict:
        """Press keyboard keys (e.g. ['ctrl', 'c'] for Ctrl+C)."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.press_keys, keys, hold=hold)
        return {"success": result.success, "keys": keys, "hold": hold}

    async def computer_scroll(self, x: int, y: int, direction: str = "down", amount: int = 1) -> dict:
        """Scroll the screen at position (x, y)."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.scroll, x, y, direction=direction, amount=amount
        )
        return {"success": result.success, "direction": direction, "amount": amount}

    async def computer_move_mouse(self, x: int, y: int) -> dict:
        """Move mouse to coordinates (x, y) without clicking."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.move_mouse, x, y)
        return {"success": result.success, "x": x, "y": y}

    async def computer_drag_mouse(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left"
    ) -> dict:
        """Drag mouse from (from_x, from_y) to (to_x, to_y)."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.drag_mouse, from_x, from_y, to_x, to_y, button=button
        )
        return {"success": result.success, "from": [from_x, from_y], "to": [to_x, to_y]}

    async def computer_get_screen_size(self) -> dict:
        """Get the screen resolution."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.get_screen_size)
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": "" if result.success else "AgentBay screen-size lookup failed",
        }

    async def computer_start_app(self, cmd: str, work_dir: str = "") -> dict:
        """Start an application by its command."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.start_app, cmd, work_directory=work_dir
        )
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": "" if result.success else "AgentBay application start failed",
        }

    async def computer_get_installed_apps(
        self,
        start_menu: bool = True,
        desktop: bool = True,
        ignore_system_apps: bool = True,
    ) -> dict:
        """List installed applications and their launch commands."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(
            self._session.computer.get_installed_apps,
            start_menu,
            desktop,
            ignore_system_apps,
        )
        apps = []
        for app in (getattr(result, "data", None) or []):
            apps.append(vars(app) if hasattr(app, "__dict__") else str(app))
        return {
            "success": result.success,
            "apps": apps,
            "error_message": "" if result.success else "AgentBay application list failed",
        }

    async def computer_get_cursor_position(self) -> dict:
        """Get current cursor position."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.get_cursor_position)
        return {
            "success": result.success,
            "data": getattr(result, "data", None),
            "error_message": "" if result.success else "AgentBay cursor lookup failed",
        }

    async def computer_get_active_window(self) -> dict:
        """Get info about the currently active window."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.get_active_window)
        window = getattr(result, "window", None)
        return {
            "success": result.success,
            "window": vars(window) if window and hasattr(window, "__dict__") else str(window),
            "error_message": "" if result.success else "AgentBay active-window lookup failed",
        }

    async def computer_list_windows(self, timeout_ms: int = 3000) -> dict:
        """List root desktop windows with IDs and geometry."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.list_root_windows, timeout_ms)
        windows = []
        for window in (getattr(result, "windows", None) or []):
            windows.append(vars(window) if hasattr(window, "__dict__") else str(window))
        return {
            "success": result.success,
            "windows": windows,
            "error_message": "" if result.success else "AgentBay window list failed",
        }

    async def computer_activate_window(self, window_id: int) -> dict:
        """Activate (bring to front) a window by its ID."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.activate_window, window_id)
        return {"success": result.success, "window_id": window_id}

    async def computer_close_window(self, window_id: int) -> dict:
        """Close a desktop window by its ID."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.close_window, window_id)
        return {
            "success": result.success,
            "window_id": window_id,
            "error_message": "" if result.success else "AgentBay close-window failed",
        }

    async def computer_list_visible_apps(self) -> dict:
        """List currently visible/running applications."""
        await self._ensure_computer_session()
        result = await asyncio.to_thread(self._session.computer.list_visible_apps)
        data = getattr(result, "data", [])
        # Convert process objects to dicts
        apps = []
        for p in (data or []):
            apps.append(vars(p) if hasattr(p, "__dict__") else str(p))
        return {
            "success": result.success,
            "apps": apps,
            "error_message": "" if result.success else "AgentBay visible-application list failed",
        }

    # ─── Live Preview Support ──────────────────────────

    async def get_live_url(self) -> str | None:
        """Get the VNC/viewer URL for the current computer session.

        Calls session.get_link() which returns a shareable viewer URL
        for the cloud desktop. Returns None if no session is active
        or the API call fails.
        """
        if not self._session:
            return None
        try:
            result = await asyncio.to_thread(self._session.get_link)
            if result.success and result.data:
                logger.info("[AgentBay] Got live URL")
                return result.data
            logger.warning("[AgentBay] get_link() failed")
            return None
        except Exception as exc:
            logger.warning(
                "[AgentBay] Failed to get live URL error_type={}",
                type(exc).__name__,
            )
            return None

    async def get_desktop_snapshot_base64(self) -> str | None:
        """Take a quick desktop screenshot and return compressed base64 JPEG.

        Used for live preview panel. Calls the same screenshot API as
        computer_screenshot() but without the sleep delay, and compresses
        the result for efficient WebSocket transfer.
        Returns data:image/jpeg;base64,... or None on failure.
        """
        if not self._session:
            return None
        try:
            # Use the same screenshot logic as computer_screenshot()
            try:
                result = await asyncio.to_thread(self._session.computer.screenshot)
                if not result.success and "beta_take_screenshot" in (result.error_message or ""):
                    result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
            except Exception as e:
                if "beta_take_screenshot" in str(e):
                    result = await asyncio.to_thread(self._session.computer.beta_take_screenshot)
                else:
                    raise

            screenshot_data = getattr(result, "data", None)
            if not screenshot_data:
                return None

            # Compress to JPEG base64 for live preview
            import base64
            from io import BytesIO
            from PIL import Image

            img = Image.open(BytesIO(screenshot_data))
            # Resize to max 1920px wide for live preview (up from 1280px to preserve details)
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as exc:
            logger.warning(
                "[AgentBay] Desktop snapshot failed error_type={}",
                type(exc).__name__,
            )
            return None

    async def get_browser_snapshot_base64(self) -> str | None:
        """Take a quick browser screenshot and return compressed base64 JPEG.

        Used for live preview panel — no wait/sleep since we want
        the snapshot to reflect the current state immediately.
        Returns data:image/jpeg;base64,... or None on failure.
        """
        if not self._session:
            logger.info("[AgentBay] Browser snapshot skipped: No active session")
            return None
        if not getattr(self, "_browser_initialized", False):
            logger.info("[AgentBay] Browser snapshot skipped: Browser not initialized")
            return None
        
        try:
            screenshot_data = await asyncio.to_thread(
                self._session.browser.operator.screenshot, full_page=False
            )
            if not screenshot_data:
                logger.info("[AgentBay] Browser snapshot returned empty data")
                return None

            # Compress screenshot to JPEG base64 for efficient transfer
            import base64
            from io import BytesIO
            from PIL import Image

            if isinstance(screenshot_data, str):
                # The AgentBay SDK may return a raw base64 string without proper
                # padding. Normalize by stripping whitespace and adding padding chars.
                screenshot_data = screenshot_data.strip()
                # Remove data URI prefix if present (e.g., "data:image/png;base64,")
                if "," in screenshot_data:
                    screenshot_data = screenshot_data.split(",", 1)[1]
                # Add base64 padding if missing
                missing_padding = len(screenshot_data) % 4
                if missing_padding:
                    screenshot_data += "=" * (4 - missing_padding)
                screenshot_data = base64.b64decode(screenshot_data)


            img = Image.open(BytesIO(screenshot_data))
            # Resize to max 1920px wide for live preview (up from 1280px to preserve details)
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as exc:
            logger.warning(
                "[AgentBay] Browser snapshot failed error_type={}",
                type(exc).__name__,
            )
            return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()


# ─── Session Cache for Tool Executions ──────────────────────────
# Key: (agent_id, session_id, image_type) so each ChatSession gets
# its own independent AgentBay instance for browser/computer/code.
# Previously keyed by (agent_id, image_type) which meant all users
# of the same Agent shared one browser/desktop — causing conflicts.

_agentbay_sessions: dict[tuple[uuid.UUID, str, str], tuple[AgentBayClient, datetime]] = {}
_AGENTBAY_SESSION_TIMEOUT = timedelta(minutes=5)

_SESSION_CREATE_LOCK_TTL_SECONDS = 180
_AGENT_DELETION_LOCK_TTL_SECONDS = 1800
_SESSION_CREATE_ACQUIRE_LUA = """
if redis.call('exists', KEYS[2]) == 1 then
  return -1
end
if redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX') then
  return 1
end
return 0
"""
_SESSION_CREATE_REFRESH_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_SESSION_CREATE_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


AGENTBAY_API_URL = "https://api.agentbay.ai/v1"


def _is_plausible_agentbay_api_key(value: str | None) -> bool:
    """AgentBay API keys use an akm-* token format.

    This keeps encrypted blobs that failed to decrypt from being treated as
    plaintext keys and sent to AgentBay, where they surface as
    "invalid apiKey or token".
    """
    return bool(isinstance(value, str) and value.strip().startswith("akm-"))


def _canonical_chat_session_id(session_id: object) -> str | None:
    """Return one textual identity for every representation of a UUID."""

    try:
        return str(uuid.UUID(str(session_id)))
    except (TypeError, ValueError):
        return None


async def get_agentbay_api_key_for_agent(agent_id: uuid.UUID, db=None) -> Optional[str]:
    """Return the configured AgentBay API key for the given agent.

    Resolution order:
    1. Per-agent ChannelConfig (channel_type='agentbay') — set via Agent detail page
    2. Global Tool.config.api_key (category='agentbay') — set via Company Settings
    """
    from app.models.channel_config import ChannelConfig
    from app.models.tool import Tool
    from sqlalchemy import select
    from app.database import async_session
    from app.core.security import decrypt_data
    from app.config import get_settings

    async def _fetch(session):
        # 1) Check per-agent ChannelConfig first (highest priority)
        result = await session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "agentbay",
                ChannelConfig.is_configured,
            )
        )
        config = result.scalar_one_or_none()
        if config and config.app_secret:
            # Try to decrypt, fallback to plaintext if it fails
            try:
                candidate = decrypt_data(config.app_secret, get_settings().SECRET_KEY)
            except Exception:
                candidate = config.app_secret
            if _is_plausible_agentbay_api_key(candidate):
                return candidate

        # 2) Fallback: check global Tool.config.api_key for agentbay tools.
        #
        # Only agentbay_browser_navigate (the "primary" AgentBay tool) has a
        # config_schema with an api_key field, so it is the only tool whose
        # config is ever populated with a key via the Company Settings UI.
        # We therefore query it first, then fall back to scanning all agentbay
        # tools — this prevents a non-deterministic .limit(1) from returning a
        # tool with an empty config (e.g. agentbay_computer_screenshot), which
        # would silently return None even when a key IS configured.
        candidate_tools: list[Tool] = []
        tool_result = await session.execute(
            select(Tool).where(
                Tool.name == "agentbay_browser_navigate",
                Tool.enabled,
            ).limit(1)
        )
        tool = tool_result.scalar_one_or_none()
        if tool:
            candidate_tools.append(tool)

        # Also scan all agentbay tools in case the key was stored on a
        # different category representative by an older UI.
        all_result = await session.execute(
            select(Tool).where(
                Tool.category == "agentbay",
                Tool.enabled,
            ).order_by(Tool.name)
        )
        candidate_tools.extend(
            candidate
            for candidate in all_result.scalars().all()
            if not tool or candidate.id != tool.id
        )

        for candidate_tool in candidate_tools:
            if not (candidate_tool.config and candidate_tool.config.get("api_key")):
                continue
            api_key = candidate_tool.config["api_key"]
            try:
                candidate = decrypt_data(api_key, get_settings().SECRET_KEY)
            except Exception:
                candidate = api_key
            if _is_plausible_agentbay_api_key(candidate):
                return candidate

        return None

    if db:
        return await _fetch(db)
    async with async_session() as session:
        return await _fetch(session)


async def test_agentbay_channel(agent_id: uuid.UUID, current_user, db) -> dict:
    """Validate effective configuration without creating billable provider sessions."""
    key = await get_agentbay_api_key_for_agent(agent_id, db)
    if not key:
        return {"ok": False, "error": "AgentBay not configured"}

    from app.services.agent_tools import _get_tool_config

    tool_config = await _get_tool_config(agent_id, "agentbay_browser_navigate")
    os_type = (tool_config or {}).get("os_type", "windows")
    computer_image = "windows_latest" if os_type == "windows" else "linux_latest"
    capabilities = {
        "browser": {
            "configured": True,
            "runtime_tested": False,
            "image": "browser_latest",
            "status": "configuration_validated",
        },
        "computer": {
            "configured": True,
            "runtime_tested": False,
            "image": computer_image,
            "status": "configuration_validated",
        },
        "code": {
            "configured": True,
            "runtime_tested": False,
            "enabled": False,
            "image": "linux_latest",
            "status": "separate_production_authorization_required",
            "reason": (
                "Code is disabled in the normal production release and requires "
                "a separate platform-authorized activation workflow"
            ),
        },
    }
    return {
        "ok": True,
        "runtime_tested": False,
        "message": (
            "✅ AgentBay credential is configured and locally well-formed. "
            "Remote authorization and runtime were not tested; no provider session was "
            "created. Code requires separate production authorization."
        ),
        "capabilities": capabilities,
    }


async def _configured_agentbay_client(
    agent_id: uuid.UUID,
) -> tuple[AgentBayClient, dict | None]:
    """Build an AgentBay client from the effective, validated credential."""

    from app.services.agent_tools import _get_tool_config

    tool_config = await _get_tool_config(agent_id, "agentbay_browser_navigate")
    api_key = None
    if tool_config and tool_config.get("api_key"):
        api_key = tool_config.get("api_key")
        from app.config import get_settings
        from app.core.security import decrypt_data

        try:
            api_key = decrypt_data(api_key, get_settings().SECRET_KEY)
        except Exception:
            pass
        if not _is_plausible_agentbay_api_key(api_key):
            api_key = None

    if not api_key:
        api_key = await get_agentbay_api_key_for_agent(agent_id)
    if not api_key:
        raise RuntimeError(
            "AgentBay not configured for this agent. Please configure in Tools > AgentBay."
        )
    return AgentBayClient(api_key), tool_config


async def _load_agentbay_lane(
    agent_id: uuid.UUID,
    session_id: str,
) -> tuple[uuid.UUID | None, uuid.UUID, str] | None:
    """Resolve and authorize an exact durable user/chat lane."""

    canonical_session_id = _canonical_chat_session_id(session_id)
    if canonical_session_id is None:
        return None
    chat_session_id = uuid.UUID(canonical_session_id)

    from sqlalchemy import or_, select

    from app.core.permissions import (
        evaluate_agent_relationship_status,
        get_agent_access_level_for_user_id,
        is_agent_expired,
    )
    from app.database import async_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.org import AgentAgentRelationship
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_session_access import validate_active_user_chat_lane

    async with async_session() as db:
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == chat_session_id,
                or_(
                    ChatSession.agent_id == agent_id,
                    (
                        (ChatSession.source_channel == "agent")
                        & (ChatSession.peer_agent_id == agent_id)
                    ),
                ),
            )
        )
        chat_session = session_result.scalar_one_or_none()
        if chat_session is None:
            raise RuntimeError("AgentBay chat session is not available")
        if chat_session.user_id is None:
            raise PermissionError("AgentBay chat session has no owner")
        owner = await db.get(User, chat_session.user_id)
        if owner is None or not owner.is_active:
            raise PermissionError("AgentBay chat-session owner is unavailable")

        if chat_session.source_channel != "agent":
            lane = await validate_active_user_chat_lane(
                db,
                agent_id=agent_id,
                owner_user_id=chat_session.user_id,
                session_id=chat_session.id,
            )
            return lane.agent.tenant_id, lane.owner.id, canonical_session_id

        participant_ids = {chat_session.agent_id, chat_session.peer_agent_id}
        if None in participant_ids or agent_id not in participant_ids:
            raise PermissionError("AgentBay A2A session participants are invalid")
        agents = list(
            (
                await db.execute(
                    select(Agent).where(Agent.id.in_(participant_ids))
                )
            ).scalars().all()
        )
        if len(agents) != 2:
            raise PermissionError("AgentBay A2A Agent is unavailable")
        if any(
            agent.status not in {"running", "idle"} or is_agent_expired(agent)
            for agent in agents
        ):
            raise PermissionError("AgentBay A2A Agent is unavailable")
        tenant_ids = {agent.tenant_id for agent in agents}
        if len(tenant_ids) != 1 or None in tenant_ids:
            raise PermissionError("AgentBay A2A company boundary is invalid")
        tenant_id = next(iter(tenant_ids))
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None or not tenant.is_active:
            raise PermissionError("AgentBay company is inactive")
        if owner.tenant_id != tenant_id:
            raise PermissionError("AgentBay A2A owner tenant is invalid")
        for participant in agents:
            if not await get_agent_access_level_for_user_id(
                db,
                owner.id,
                participant,
            ):
                raise PermissionError(
                    "AgentBay A2A owner no longer has Agent access"
                )

        first_id, second_id = tuple(participant_ids)
        relationships = list(
            (
                await db.execute(
                    select(AgentAgentRelationship).where(
                        or_(
                            (
                                (AgentAgentRelationship.agent_id == first_id)
                                & (
                                    AgentAgentRelationship.target_agent_id
                                    == second_id
                                )
                            ),
                            (
                                (AgentAgentRelationship.agent_id == second_id)
                                & (
                                    AgentAgentRelationship.target_agent_id
                                    == first_id
                                )
                            ),
                        )
                    )
                )
            ).scalars().all()
        )
        active_relationship = False
        for relationship in relationships:
            status = await evaluate_agent_relationship_status(
                db,
                relationship,
                current_user_id=owner.id,
            )
            if status.get("access_status") == "active":
                active_relationship = True
                break
        if not active_relationship:
            raise PermissionError("AgentBay A2A relationship is inactive")
        return tenant_id, owner.id, canonical_session_id


async def _get_active_agentbay_ledger(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    image_type: str,
):
    """Return the sole active ledger row, quarantining historical duplicates."""

    from sqlalchemy import and_, or_, select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        result = await db.execute(
            select(AgentBaySessionLedger)
            .where(
                AgentBaySessionLedger.agent_id == agent_id,
                AgentBaySessionLedger.image_type == image_type,
                or_(
                    AgentBaySessionLedger.status == "cleanup_required",
                    and_(
                        AgentBaySessionLedger.status == "active",
                        AgentBaySessionLedger.user_id == user_id,
                        AgentBaySessionLedger.chat_session_id == str(session_id),
                    ),
                ),
            )
            .order_by(
                AgentBaySessionLedger.last_used_at.desc().nullslast(),
                AgentBaySessionLedger.started_at.desc(),
                AgentBaySessionLedger.id,
            )
        )
        rows = list(result.scalars().all())
        if not rows:
            return None
        now = datetime.now(timezone.utc)
        relevant_rows = []
        ledger_changed = False
        for row in rows:
            context = row.context if isinstance(row.context, dict) else {}
            if row.status == "cleanup_required":
                # A v2 row has an exact owner/chat binding, so its ambiguous
                # deletion poisons only that lane. Legacy/untrusted rows lack
                # enough identity to narrow safely and remain Agent-wide.
                if context.get("binding_version") != 2 or (
                    row.user_id == user_id
                    and row.chat_session_id == str(session_id)
                ):
                    relevant_rows.append(row)
            elif context.get("binding_version") != 2:
                row.status = "cleanup_required"
                row.close_reason = "untrusted_legacy_binding"
                row.error_message = "Operator provider cleanup required"
                relevant_rows.append(row)
                ledger_changed = True
            else:
                relevant_rows.append(row)
        if ledger_changed:
            await db.commit()
        rows = relevant_rows
        if not rows:
            return None
        cleanup_rows = [row for row in rows if row.status == "cleanup_required"]
        if cleanup_rows:
            # Never create/attach another sandbox while an unconfirmed provider
            # deletion remains associated with this exact user/chat lane.
            return cleanup_rows[0]
        rows = [row for row in rows if row.status == "active"]
        if not rows:
            return None
        keeper = rows[0]
        if keeper.expires_at and keeper.expires_at <= now:
            # The caller must attach and delete the remote sandbox before the
            # durable lane is released for replacement.
            return keeper
        duplicates = rows[1:]
        for duplicate in duplicates:
            # Never hide a remote provider sandbox from reconciliation. A
            # duplicate means its deletion has not been proved, so keep it in
            # the release-blocking cleanup queue and stop this exact lane.
            duplicate.status = "cleanup_required"
            duplicate.close_reason = "duplicate_active_lane_cleanup_required"
            duplicate.error_message = "Operator provider cleanup required"
            duplicate.closed_at = None
        if duplicates:
            await db.commit()
            return duplicates[0]
        keeper.last_used_at = now
        await db.commit()
        return keeper


async def _mark_agentbay_ledger_unavailable(
    ledger_id: uuid.UUID,
    error: BaseException | str,
) -> None:
    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        result = await db.execute(
            select(AgentBaySessionLedger).where(
                AgentBaySessionLedger.id == ledger_id
            )
        )
        ledger = result.scalar_one_or_none()
        if ledger is not None and ledger.status == "active":
            ledger.status = "error"
            ledger.close_reason = "provider_attach_failed"
            ledger.error_message = (
                type(error).__name__
                if isinstance(error, BaseException)
                else "provider_attach_failed"
            )
            ledger.closed_at = datetime.now(timezone.utc)
            await db.commit()


async def _mark_agentbay_ledger_cleanup_required(
    ledger_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        result = await db.execute(
            select(AgentBaySessionLedger).where(
                AgentBaySessionLedger.id == ledger_id
            )
        )
        ledger = result.scalar_one_or_none()
        if ledger is not None and ledger.status == "active":
            ledger.status = "cleanup_required"
            ledger.close_reason = reason
            ledger.error_message = "Operator provider cleanup required"
            ledger.closed_at = None
            await db.commit()


async def _record_agentbay_cleanup_required(
    *,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    provider_session_id: str,
    image_type: str,
    reason: str,
) -> None:
    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    now = datetime.now(timezone.utc)
    async with async_session() as db:
        db.add(
            AgentBaySessionLedger(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                chat_session_id=str(session_id),
                provider_session_id=provider_session_id,
                image_type=image_type,
                purpose="operator_cleanup",
                status="cleanup_required",
                started_at=now,
                last_used_at=now,
                close_reason=reason,
                error_message="Operator provider cleanup required",
                context={"binding_version": 2},
            )
        )
        await db.commit()


async def _mark_agentbay_ledger_closed(
    ledger_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        result = await db.execute(
            select(AgentBaySessionLedger).where(
                AgentBaySessionLedger.id == ledger_id
            )
        )
        ledger = result.scalar_one_or_none()
        if ledger is not None and ledger.status == "active":
            ledger.status = "closed"
            ledger.close_reason = reason
            ledger.closed_at = datetime.now(timezone.utc)
            await db.commit()


async def _record_agentbay_ledger(
    *,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    provider_session_id: str,
    image_type: str,
) -> bool:
    from sqlalchemy.exc import IntegrityError

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    now = datetime.now(timezone.utc)
    async with async_session() as db:
        db.add(
            AgentBaySessionLedger(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                chat_session_id=str(session_id),
                provider_session_id=provider_session_id,
                image_type=image_type,
                purpose="tool_execution",
                status="active",
                started_at=now,
                last_used_at=now,
                expires_at=now + timedelta(hours=1),
                context={"binding_version": 2},
            )
        )
        try:
            await db.commit()
            return True
        except IntegrityError:
            await db.rollback()
            return False


def _agentbay_creation_lock_key(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    image_type: str,
) -> str:
    return f"agentbay-session-create:{agent_id}:{user_id}:{session_id}:{image_type}"


async def _agentbay_agent_has_live_fences(redis, agent_id: uuid.UUID) -> bool:
    """Return whether any creation/control/tool lease can still touch the Agent."""

    patterns = (
        f"agentbay-session-create:{agent_id}:*",
        f"agentbay-take-control:{agent_id}:*",
        f"agentbay-tool-execution:{agent_id}:*",
        f"agentbay-tool-verification:{agent_id}:*",
        f"agentbay-control-interaction:{agent_id}:*",
    )
    for pattern in patterns:
        async for _key in redis.scan_iter(match=pattern, count=100):
            return True
    return False


async def _set_agentbay_lane_cleanup_required(
    snapshot: _AgentBayLaneSnapshot,
    *,
    reason: str,
) -> None:
    """Persist an ambiguous provider deletion in a short CAS transaction."""

    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        row = (
            await db.execute(
                select(AgentBaySessionLedger)
                .where(AgentBaySessionLedger.id == snapshot.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.status == "closed":
            return
        if (
            row.status not in {"active", "cleanup_required"}
            or row.provider_session_id != snapshot.provider_session_id
        ):
            raise RuntimeError("AgentBay cleanup ledger changed during provider deletion")
        row.status = "cleanup_required"
        row.close_reason = reason
        row.error_message = "Operator provider cleanup required"
        row.closed_at = None
        await db.commit()


async def _close_agentbay_lane_after_provider_delete(
    snapshot: _AgentBayLaneSnapshot,
    *,
    reason: str,
) -> None:
    """Close one exact ledger row only after provider deletion was proven."""

    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        row = (
            await db.execute(
                select(AgentBaySessionLedger)
                .where(AgentBaySessionLedger.id == snapshot.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("AgentBay cleanup ledger disappeared")
        if row.status == "closed" and row.provider_session_id == snapshot.provider_session_id:
            return
        if (
            row.status not in {"active", "cleanup_required"}
            or row.provider_session_id != snapshot.provider_session_id
        ):
            raise RuntimeError("AgentBay cleanup ledger changed during provider deletion")
        row.status = "closed"
        row.close_reason = reason
        row.error_message = None
        row.closed_at = datetime.now(timezone.utc)
        await db.commit()


async def _persist_agentbay_cleanup_under_cancellation(
    snapshot: _AgentBayLaneSnapshot,
    *,
    reason: str,
) -> None:
    task = asyncio.create_task(
        _set_agentbay_lane_cleanup_required(snapshot, reason=reason)
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _close_agentbay_lanes_for_agent(*, agent_id: uuid.UUID) -> None:
    """Strictly delete every durable provider sandbox before Agent deletion."""

    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(AgentBaySessionLedger)
                    .where(
                        AgentBaySessionLedger.agent_id == agent_id,
                        AgentBaySessionLedger.status.in_(
                            ["active", "cleanup_required"]
                        ),
                    )
                    .order_by(
                        AgentBaySessionLedger.chat_session_id,
                        AgentBaySessionLedger.image_type,
                        AgentBaySessionLedger.id,
                    )
                )
            ).scalars().all()
        )
        snapshots = [
            _AgentBayLaneSnapshot(
                id=row.id,
                provider_session_id=row.provider_session_id,
                image_type=row.image_type,
                chat_session_id=row.chat_session_id,
            )
            for row in rows
        ]
        await db.rollback()

    if not snapshots:
        return
    client: AgentBayClient | None = None
    for snapshot in snapshots:
        if not snapshot.provider_session_id:
            await _set_agentbay_lane_cleanup_required(
                snapshot,
                reason="agent_delete_missing_provider_identity",
            )
            raise RuntimeError(
                "AgentBay provider cleanup must be verified before deleting this Agent"
            )
        if client is None:
            client, _tool_config = await _configured_agentbay_client(agent_id)
        try:
            await client.attach_session(
                snapshot.provider_session_id,
                snapshot.image_type,
            )
            await client.delete_session_strict()
        except BaseException:
            await _persist_agentbay_cleanup_under_cancellation(
                snapshot,
                reason="agent_delete_provider_cleanup_unconfirmed",
            )
            raise

        await _close_agentbay_lane_after_provider_delete(
            snapshot,
            reason="agent_deleted",
        )
        canonical_session_id = _canonical_chat_session_id(snapshot.chat_session_id)
        if canonical_session_id is not None:
            cached = _agentbay_sessions.pop(
                (agent_id, canonical_session_id, snapshot.image_type),
                None,
            )
            if cached is not None:
                cached[0]._session = None
                cached[0]._browser_initialized = False


@asynccontextmanager
async def agentbay_agent_deletion_fence(*, agent_id: uuid.UUID):
    """Block new provider work and prove every sandbox deletion before DB delete."""

    from app.core.events import get_redis
    from app.services.agentbay_control_lock import agentbay_agent_deletion_key

    redis = await get_redis()
    key = agentbay_agent_deletion_key(agent_id)
    token = str(uuid.uuid4())
    acquired = bool(
        await redis.set(
            key,
            token,
            ex=_AGENT_DELETION_LOCK_TTL_SECONDS,
            nx=True,
        )
    )
    if not acquired:
        raise RuntimeError("AgentBay Agent deletion is already in progress")

    stop = asyncio.Event()
    fence_lost = asyncio.Event()
    owner_task = asyncio.current_task()

    async def _renew() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
                return
            except TimeoutError:
                pass
            try:
                refreshed = await redis.eval(
                    _SESSION_CREATE_REFRESH_LUA,
                    1,
                    key,
                    token,
                    str(_AGENT_DELETION_LOCK_TTL_SECONDS),
                )
            except Exception:
                refreshed = 0
            if int(refreshed or 0) != 1:
                fence_lost.set()
                if owner_task is not None and not owner_task.done():
                    owner_task.cancel(
                        "AgentBay Agent-deletion fence could not be renewed"
                    )
                return

    renewal_task = asyncio.create_task(_renew())
    try:
        if await _agentbay_agent_has_live_fences(redis, agent_id):
            raise RuntimeError(
                "AgentBay session is busy; retry Agent deletion after it settles"
            )
        await _close_agentbay_lanes_for_agent(agent_id=agent_id)
        yield
    finally:
        stop.set()
        renewal_task.cancel()
        await asyncio.gather(renewal_task, return_exceptions=True)
        if not fence_lost.is_set():
            try:
                await redis.eval(_SESSION_CREATE_RELEASE_LUA, 1, key, token)
            except Exception as exc:
                # The Agent is already durably stopped/deletion-requested; a
                # failed Redis release must not turn a committed DB deletion
                # into a misleading client failure. The lease expires by TTL.
                logger.error(
                    "[AgentBay] Agent-deletion lock release failed error_type={}",
                    type(exc).__name__,
                )


async def _close_agentbay_lanes_for_chat_session(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
) -> None:
    """Prove provider deletion for every durable sandbox in one chat lane."""

    from sqlalchemy import select

    from app.database import async_session
    from app.models.agentbay_session import AgentBaySessionLedger

    async with async_session() as db:
        result = await db.execute(
            select(AgentBaySessionLedger)
            .where(
                AgentBaySessionLedger.agent_id == agent_id,
                AgentBaySessionLedger.user_id == user_id,
                AgentBaySessionLedger.chat_session_id == str(session_id),
                AgentBaySessionLedger.status.in_(["active", "cleanup_required"]),
            )
            .order_by(AgentBaySessionLedger.image_type, AgentBaySessionLedger.id)
        )
        rows = list(result.scalars().all())
        snapshots = [
            _AgentBayLaneSnapshot(
                id=row.id,
                provider_session_id=row.provider_session_id,
                image_type=row.image_type,
                chat_session_id=row.chat_session_id,
            )
            for row in rows
        ]
        await db.rollback()

    if not snapshots:
        return
    client: AgentBayClient | None = None
    for snapshot in snapshots:
        if not snapshot.provider_session_id:
            await _set_agentbay_lane_cleanup_required(
                snapshot,
                reason="chat_delete_missing_provider_identity",
            )
            raise RuntimeError(
                "AgentBay provider cleanup must be verified before deleting this chat"
            )
        if client is None:
            client, _tool_config = await _configured_agentbay_client(agent_id)
        try:
            await client.attach_session(
                snapshot.provider_session_id,
                snapshot.image_type,
            )
            await client.delete_session_strict()
        except BaseException:
            await _persist_agentbay_cleanup_under_cancellation(
                snapshot,
                reason="chat_delete_provider_cleanup_unconfirmed",
            )
            raise
        await _close_agentbay_lane_after_provider_delete(
            snapshot,
            reason="chat_session_deleted",
        )
        cached = _agentbay_sessions.pop(
            (agent_id, str(session_id), snapshot.image_type),
            None,
        )
        if cached is not None:
            cached[0]._session = None
            cached[0]._browser_initialized = False


@asynccontextmanager
async def agentbay_chat_session_deletion_fence(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
):
    """Block creation/control while a chat and all its sandboxes are deleted."""

    from app.core.events import get_redis
    from app.services.agentbay_control_lock import (
        agentbay_agent_deletion_key,
        agentbay_tool_execution_lease,
    )

    canonical_session_id = _canonical_chat_session_id(session_id)
    if canonical_session_id is None:
        raise RuntimeError("AgentBay chat deletion requires a canonical session UUID")

    redis = await get_redis()
    token = str(uuid.uuid4())
    image_types = ("browser", "code", "computer")
    keys = [
        _agentbay_creation_lock_key(
            agent_id,
            user_id,
            canonical_session_id,
            image_type,
        )
        for image_type in image_types
    ]
    acquired: list[str] = []
    renewal_task: asyncio.Task | None = None
    owner_task = asyncio.current_task()

    async def _renew() -> None:
        while True:
            await asyncio.sleep(max(1, _SESSION_CREATE_LOCK_TTL_SECONDS // 3))
            for key in keys:
                try:
                    renewed = await redis.eval(
                        _SESSION_CREATE_REFRESH_LUA,
                        1,
                        key,
                        token,
                        str(_SESSION_CREATE_LOCK_TTL_SECONDS),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    renewed = 0
                if int(renewed or 0) != 1:
                    if owner_task is not None and not owner_task.done():
                        owner_task.cancel(
                            "AgentBay chat-deletion fence could not be renewed"
                        )
                    return

    try:
        async with agentbay_tool_execution_lease(agent_id, canonical_session_id):
            for key in keys:
                acquire_result = int(
                    await redis.eval(
                        _SESSION_CREATE_ACQUIRE_LUA,
                        2,
                        key,
                        agentbay_agent_deletion_key(agent_id),
                        token,
                        str(_SESSION_CREATE_LOCK_TTL_SECONDS),
                    )
                )
                if acquire_result < 0:
                    raise RuntimeError("AgentBay Agent deletion is in progress")
                if acquire_result == 0:
                    raise RuntimeError(
                        "AgentBay session is busy; retry chat deletion later"
                    )
                acquired.append(key)
            renewal_task = asyncio.create_task(_renew())
            await _close_agentbay_lanes_for_chat_session(
                agent_id=agent_id,
                user_id=user_id,
                session_id=canonical_session_id,
            )
            yield
    finally:
        if renewal_task is not None:
            renewal_task.cancel()
            await asyncio.gather(renewal_task, return_exceptions=True)
        for key in reversed(acquired):
            try:
                await redis.eval(
                    _SESSION_CREATE_RELEASE_LUA,
                    1,
                    key,
                    token,
                )
            except Exception as exc:
                logger.error(
                    "[AgentBay] Chat-deletion lock release failed error_type={}",
                    type(exc).__name__,
                )


async def _attach_from_ledger(
    client: AgentBayClient,
    ledger,
    *,
    image_type: str,
) -> bool:
    if not ledger or not ledger.provider_session_id:
        return False
    context = ledger.context if isinstance(ledger.context, dict) else {}
    if ledger.status == "cleanup_required":
        raise RuntimeError(
            "AgentBay provider cleanup must be verified before this lane can resume"
        )
    if (
        ledger.status != "active"
        or context.get("binding_version") != 2
        or not ledger.tenant_id
        or not ledger.agent_id
        or not ledger.user_id
        or not ledger.chat_session_id
    ):
        return False
    try:
        await client.attach_session(ledger.provider_session_id, image_type)
        if ledger.expires_at and ledger.expires_at <= datetime.now(timezone.utc):
            try:
                await client.delete_session_strict()
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    _mark_agentbay_ledger_cleanup_required(
                        ledger.id,
                        reason="provider_session_expiry_cleanup_unconfirmed",
                    )
                )
                await asyncio.shield(cleanup_task)
                raise
            except Exception:
                await _mark_agentbay_ledger_cleanup_required(
                    ledger.id,
                    reason="provider_session_expiry_cleanup_unconfirmed",
                )
                return False
            await _mark_agentbay_ledger_closed(
                ledger.id,
                reason="provider_session_expired",
            )
            return False
        return True
    except Exception as exc:
        error_type = type(exc).__name__
        logger.warning(
            "[AgentBay] Durable provider-session attach failed error_type={}",
            error_type,
        )
        await _mark_agentbay_ledger_cleanup_required(
            ledger.id,
            reason="provider_attach_unconfirmed",
        )
        return False


async def get_agentbay_client_for_agent(
    agent_id: uuid.UUID,
    image_type: str,
    session_id: str = "",
) -> AgentBayClient:
    """Get or create AgentBay client for agent.

    Sessions are cached per (agent_id, session_id, image_type) so that each
    ChatSession gets its own independent AgentBay instance. Multiple users
    chatting with the same Agent will each have isolated browser/desktop/code
    environments.

    Args:
        agent_id: The agent UUID.
        image_type: One of 'browser', 'computer', 'code'.
        session_id: The ChatSession ID. Defaults to '' for backward compat
                    (e.g. test_agentbay_channel, single-session callers).
    """

    lane = await _load_agentbay_lane(agent_id, session_id)
    canonical_session_id = lane[2] if lane else None
    cache_session_id = canonical_session_id or str(session_id)
    now = datetime.now()
    cache_key = (agent_id, cache_session_id, image_type)

    if lane is None:
        raise PermissionError(
            "AgentBay requires an exact authorized ChatSession UUID"
        )

    tenant_id, user_id, canonical_session_id = lane
    # The durable ledger, not process memory, is the reuse authority. Read it
    # before every cache hit so cleanup/expiry/reconciliation in another worker
    # takes effect immediately.
    ledger = await _get_active_agentbay_ledger(
        agent_id=agent_id,
        user_id=user_id,
        session_id=canonical_session_id,
        image_type=image_type,
    )

    if cache_key in _agentbay_sessions:
        client, last_used = _agentbay_sessions[cache_key]
        cached_provider_id = (
            str(client._session.session_id) if client._session is not None else None
        )
        ledger_context = (
            ledger.context if ledger is not None and isinstance(ledger.context, dict) else {}
        )
        durable_cache_match = bool(
            ledger is not None
            and ledger.status == "active"
            and ledger_context.get("binding_version") == 2
            and ledger.provider_session_id
            and str(ledger.provider_session_id) == cached_provider_id
            and (ledger.expires_at is None or ledger.expires_at > datetime.now(timezone.utc))
        )
        if durable_cache_match and now - last_used < _AGENTBAY_SESSION_TIMEOUT:
            # Session still valid, refresh timestamp and reuse
            _agentbay_sessions[cache_key] = (client, now)
            return client
        # Drop only this process-local handle. The exact durable row below will
        # decide whether to attach, delete an expired provider, or fail closed.
        logger.info(
            "[AgentBay] Cache binding is stale for {}; detaching",
            image_type,
        )
        client._session = None
        del _agentbay_sessions[cache_key]

    client, tool_config = await _configured_agentbay_client(agent_id)
    if await _attach_from_ledger(client, ledger, image_type=image_type):
        _agentbay_sessions[cache_key] = (client, now)
        return client

    from app.core.events import get_redis
    from app.services.agentbay_control_lock import agentbay_agent_deletion_key

    lock_key = _agentbay_creation_lock_key(
        agent_id, user_id, canonical_session_id, image_type
    )
    lock_token = str(uuid.uuid4())
    redis = await get_redis()
    acquired = False
    renewal_task: asyncio.Task | None = None
    owner_task = asyncio.current_task()

    async def _renew_creation_lock() -> None:
        while True:
            await asyncio.sleep(max(1, _SESSION_CREATE_LOCK_TTL_SECONDS // 3))
            try:
                renewed = await redis.eval(
                    _SESSION_CREATE_REFRESH_LUA,
                    1,
                    lock_key,
                    lock_token,
                    str(_SESSION_CREATE_LOCK_TTL_SECONDS),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[AgentBay] Session creation-lock renewal failed error_type={}",
                    type(exc).__name__,
                )
                renewed = 0
            if int(renewed or 0) != 1:
                logger.error("[AgentBay] Session creation-lock fence was lost")
                if owner_task is not None:
                    owner_task.cancel()
                return
    try:
        # Wait for an in-flight creator while repeatedly checking the durable
        # ledger. Redis failure propagates so creation fails closed.
        for _attempt in range(120):
            acquire_result = int(
                await redis.eval(
                    _SESSION_CREATE_ACQUIRE_LUA,
                    2,
                    lock_key,
                    agentbay_agent_deletion_key(agent_id),
                    lock_token,
                    str(_SESSION_CREATE_LOCK_TTL_SECONDS),
                )
            )
            if acquire_result < 0:
                raise PermissionError("AgentBay Agent deletion is in progress")
            acquired = acquire_result == 1
            ledger = await _get_active_agentbay_ledger(
                agent_id=agent_id,
                user_id=user_id,
                session_id=canonical_session_id,
                image_type=image_type,
            )
            if await _attach_from_ledger(client, ledger, image_type=image_type):
                _agentbay_sessions[cache_key] = (client, now)
                return client
            if acquired:
                break
            await asyncio.sleep(0.25)
        if not acquired:
            raise RuntimeError("Timed out waiting for the AgentBay session creator")

        renewal_task = asyncio.create_task(_renew_creation_lock())

        # The session may have been deleted or its ACL revoked while this
        # creator waited for Redis. Re-resolve the exact lane only after the
        # creation fence is ours, before any provider side effect.
        locked_lane = await _load_agentbay_lane(agent_id, canonical_session_id)
        if locked_lane != (tenant_id, user_id, canonical_session_id):
            raise PermissionError("AgentBay chat lane changed while waiting")

        provider_created = False
        durable_registered = False
        provider_session_id: str | None = None
        try:
            creation_task = asyncio.create_task(
                _create_agentbay_session(
                    client,
                    agent_id,
                    user_id,
                    image_type,
                    tool_config,
                )
            )
            try:
                await asyncio.shield(creation_task)
            except asyncio.CancelledError:
                # asyncio.to_thread cannot be cancelled safely. Keep the Redis
                # renewal alive and wait until the SDK call settles so a newly
                # created remote sandbox can be deleted or durably poisoned.
                await asyncio.gather(creation_task, return_exceptions=True)
                raise
            provider_created = client._session is not None
            if not provider_created:
                raise RuntimeError("AgentBay provider creation was not confirmed")
            provider_session_id = str(client._session.session_id)
            # A pause/deletion/ACL/company change may have committed while the
            # provider SDK call was in flight. Do not make that sandbox durable;
            # the outer cleanup path must delete it first.
            post_creation_lane = await _load_agentbay_lane(
                agent_id,
                canonical_session_id,
            )
            if post_creation_lane != (tenant_id, user_id, canonical_session_id):
                raise PermissionError(
                    "AgentBay chat lane was revoked during provider creation"
                )
            recorded = await _record_agentbay_ledger(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                session_id=canonical_session_id,
                provider_session_id=provider_session_id,
                image_type=image_type,
            )
            if recorded:
                durable_registered = True
            else:
                # A creator that lost the durable unique race must delete its
                # untracked provider sandbox before attaching the winner.
                try:
                    await client.delete_session_strict()
                except BaseException:
                    cleanup_task = asyncio.create_task(
                        _record_agentbay_cleanup_required(
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            user_id=user_id,
                            session_id=canonical_session_id,
                            provider_session_id=provider_session_id,
                            image_type=image_type,
                            reason="duplicate_creator_cleanup_unconfirmed",
                        )
                    )
                    await asyncio.shield(cleanup_task)
                    durable_registered = True
                    raise
                else:
                    provider_created = False
                winner = await _get_active_agentbay_ledger(
                    agent_id=agent_id,
                    user_id=user_id,
                    session_id=canonical_session_id,
                    image_type=image_type,
                )
                if not await _attach_from_ledger(
                    client, winner, image_type=image_type
                ):
                    raise RuntimeError(
                        "AgentBay active-lane race could not attach the durable winner"
                    )
            _agentbay_sessions[cache_key] = (client, now)
            return client
        except BaseException:
            provider_created = client._session is not None
            if provider_created and provider_session_id is None:
                provider_session_id = str(client._session.session_id)
            if provider_created and not durable_registered:
                async def _cleanup_untracked_provider() -> None:
                    try:
                        await client.delete_session_strict()
                    except BaseException as cleanup_exc:
                        await _record_agentbay_cleanup_required(
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            user_id=user_id,
                            session_id=canonical_session_id,
                            provider_session_id=str(provider_session_id),
                            image_type=image_type,
                            reason="untracked_session_cleanup_unconfirmed",
                        )
                        logger.error(
                            "[AgentBay] Untracked provider-session cleanup failed error_type={}",
                            type(cleanup_exc).__name__,
                        )

                cleanup_task = asyncio.create_task(_cleanup_untracked_provider())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await asyncio.gather(cleanup_task, return_exceptions=True)
            raise
    finally:
        if renewal_task is not None:
            renewal_task.cancel()
            await asyncio.gather(renewal_task, return_exceptions=True)
        if acquired:
            try:
                await redis.eval(
                    _SESSION_CREATE_RELEASE_LUA,
                    1,
                    lock_key,
                    lock_token,
                )
            except Exception as exc:
                logger.error(
                    "[AgentBay] Session creation-lock release failed error_type={}",
                    type(exc).__name__,
                )


async def _create_agentbay_session(
    client: AgentBayClient,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    image_type: str,
    tool_config: dict | None,
) -> None:
    if image_type == "browser":
        await client.create_session(
            "browser_latest",
            _manager_token=_AGENTBAY_MANAGER_CREATION_TOKEN,
        )
        await _inject_credentials(client, agent_id, user_id)
    elif image_type == "computer":
        os_type = (tool_config or {}).get("os_type", "windows")
        computer_image = (
            "windows_latest" if os_type == "windows" else "linux_latest"
        )
        logger.info(
            "[AgentBay] Creating computer session OS={} image={}",
            os_type,
            computer_image,
        )
        await client.create_session(
            computer_image,
            _manager_token=_AGENTBAY_MANAGER_CREATION_TOKEN,
        )
    else:
        await client.create_session(
            "code_latest",
            _manager_token=_AGENTBAY_MANAGER_CREATION_TOKEN,
        )


async def get_existing_agentbay_client_for_agent(
    agent_id: uuid.UUID,
    image_type: str,
    session_id: str,
) -> AgentBayClient | None:
    """Attach to an existing exact lane without ever creating a blank sandbox."""

    lane = await _load_agentbay_lane(agent_id, session_id)
    canonical_session_id = lane[2] if lane else None
    cache_key = (agent_id, canonical_session_id or str(session_id), image_type)
    if lane is None:
        return None
    _tenant_id, user_id, canonical_session_id = lane
    ledger = await _get_active_agentbay_ledger(
        agent_id=agent_id,
        user_id=user_id,
        session_id=canonical_session_id,
        image_type=image_type,
    )
    cached = _agentbay_sessions.get(cache_key)
    if cached is not None:
        client, _last_used = cached
        cached_provider_id = (
            str(client._session.session_id) if client._session is not None else None
        )
        ledger_context = (
            ledger.context if ledger is not None and isinstance(ledger.context, dict) else {}
        )
        if (
            ledger is not None
            and ledger.status == "active"
            and ledger_context.get("binding_version") == 2
            and str(ledger.provider_session_id or "") == cached_provider_id
            and (
                ledger.expires_at is None
                or ledger.expires_at > datetime.now(timezone.utc)
            )
        ):
            _agentbay_sessions[cache_key] = (client, datetime.now())
            return client
        client._session = None
        del _agentbay_sessions[cache_key]
    if ledger is None:
        return None
    client, _tool_config = await _configured_agentbay_client(agent_id)
    if not await _attach_from_ledger(client, ledger, image_type=image_type):
        return None
    _agentbay_sessions[cache_key] = (client, datetime.now())
    return client


async def cleanup_agentbay_sessions():
    """Clean up process-local handles without deleting durable user sandboxes."""
    now = datetime.now()
    expired = [
        cache_key for cache_key, (client, last_used) in _agentbay_sessions.items()
        if now - last_used > _AGENTBAY_SESSION_TIMEOUT
    ]
    for cache_key in expired:
        client, _ = _agentbay_sessions.pop(cache_key)
        agent_id, session_id, image_type = cache_key
        logger.info(f"[AgentBay] Cleaning up expired {image_type} session for agent {agent_id}")
        try:
            uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            await client.close_session()
        else:
            # UUID chat lanes are shared through the durable ledger and may be
            # attached by API and worker processes simultaneously. Provider
            # expiry/failed re-attach closes the ledger; local idle cleanup must
            # not delete the shared remote session.
            client._session = None
            client._browser_initialized = False


async def start_agentbay_session_cache_daemon() -> None:
    """Continuously detach expired process-local AgentBay handles."""

    interval = max(min(int(_AGENTBAY_SESSION_TIMEOUT.total_seconds() // 2), 60), 5)
    logger.info("[AgentBay] local session-cache daemon started interval={}s", interval)
    while True:
        try:
            await cleanup_agentbay_sessions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[AgentBay] local session-cache cleanup failed")
        await asyncio.sleep(interval)


async def _inject_credentials(
    client: AgentBayClient,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
):
    """Inject stored cookies into the browser via CDP after initialization.

    Reads all 'active' credentials with cookies from the agent_credentials table,
    decrypts cookies_json, and injects them via a Playwright Node.js script that
    connects to Chrome's CDP port (localhost:9222).

    This runs automatically after every browser session creation. If no credentials
    exist or injection fails, it logs a warning but does not block the session.
    """
    import json
    from app.database import async_session as async_session_factory
    from app.models.agent_credential import AgentCredential
    from sqlalchemy import select
    from app.core.security import decrypt_data
    from app.config import get_settings

    settings = get_settings()

    # Fetch active credentials with stored cookies
    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(AgentCredential).where(
                    AgentCredential.agent_id == agent_id,
                    AgentCredential.owner_user_id == owner_user_id,
                    AgentCredential.status == "active",
                    AgentCredential.cookies_json.isnot(None),
                )
            )
            credentials = result.scalars().all()
    except Exception as exc:
        logger.warning(
            "[AgentBay] Failed to query credentials for injection error_type={}",
            type(exc).__name__,
        )
        return

    if not credentials:
        return  # No cookies to inject

    # Collect and decrypt all cookies
    all_cookies = []
    for cred in credentials:
        try:
            raw = decrypt_data(cred.cookies_json, settings.SECRET_KEY)
            cookies = json.loads(raw)
            if isinstance(cookies, list):
                all_cookies.extend(cookies)
        except Exception as exc:
            logger.warning(
                "[AgentBay] Failed to decrypt cookies platform_present={} error_type={}",
                bool(cred.platform),
                type(exc).__name__,
            )

    if not all_cookies:
        return

    # Ensure browser is initialized before injection (Chrome must be running)
    try:
        await client._ensure_browser_initialized()
    except Exception as exc:
        logger.warning(
            "[AgentBay] Cannot inject cookies; browser not initialized error_type={}",
            type(exc).__name__,
        )
        return

    # Build a credential-free Node.js program. Cookie bytes are supplied only
    # through the one-shot process environment; they are never embedded in a
    # command string or persisted in the remote sandbox filesystem.
    #
    # Cookies stored in DB were already sanitized at export time (sameSite title-cased,
    # expires:-1 removed, domain without leading dot), so we only do a defensive
    # re-sanitize here in case older records were stored before the fix.
    import base64 as _base64
    cookies_payload_b64 = _base64.b64encode(
        json.dumps(all_cookies, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    inject_script = r"""
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    try {
        const encodedCookies = process.env.ASTRA_COOKIES_B64 || '';
        delete process.env.ASTRA_COOKIES_B64;
        const rawCookies = JSON.parse(Buffer.from(encodedCookies, 'base64').toString('utf8'));
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];

        // Defensive sanitize: normalize sameSite casing and strip invalid expires
        const sameSiteMap = { none: 'None', lax: 'Lax', strict: 'Strict' };
        const cookies = rawCookies.map(c => {
            const out = { ...c };
            if (out.sameSite != null) {
                out.sameSite = sameSiteMap[String(out.sameSite).toLowerCase()] || 'Lax';
            }
            if (out.expires != null && out.expires <= 0) {
                delete out.expires;
            }
            // Ensure domain has leading dot for subdomain matching
            if (out.domain && !out.domain.startsWith('.')) {
                out.domain = '.' + out.domain;
            }
            return out;
        });

        let injected = 0;
        let failed = 0;
        // Inject one at a time so a single bad cookie doesn't break the rest
        for (const cookie of cookies) {
            try {
                await context.addCookies([cookie]);
                injected++;
            } catch (e) {
                failed++;
                if (failed <= 3) {
                    // Cookie validation errors may echo secret values. Emit
                    // only a bounded content-free signal.
                    console.error('INJECT_SKIP');
                }
            }
        }
        console.log('INJECT_OK:' + injected + ' injected, ' + failed + ' skipped');
        process.exit(0);
    } catch (e) {
        console.error('INJECT_FAIL:' + e.message);
        process.exit(1);
    }
})();
"""
    try:
        # The command contains only the credential-free program. The SDK's
        # per-process envs channel is ephemeral and avoids shell interpolation,
        # process arguments, and sandbox file residue.
        script_b64 = _base64.b64encode(inject_script.encode('utf-8')).decode('ascii')
        exec_result = await asyncio.to_thread(
            client._session.command.exec,
            (
                "node -e \"eval(Buffer.from('"
                + script_b64
                + "','base64').toString('utf8'))\""
            ),
            timeout_ms=15000,
            envs={"ASTRA_COOKIES_B64": cookies_payload_b64},
        )
        stdout = getattr(exec_result, 'stdout', '') or getattr(exec_result, 'output', '') or ''
        stderr = getattr(exec_result, 'stderr', '') or ''

        if "INJECT_OK" in stdout:
            logger.info(f"[AgentBay] Cookie injection successful for agent {agent_id}")
            # Update last_injected_at for all injected credentials
            try:
                from datetime import timezone as tz
                now = datetime.now(tz.utc)
                async with async_session_factory() as db:
                    for cred in credentials:
                        cred.last_injected_at = now
                        db.add(cred)
                    await db.commit()
            except Exception as exc:
                logger.warning(
                    "[AgentBay] Failed to update last_injected_at error_type={}",
                    type(exc).__name__,
                )
        else:
            logger.warning(
                "[AgentBay] Cookie injection may have failed stdout_chars={} stderr_chars={}",
                len(stdout),
                len(stderr),
            )
    except Exception as exc:
        logger.warning(
            "[AgentBay] Cookie injection error_type={}",
            type(exc).__name__,
        )
