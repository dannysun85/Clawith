"""HTML to PDF conversion service."""

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.document_conversion.chrome_renderer import chrome_executable
from app.services.document_conversion.chrome_renderer import collect_browser_layout
from app.services.document_conversion.presentation_contract import (
    PresentationVisualQualityError,
    validate_browser_slide_visual_quality,
    validate_presentation_html_contract,
)
from app.services.process_utils import terminate_popen_process_group


_CDP_MAX_MESSAGE_BYTES = 200_000_000


def _validate_pdf_page_count(
    pdf_file: Path,
    expected_page_count: int | None,
) -> None:
    """Reject rendered PDFs whose physical page count violates the deck plan."""

    if expected_page_count is None:
        return

    import fitz

    with fitz.open(str(pdf_file)) as document:
        actual_page_count = document.page_count
    if actual_page_count != expected_page_count:
        raise ValueError(
            "Rendered PDF page count mismatch: "
            f"expected {expected_page_count}, found {actual_page_count}"
        )


def _write_slide_screenshot_pdf(
    screenshot_paths: list[Path],
    target: Path,
    *,
    design_width_px: int,
    design_height_px: int,
) -> None:
    """Build a visual-preview PDF from browser screenshots of each slide."""

    import fitz

    width_points = design_width_px * 72 / 96
    height_points = design_height_px * 72 / 96
    document = fitz.open()
    try:
        for screenshot_path in screenshot_paths:
            if not screenshot_path.is_file():
                raise ValueError(
                    f"Presentation slide screenshot not found: {screenshot_path}"
                )
            page = document.new_page(
                width=width_points,
                height=height_points,
            )
            page.insert_image(page.rect, filename=str(screenshot_path))
        document.save(
            str(target),
            garbage=4,
            deflate=True,
        )
    finally:
        document.close()


async def _render_managed_presentation_pdf(
    src_file: Path,
    tgt_file: Path,
    *,
    design_width_px: int,
    design_height_px: int,
    expected_page_count: int,
) -> None:
    """Render managed slide decks from screen-layout screenshots.

    Chrome print fragmentation can move an oversized CSS grid out of its
    fixed-height slide and leave a visually blank physical page. Capturing the
    already-validated slide roots preserves the exact screen composition used
    by the product preview.
    """

    layout = await collect_browser_layout(
        src_file,
        design_width_px,
        design_height_px,
        "visual",
        1.0,
    )
    validate_browser_slide_visual_quality(
        layout or {},
        screenshot_key="screenshots",
    )
    screenshot_paths = [
        Path(str(path))
        for path in (layout or {}).get("screenshots") or []
        if path
    ]
    try:
        if len(screenshot_paths) != expected_page_count:
            raise ValueError(
                "Presentation screenshot page count mismatch: "
                f"expected {expected_page_count}, found {len(screenshot_paths)}"
            )
        _write_slide_screenshot_pdf(
            screenshot_paths,
            tgt_file,
            design_width_px=design_width_px,
            design_height_px=design_height_px,
        )
    finally:
        for screenshot_path in screenshot_paths:
            screenshot_path.unlink(missing_ok=True)


def _paged_pdf_geometry(
    arguments: dict[str, Any],
    *,
    design_width_px: int,
    design_height_px: int,
) -> dict[str, float | bool]:
    """Keep paged presentation PDFs at the authored slide aspect ratio."""

    return {
        "preferCSSPageSize": bool(arguments.get("prefer_css_page_size", True)),
        "paperWidth": float(arguments.get("paper_width") or design_width_px / 96.0),
        "paperHeight": float(arguments.get("paper_height") or design_height_px / 96.0),
        "scale": float(arguments.get("scale") or 1.0),
    }


def _install_weasyprint_stub_if_unavailable() -> None:
    """Keep WeasyPrint mockable when native system libraries are missing."""
    if "weasyprint" in sys.modules:
        return
    try:
        __import__("weasyprint")
        return
    except Exception as exc:
        unavailable_error = str(exc)
        module = types.ModuleType("weasyprint")

        class _UnavailableHTML:
            def __init__(self, *args, **kwargs):
                raise RuntimeError(f"WeasyPrint unavailable: {unavailable_error}")

        module.HTML = _UnavailableHTML
        module.__clawith_unavailable_error__ = exc
        sys.modules["weasyprint"] = module


_install_weasyprint_stub_if_unavailable()


