"""Shared Chrome rendering helpers for document conversion."""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.process_utils import terminate_popen_process_group


def chrome_executable() -> str | None:
    """Return a local Chrome/Chromium executable path if one is available."""
    candidates = [
        os.environ.get("CHROME_BIN"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    return next((str(path) for path in candidates if path and Path(path).exists()), None)


def is_complex_css_paint(value: str | None) -> bool:
    value = str(value or "")
    return "gradient(" in value or "url(" in value


def is_translucent_css_color(value: str | None) -> bool:
    import re

    value = str(value or "").strip().lower()
    match = re.match(r"rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([0-9.]+)\s*\)", value)
    if not match:
        return False
    try:
        return float(match.group(1)) < 0.999
    except ValueError:
        return False


def build_hybrid_text_capture_css(
    text_item_ids: list[str],
    *,
    text_clip_item_ids: list[str] | None = None,
) -> str:
    """Hide editable text, including styled inline descendants, in visual captures."""
    selectors: list[str] = []
    for item_id in text_item_ids:
        selector = f'[data-clawith-item-id="{item_id}"]'
        selectors.extend(
            (
                selector,
                f"{selector} *",
                f"{selector}::before",
                f"{selector}::after",
                f"{selector} *::before",
                f"{selector} *::after",
            )
        )
    if not selectors:
        return ""
    css = (
        ",".join(selectors)
        + " { color: transparent !important; "
        "-webkit-text-fill-color: transparent !important; "
        "text-shadow: none !important; }"
    )
    clip_selectors = [
        f'[data-clawith-item-id="{item_id}"]'
        for item_id in (text_clip_item_ids or [])
    ]
    if clip_selectors:
        css += (
            " "
            + ",".join(clip_selectors)
            + " { background-image: none !important; "
            "background-color: transparent !important; }"
        )
    return css


async def collect_browser_layout(
    src_file: Path,
    design_w_px: int,
    design_h_px: int,
    render_mode: str,
    render_scale: float = 2.0,
) -> dict[str, Any] | None:
    import socket
    import subprocess
    import sys
    import tempfile
    import time
    import urllib.request
    import websockets

    chrome = chrome_executable()
    if not chrome:
        return None

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    profile_dir = tempfile.TemporaryDirectory(prefix="clawith-html-pptx-")
    
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
            return None

        file_url = src_file.resolve().as_uri()
        req = urllib.request.Request(f"{base}/json/new?{file_url}", method="PUT")
        with urllib.request.urlopen(req, timeout=2) as resp:
            target = json.loads(resp.read().decode("utf-8"))
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return None

        expression = r"""
(() => {
  const transparent = new Set(['rgba(0, 0, 0, 0)', 'transparent']);
  const viewport = { width: window.innerWidth, height: window.innerHeight };
  const pageStyle = getComputedStyle(document.body || document.documentElement);
  const pageBg = cssPaint(pageStyle) || '#ffffff';

  function isTransparentColor(value) {
    return !value || transparent.has(value) || /^rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\)$/.test(value);
  }

  function cssPaint(cs) {
    if (cs.backgroundColor && !isTransparentColor(cs.backgroundColor)) return cs.backgroundColor;
    if (cs.backgroundImage && cs.backgroundImage !== 'none') return cs.backgroundImage;
    return '';
  }

  function isVisible(el) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity || 1) > 0.01 && r.width > 0.5 && r.height > 0.5;
  }

  function childElements(el) {
    return Array.from(el.children || []).filter(child => {
      if (isVisible(child)) return true;
      return Array.from(child.querySelectorAll('*')).some(isVisible);
    });
  }

  function directText(el) {
    return Array.from(el.childNodes || [])
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent || '')
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function fullText(el) {
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function isInlineTag(tag) {
    return ['a', 'abbr', 'b', 'br', 'code', 'em', 'i', 'mark', 'small', 'span', 'strong', 'sub', 'sup', 'u'].includes(tag);
  }

  function isBlockTextTag(tag) {
    return ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'button', 'a', 'td', 'th'].includes(tag);
  }

  function hasPaint(cs) {
    const bg = cssPaint(cs);
    const border = ['Top', 'Right', 'Bottom', 'Left'].some(side => parseFloat(cs[`border${side}Width`] || '0') > 0);
    return !!bg || border;
  }

  function isTextClipBackground(cs) {
    const clip = `${cs.backgroundClip || ''} ${cs.webkitBackgroundClip || ''}`.toLowerCase();
    const fill = `${cs.webkitTextFillColor || ''}`.toLowerCase();
    return clip.includes('text') || fill === 'transparent' || fill === 'rgba(0, 0, 0, 0)';
  }

  function textLineBoxes(el, rootRect, directOnly = false) {
    const groups = [];
    const nodes = [];
    if (directOnly) {
      Array.from(el.childNodes || [])
        .filter(node => node.nodeType === Node.TEXT_NODE)
        .forEach(node => nodes.push(node));
    } else {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) nodes.push(node);
    }
    let measuredCharacters = 0;
    for (const node of nodes) {
      if (measuredCharacters >= 1200) break;
      const value = node.textContent || '';
      for (let index = 0; index < value.length && measuredCharacters < 1200; index += 1) {
        const character = value[index];
        const range = document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + 1);
        const rect = range.getBoundingClientRect();
        measuredCharacters += 1;
        if (rect.width <= 0 || rect.height <= 0) continue;
        let group = groups.find(candidate => Math.abs(candidate.top - rect.top) <= Math.max(2, rect.height * 0.2));
        if (!group) {
          group = {
            text: '',
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
          };
          groups.push(group);
        }
        group.text += character;
        group.left = Math.min(group.left, rect.left);
        group.top = Math.min(group.top, rect.top);
        group.right = Math.max(group.right, rect.right);
        group.bottom = Math.max(group.bottom, rect.bottom);
      }
    }
    return groups
      .sort((left, right) => left.top - right.top || left.left - right.left)
      .map(group => ({
        text: group.text.replace(/\s+/g, ' ').trim(),
        x: group.left - rootRect.left,
        y: group.top - rootRect.top,
        w: group.right - group.left,
        h: group.bottom - group.top,
      }));
  }

  function itemFor(el, rootRect, kind, text, directOnly = false) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    // One DOM element can contribute both a painted shape and editable text.
    // Reusing its identifier is essential: assigning a second id for the text
    // entry makes the earlier shape-capture selector point at nothing, so the
    // generated PPTX embeds a crop of the slide background instead of the card.
    const itemId = el.getAttribute('data-clawith-item-id')
      || `item-${Math.random().toString(36).slice(2)}-${Date.now()}`;
    el.setAttribute('data-clawith-item-id', itemId);
    return {
      itemId,
      kind,
      tag: el.tagName.toLowerCase(),
      text: text || '',
      src: el.currentSrc || el.getAttribute('src') || '',
      x: r.left - rootRect.left,
      y: r.top - rootRect.top,
      w: r.width,
      h: r.height,
      clientWidth: el.clientWidth,
      clientHeight: el.clientHeight,
      scrollWidth: el.scrollWidth,
      scrollHeight: el.scrollHeight,
      allowOverlap: el.getAttribute('data-allow-overlap') === 'true',
      textRole: el.getAttribute('data-clawith-text-role') || '',
      lines: kind === 'text' ? textLineBoxes(el, rootRect, directOnly) : [],
      style: {
color: cs.color,
backgroundColor: cs.backgroundColor,
backgroundImage: cs.backgroundImage,
borderColor: cs.borderTopColor,
borderWidth: cs.borderTopWidth,
borderRadius: cs.borderTopLeftRadius,
fontSize: cs.fontSize,
fontFamily: cs.fontFamily,
fontWeight: cs.fontWeight,
textAlign: cs.textAlign,
display: cs.display,
alignItems: cs.alignItems,
justifyContent: cs.justifyContent,
paddingLeft: cs.paddingLeft,
paddingRight: cs.paddingRight,
paddingTop: cs.paddingTop,
paddingBottom: cs.paddingBottom,
height: cs.height,
maxHeight: cs.maxHeight,
overflow: cs.overflow,
overflowX: cs.overflowX,
overflowY: cs.overflowY,
webkitTextFillColor: cs.webkitTextFillColor,
backgroundClip: cs.backgroundClip,
webkitBackgroundClip: cs.webkitBackgroundClip,
lineHeight: cs.lineHeight,
opacity: cs.opacity,
boxShadow: cs.boxShadow,
filter: cs.filter,
backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter,
      },
    };
  }

  function fitTextItemToLines(item) {
    const lines = (item.lines || []).filter(line => line.text && line.w > 0 && line.h > 0);
    if (!lines.length) return item;
    const originalRight = item.x + item.w;
    const left = Math.min(...lines.map(line => line.x));
    const top = Math.min(...lines.map(line => line.y));
    const right = Math.max(originalRight, ...lines.map(line => line.x + line.w));
    const bottom = Math.max(...lines.map(line => line.y + line.h));
    item.x = left;
    item.y = top;
    item.w = Math.max(1, right - left);
    item.h = Math.max(1, bottom - top);
    return item;
  }

  function collectRoot(root) {
    const rootRectRaw = root === document.body
      ? { left: 0, top: 0, width: viewport.width, height: viewport.height }
      : root.getBoundingClientRect();
      const rootRect = {
left: rootRectRaw.left || 0,
top: rootRectRaw.top || 0,
width: rootRectRaw.width || viewport.width,
height: rootRectRaw.height || viewport.height,
    };
    const items = [];
    const rootStyle = getComputedStyle(root);
    let preferWholeSlideVisualCapture = false;

    function walk(el) {
      if (!isVisible(el)) {
        const visibleDescendants = childElements(el);
        if (visibleDescendants.length) {
          // Presentation generators commonly wrap an absolute-positioned
          // visual canvas in a zero-size data-visual element. Preserve that
          // canvas as a single high-fidelity layer while extracting its text.
          preferWholeSlideVisualCapture = true;
        }
        visibleDescendants.forEach(walk);
        return;
      }
      const cs = getComputedStyle(el);
      const children = childElements(el);
      const tag = el.tagName.toLowerCase();
      const text = directText(el);
      const hasBlockChildren = children.some(child => !isInlineTag(child.tagName.toLowerCase()));
      const hasSeparatedInlineChildren = !hasBlockChildren && children.some(child => {
        const childStyle = getComputedStyle(child);
        return hasPaint(childStyle) || isTextClipBackground(childStyle);
      });

      // Keep SVG/canvas as one bounded visual item. Their internals are not
      // safely editable PPT primitives, but the hybrid renderer can capture
      // only this region instead of rasterizing the whole slide.
      if (tag === 'svg' || tag === 'canvas') {
        const visualItem = itemFor(el, rootRect, 'shape');
        visualItem.requiresScreenshot = true;
        items.push(visualItem);
        return;
      }

      if (el !== root && hasPaint(cs) && !isTextClipBackground(cs)) {
items.push(itemFor(el, rootRect, 'shape'));
      }
      if (tag === 'img') {
items.push(itemFor(el, rootRect, 'image'));
return;
      }
      if (isBlockTextTag(tag) && !hasBlockChildren) {
const content = fullText(el);
if (content) items.push(itemFor(el, rootRect, 'text', content));
return;
      }
      if (children.length && !hasBlockChildren && !hasSeparatedInlineChildren) {
        // Painted containers such as cards need their own visual layer, while
        // their inline descendants retain distinct typography in the editable
        // layer. Collapsing the whole card into one text item loses heading and
        // metadata styles and also used to overwrite the shape capture id.
        if (hasPaint(cs)) {
          if (text) {
            items.push(fitTextItemToLines(itemFor(el, rootRect, 'text', text, true)));
          }
          children.forEach(walk);
          return;
        }
        const content = fullText(el);
        if (content) items.push(itemFor(el, rootRect, 'text', content));
        return;
      }
      if (children.length && !hasBlockChildren && hasSeparatedInlineChildren) {
        if (text) {
          items.push(fitTextItemToLines(itemFor(el, rootRect, 'text', text, true)));
        }
        children.forEach(walk);
        return;
      }
      if (text || ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'span', 'strong', 'em', 'button', 'a'].includes(tag)) {
const content = text || (children.length ? '' : (el.innerText || '').replace(/\s+/g, ' ').trim());
if (content) items.push(itemFor(el, rootRect, 'text', content));
      }
      children.forEach(walk);
    }

    childElements(root).forEach(walk);
    return {
      x: rootRect.left,
      y: rootRect.top,
      width: rootRect.width,
      height: rootRect.height,
      backgroundColor: cssPaint(rootStyle) || pageBg,
      preferWholeSlideVisualCapture,
      items,
    };
  }

  let roots = Array.from(document.querySelectorAll('.slide,[data-slide]')).filter(isVisible);
  if (!roots.length) {
    const body = document.body || document.documentElement;
    roots = Array.from(body.children || [])
      .filter(el => isVisible(el) && !['script', 'style', 'link', 'meta'].includes(el.tagName.toLowerCase()))
      .filter(el => el.getBoundingClientRect().height >= 24);
    if (roots.length === 1) {
      const only = roots[0];
      const onlyRect = only.getBoundingClientRect();
      const children = Array.from(only.children || [])
.filter(el => isVisible(el) && !['script', 'style', 'link', 'meta'].includes(el.tagName.toLowerCase()))
.filter(el => el.getBoundingClientRect().height >= 24);
      if (onlyRect.height > viewport.height * 1.2 && children.length > 1) {
roots = children;
      } else if (onlyRect.width < viewport.width * 0.92 || onlyRect.height < viewport.height * 0.92) {
roots = [body];
      }
    }
  }
  if (!roots.length) roots = [document.body || document.documentElement];
  roots.forEach((root, index) => root.setAttribute('data-clawith-slide-root', String(index)));
  const brokenImages = Array.from(document.images || [])
    .filter(img => !img.complete || img.naturalWidth < 1 || img.naturalHeight < 1)
    .map(img => img.currentSrc || img.getAttribute('src') || '<empty>');
  return { viewport, pageBackground: pageBg, brokenImages, slides: roots.map(collectRoot) };
})()
"""

        msg_id = 0

        async with websockets.connect(ws_url, max_size=20_000_000) as ws_conn:
            async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal msg_id
                msg_id += 1
                await ws_conn.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                while True:
                    raw = await asyncio.wait_for(ws_conn.recv(), timeout=8)
                    message = json.loads(raw)
                    if message.get("id") == msg_id:
                        return message

            await send("Page.enable")
            await send("Runtime.enable")
            await send("Emulation.setDeviceMetricsOverride", {
                "width": design_w_px,
                "height": design_h_px,
                "deviceScaleFactor": render_scale,
                "mobile": False,
            })
            await send("Page.navigate", {"url": file_url})
            load_deadline = time.time() + 8
            while time.time() < load_deadline:
                raw = await asyncio.wait_for(ws_conn.recv(), timeout=8)
                message = json.loads(raw)
                if message.get("method") == "Page.loadEventFired":
                    break
            await asyncio.sleep(0.25)
            await send(
                "Runtime.evaluate",
                {
                    "expression": (
                        "Promise.all(Array.from(document.images || []).map(img => "
                        "img.complete ? Promise.resolve() : new Promise(resolve => {"
                        "const done=()=>resolve(); img.addEventListener('load',done,{once:true}); "
                        "img.addEventListener('error',done,{once:true}); setTimeout(done,3000);"
                        "})))"
                    ),
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            result = await send("Runtime.evaluate", {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            })
            layout = result.get("result", {}).get("result", {}).get("value")
            broken_images = list((layout or {}).get("brokenImages") or [])
            if broken_images:
                raise ValueError(
                    "Presentation browser could not load image assets: "
                    + ", ".join(str(path) for path in broken_images[:5])
                )
            if layout and render_mode in ("visual", "screenshot", "image", "hybrid"):
                import base64
                screenshots: list[str | None] = []
                for idx, slide_data in enumerate(layout.get("slides") or []):
                    clip_w = max(1.0, float(slide_data.get("width") or design_w_px))
                    clip_h = max(1.0, float(slide_data.get("height") or design_h_px))
                    screenshot_result = await send("Page.captureScreenshot", {
                        "format": "png",
                        "captureBeyondViewport": True,
                        "fromSurface": True,
                        "clip": {
                            "x": max(0.0, float(slide_data.get("x") or 0)),
                            "y": max(0.0, float(slide_data.get("y") or 0)),
                            "width": clip_w,
                            "height": clip_h,
                            "scale": 1,
                        },
                    })
                    data = screenshot_result.get("result", {}).get("data")
                    if not data:
                        screenshots.append(None)
                        continue
                    with tempfile.NamedTemporaryFile(delete=False, suffix=f"-slide-{idx + 1}.png") as image_tmp:
                        image_file = Path(image_tmp.name)
                    image_file.write_bytes(base64.b64decode(data))
                    screenshots.append(str(image_file))
                layout["screenshots"] = screenshots
            if layout and render_mode in ("editable", "hybrid_editable"):
                import base64
                background_screenshots: list[str | None] = []
                content_screenshots: list[str | None] = []
                shape_screenshots: dict[str, str] = {}
                page_bg_value = str(layout.get("pageBackground") or "")
                for idx, slide_data in enumerate(layout.get("slides") or []):
                    bg_value = str(slide_data.get("backgroundColor") or "")
                    root_w = max(1.0, float(slide_data.get("width") or design_w_px))
                    root_h = max(1.0, float(slide_data.get("height") or design_h_px))
                    root_is_full_canvas = root_w >= design_w_px * 0.98 and root_h >= design_h_px * 0.98
                    needs_bg = (
                        is_complex_css_paint(bg_value)
                        or is_translucent_css_color(bg_value)
                        or (is_complex_css_paint(page_bg_value) and not root_is_full_canvas)
                    )
                    if not needs_bg:
                        background_screenshots.append(None)
                    else:
                        hide_expr = (
                            "(() => {"
                            "const id='clawith-bg-capture-style';"
                            "document.getElementById(id)?.remove();"
                            "const style=document.createElement('style');"
                            "style.id=id;"
                            f"style.textContent='[data-clawith-slide-root=\"{idx}\"] > * {{ visibility: hidden !important; }}';"
                            "document.head.appendChild(style);"
                            "})()"
                        )
                        restore_expr = "document.getElementById('clawith-bg-capture-style')?.remove()"
                        await send("Runtime.evaluate", {"expression": hide_expr, "awaitPromise": True})
                        try:
                            screenshot_result = await send("Page.captureScreenshot", {
                                "format": "png",
                                "captureBeyondViewport": True,
                                "fromSurface": True,
                                "clip": {
                                    "x": max(0.0, float(slide_data.get("x") or 0)),
                                    "y": max(0.0, float(slide_data.get("y") or 0)),
                                    "width": root_w,
                                    "height": root_h,
                                    "scale": 1,
                                },
                            })
                        finally:
                            await send("Runtime.evaluate", {"expression": restore_expr})
                        data = screenshot_result.get("result", {}).get("data")
                        if not data:
                            background_screenshots.append(None)
                        else:
                            with tempfile.NamedTemporaryFile(
                                delete=False,
                                suffix=f"-slide-bg-{idx + 1}.png",
                            ) as image_tmp:
                                image_file = Path(image_tmp.name)
                            image_file.write_bytes(base64.b64decode(data))
                            background_screenshots.append(str(image_file))
                    # Root background capture temporarily hides direct
                    # children; after it is restored, item-level captures
                    # can preserve shadows/backdrop effects for cards.
                    if render_mode == "hybrid_editable":
                        text_item_ids = [
                            str(item.get("itemId"))
                            for item in slide_data.get("items") or []
                            if item.get("kind") == "text" and item.get("itemId")
                        ]
                        text_clip_item_ids = [
                            str(item.get("itemId"))
                            for item in slide_data.get("items") or []
                            if item.get("kind") == "text"
                            and item.get("itemId")
                            and (
                                "text"
                                in str(
                                    (item.get("style") or {}).get("backgroundClip")
                                    or (item.get("style") or {}).get(
                                        "webkitBackgroundClip"
                                    )
                                    or ""
                                ).lower()
                                or str(
                                    (item.get("style") or {}).get(
                                        "webkitTextFillColor"
                                    )
                                    or ""
                                ).lower()
                                in {"transparent", "rgba(0, 0, 0, 0)"}
                            )
                        ]
                        text_capture_css = build_hybrid_text_capture_css(
                            text_item_ids,
                            text_clip_item_ids=text_clip_item_ids,
                        )
                        hide_text_expr = (
                            "(() => {"
                            "const id='clawith-text-capture-style';"
                            "document.getElementById(id)?.remove();"
                            "const style=document.createElement('style');"
                            "style.id=id;"
                            f"style.textContent={json.dumps(text_capture_css)};"
                            "document.head.appendChild(style);"
                            "})()"
                        )
                        restore_text_expr = (
                            "document.getElementById('clawith-text-capture-style')?.remove()"
                        )
                        await send(
                            "Runtime.evaluate",
                            {"expression": hide_text_expr, "awaitPromise": True},
                        )
                        try:
                            screenshot_result = await send("Page.captureScreenshot", {
                                "format": "png",
                                "captureBeyondViewport": True,
                                "fromSurface": True,
                                "clip": {
                                    "x": max(0.0, float(slide_data.get("x") or 0)),
                                    "y": max(0.0, float(slide_data.get("y") or 0)),
                                    "width": root_w,
                                    "height": root_h,
                                    "scale": 1,
                                },
                            })
                        finally:
                            await send(
                                "Runtime.evaluate",
                                {"expression": restore_text_expr},
                            )
                        data = screenshot_result.get("result", {}).get("data")
                        if not data:
                            content_screenshots.append(None)
                        else:
                            with tempfile.NamedTemporaryFile(
                                delete=False,
                                suffix=f"-slide-content-{idx + 1}.png",
                            ) as image_tmp:
                                image_file = Path(image_tmp.name)
                            image_file.write_bytes(base64.b64decode(data))
                            content_screenshots.append(str(image_file))
                for slide_idx, slide_data in enumerate(layout.get("slides") or []):
                    for item in slide_data.get("items") or []:
                        if item.get("kind") != "shape":
                            continue
                        style = item.get("style") or {}
                        bg_value = str(style.get("backgroundImage") or "")
                        has_complex_paint = (
                            "gradient(" in bg_value
                            or "url(" in bg_value
                            or str(style.get("boxShadow") or "none") != "none"
                            or str(style.get("filter") or "none") != "none"
                            or str(style.get("backdropFilter") or "none") != "none"
                        )
                        if (
                            not has_complex_paint
                            and not item.get("requiresScreenshot")
                        ) or not item.get("itemId"):
                            continue
                        item_id = str(item["itemId"])
                        preserve_visual_children = bool(
                            item.get("requiresScreenshot")
                            and item.get("tag") in {"svg", "canvas"}
                        )
                        child_visibility_rule = (
                            (
                                f"[data-clawith-slide-root=\"{slide_idx}\"] "
                                f"[data-clawith-item-id=\"{item_id}\"] * {{ "
                                "visibility: visible !important; }"
                            )
                            if preserve_visual_children
                            else (
                                f"[data-clawith-slide-root=\"{slide_idx}\"] "
                                f"[data-clawith-item-id=\"{item_id}\"] * {{ "
                                "visibility: hidden !important; color: transparent !important; "
                                "-webkit-text-fill-color: transparent !important; "
                                "text-shadow: none !important; }"
                            )
                        )
                        clip_w = max(1.0, float(item.get("w") or 1))
                        clip_h = max(1.0, float(item.get("h") or 1))
                        hide_expr = (
                            "(() => {"
                            "const id='clawith-item-bg-capture-style';"
                            "document.getElementById(id)?.remove();"
                            "const style=document.createElement('style');"
                            "style.id=id;"
                            "style.textContent="
                            f"'[data-clawith-slide-root=\"{slide_idx}\"] * {{ visibility: hidden !important; }} "
                            f"[data-clawith-slide-root=\"{slide_idx}\"] [data-clawith-item-id=\"{item_id}\"] {{ visibility: visible !important; color: transparent !important; -webkit-text-fill-color: transparent !important; text-shadow: none !important; }} "
                            f"[data-clawith-slide-root=\"{slide_idx}\"] [data-clawith-item-id=\"{item_id}\"]::before, "
                            f"[data-clawith-slide-root=\"{slide_idx}\"] [data-clawith-item-id=\"{item_id}\"]::after {{ color: transparent !important; -webkit-text-fill-color: transparent !important; text-shadow: none !important; }} "
                            f"{child_visibility_rule}';"
                            "document.head.appendChild(style);"
                            "})()"
                        )
                        restore_expr = "document.getElementById('clawith-item-bg-capture-style')?.remove()"
                        await send("Runtime.evaluate", {"expression": hide_expr, "awaitPromise": True})
                        try:
                            screenshot_result = await send("Page.captureScreenshot", {
                                "format": "png",
                                "captureBeyondViewport": True,
                                "fromSurface": True,
                                "clip": {
                                    "x": max(0.0, float(slide_data.get("x") or 0) + float(item.get("x") or 0)),
                                    "y": max(0.0, float(slide_data.get("y") or 0) + float(item.get("y") or 0)),
                                    "width": clip_w,
                                    "height": clip_h,
                                    "scale": 1,
                                },
                            })
                        finally:
                            await send("Runtime.evaluate", {"expression": restore_expr})
                        data = screenshot_result.get("result", {}).get("data")
                        if not data:
                            continue
                        with tempfile.NamedTemporaryFile(delete=False, suffix=f"-item-bg-{item_id}.png") as image_tmp:
                            image_file = Path(image_tmp.name)
                        image_file.write_bytes(base64.b64decode(data))
                        shape_screenshots[item_id] = str(image_file)
                layout["backgroundScreenshots"] = background_screenshots
                layout["contentScreenshots"] = content_screenshots
                layout["shapeScreenshots"] = shape_screenshots
            return layout
    except Exception as layout_exc:
        logger.warning(f"Browser layout extraction failed, falling back to DOM flow conversion: {layout_exc}")
        return None
    finally:
        terminate_popen_process_group(proc)
        profile_dir.cleanup()