async def convert_html_to_pdf(src_file: Path, tgt_file: Path, target_path: str, arguments: dict[str, Any]) -> str:
    try:
        expected_page_count_value = arguments.get("expected_page_count")
        expected_page_count = (
            int(expected_page_count_value)
            if expected_page_count_value is not None
            else None
        )
        validate_presentation_html_contract(
            src_file,
            expected_page_count=expected_page_count,
            outline_file=(
                Path(str(arguments["_outline_file_path"]))
                if arguments.get("_outline_file_path")
                else None
            ),
            slide_spec_file=(
                Path(str(arguments["_slide_spec_file_path"]))
                if arguments.get("_slide_spec_file_path")
                else None
            ),
        )
        tgt_file.parent.mkdir(parents=True, exist_ok=True)
        design_w_px = int(arguments.get("design_width") or 1280)
        design_h_px = int(arguments.get("design_height") or 720)
        mode = str(arguments.get("pdf_mode") or "pages").lower()
        if expected_page_count is not None and mode == "pages":
            await _render_managed_presentation_pdf(
                src_file,
                tgt_file,
                design_width_px=design_w_px,
                design_height_px=design_h_px,
                expected_page_count=expected_page_count,
            )
            _validate_pdf_page_count(tgt_file, expected_page_count)
            return (
                "✅ Successfully converted managed presentation HTML to "
                f"visual-preview PDF: {target_path}"
            )
        chrome_pdf_error: Exception | None = None

        async def try_chrome_pdf() -> bool:
            import base64
            import socket
            import subprocess
            import tempfile
            import time
            import urllib.request
            import websockets

            chrome = chrome_executable()
            if not chrome:
                return False

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            profile_dir = tempfile.TemporaryDirectory(prefix="clawith-html-pdf-")
            chrome_args = [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--allow-file-access-from-files",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir.name}",
                "about:blank",
            ]
            import sys
            if sys.platform.startswith("linux"):
                # Linux environments (like Docker containers) require no-sandbox in standard restricted container contexts
                chrome_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])

            proc = subprocess.Popen(
                chrome_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.time() + 8
                while time.time() < deadline:
                    try:
                        with urllib.request.urlopen(f"{base}/json/version", timeout=0.25) as resp:
                            json.loads(resp.read().decode("utf-8"))
                        break
                    except Exception:
                        await asyncio.sleep(0.1)
                else:
                    return False

                file_url = src_file.resolve().as_uri()
                req = urllib.request.Request(f"{base}/json/new?{file_url}", method="PUT")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    target = json.loads(resp.read().decode("utf-8"))
                ws_url = target.get("webSocketDebuggerUrl")
                if not ws_url:
                    return False

                msg_id = 0
                async with websockets.connect(
                    ws_url,
                    max_size=_CDP_MAX_MESSAGE_BYTES,
                ) as ws_conn:
                    async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                        nonlocal msg_id
                        msg_id += 1
                        await ws_conn.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                        while True:
                            raw = await asyncio.wait_for(ws_conn.recv(), timeout=10)
                            message = json.loads(raw)
                            if message.get("id") == msg_id:
                                return message

                    await send("Page.enable")
                    await send("Runtime.enable")
                    await send("Emulation.setDeviceMetricsOverride", {
                        "width": design_w_px,
                        "height": design_h_px,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    })
                    await send("Emulation.setEmulatedMedia", {"media": "screen"})
                    await send("Page.navigate", {"url": file_url})
                    load_deadline = time.time() + 8
                    while time.time() < load_deadline:
                        raw = await asyncio.wait_for(ws_conn.recv(), timeout=10)
                        message = json.loads(raw)
                        if message.get("method") == "Page.loadEventFired":
                            break
                    await asyncio.sleep(0.25)

                    page_info = await send("Runtime.evaluate", {
                        "expression": "(() => ({w: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0, innerWidth), h: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0, innerHeight)}))()",
                        "returnByValue": True,
                    })
                    dims = page_info.get("result", {}).get("result", {}).get("value") or {}
                    scroll_w = max(1, float(dims.get("w") or design_w_px))
                    scroll_h = max(1, float(dims.get("h") or design_h_px))

                    pdf_params: dict[str, Any] = {
                        "printBackground": bool(arguments.get("print_background", True)),
                        "marginTop": float(arguments.get("margin_top", 0)),
                        "marginBottom": float(arguments.get("margin_bottom", 0)),
                        "marginLeft": float(arguments.get("margin_left", 0)),
                        "marginRight": float(arguments.get("margin_right", 0)),
                    }
                    if mode in ("single", "long", "fullpage"):
                        pdf_params.update({
                            "paperWidth": scroll_w / 96.0,
                            "paperHeight": scroll_h / 96.0,
                            "scale": 1,
                            "preferCSSPageSize": bool(
                                arguments.get("prefer_css_page_size", False)
                            ),
                        })
                    else:
                        pdf_params.update(
                            _paged_pdf_geometry(
                                arguments,
                                design_width_px=design_w_px,
                                design_height_px=design_h_px,
                            )
                        )

                    pdf_result = await send("Page.printToPDF", pdf_params)
                    data = pdf_result.get("result", {}).get("data")
                    if not data:
                        return False
                    tgt_file.write_bytes(base64.b64decode(data))
                    return True
            finally:
                terminate_popen_process_group(proc)
                profile_dir.cleanup()

        try:
            chrome_success = await try_chrome_pdf()
            if chrome_success:
                _validate_pdf_page_count(tgt_file, expected_page_count)
                return f"✅ Successfully converted HTML to PDF with Chrome: {target_path}"
            else:
                chrome_pdf_error = Exception("Chrome process timed out or failed to connect to debugging port")
                logger.warning("Chrome HTML to PDF failed (timed out), falling back to WeasyPrint")
        except Exception as exc:
            chrome_pdf_error = exc
            logger.warning(f"Chrome HTML to PDF failed, falling back to WeasyPrint: {exc}")

        from weasyprint import HTML
        HTML(filename=str(src_file)).write_pdf(str(tgt_file))
        _validate_pdf_page_count(tgt_file, expected_page_count)
        note = f" Chrome fallback reason: {chrome_pdf_error}" if chrome_pdf_error else ""
        return f"✅ Successfully converted HTML to PDF with WeasyPrint: {target_path}.{note}"
    except PresentationVisualQualityError:
        raise
    except Exception as e:
        logger.exception(f"Convert HTML to PDF failed: {e}")
        return f"❌ Conversion failed: {e}"
