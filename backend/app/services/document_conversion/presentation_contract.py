"""Deterministic source checks for presentation HTML conversion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from app.services.presentation_visual_policy import MINIMUM_PICTURE_COVERAGE_RATIO
from app.services.source_inventory import (
    SourceInventoryEntry,
    reconcile_slide_semantics,
)


_UNRESOLVED_PLACEHOLDER_PATTERNS = (
    re.compile(
        r"\[\s*(?:your\s+)?(?:brand|company|client|logo|name|title|"
        r"placeholder|insert|tbd|todo)\b[^\]]*\]",
        re.IGNORECASE,
    ),
    re.compile(r"(?:待替换|待补充|请填写|占位符)\s*(?:品牌|公司|客户|名称|标题|Logo)?"),
)
_RATING_GLYPHS = frozenset({"★", "☆", "⭐"})
_VISIBLE_SERIALIZATION_ESCAPE_PATTERN = re.compile(
    r"(?<!\\)\\(?:r\\n|[nr])"
)
_MINIMUM_BODY_FONT_SIZE_PX = 16.0
_MINIMUM_METADATA_FONT_SIZE_PX = 10.0
_METADATA_TEXT_ROLE = "metadata"
_ADAPTIVE_VISUAL_PLAN_VERSION = "adaptive-v1"
_IMAGE_VISUAL_KINDS = frozenset({"generated_image", "supplied_image"})
_EDITABLE_VISUAL_KINDS = frozenset(
    {
        "editable_chart",
        "editable_diagram",
        "editable_table",
        "editable_typography",
    }
)
_VISUAL_KINDS = _IMAGE_VISUAL_KINDS | _EDITABLE_VISUAL_KINDS
_CSS_URL_PATTERN = re.compile(
    r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)]*?))\s*\)",
    re.IGNORECASE,
)
_CSS_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_IMAGE_DECLARATION_PATTERN = re.compile(
    r"(?:^|[;{])\s*"
    r"(?:background(?:-image)?|border-image(?:-source)?|content|"
    r"list-style-image|mask(?:-image)?)\s*:\s*([^;}]+)",
    re.IGNORECASE,
)


class PresentationVisualQualityError(ValueError):
    """Deterministic rendered-layout quality failure with a machine receipt."""

    code = "presentation_visual_quality_failed"

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.receipt = {
            "version": 1,
            "gate": "presentation_render_quality",
            "status": "failed",
            "failure_count": len(failures),
            "failures": failures[:20],
            "scope_guard": {
                "may_expand_user_edit_scope": False,
                "action": "request_user_approval",
            },
        }
        summary = "; ".join(
            str(item.get("message") or item.get("code") or "render defect")
            for item in failures[:5]
        )
        super().__init__(
            "Presentation rendered visual quality failed: "
            f"{summary}. Do not change content or styles outside the user's "
            "explicitly authorized edit scope to pass this gate; request user "
            "approval when remediation requires a broader change."
        )


def local_image_sources(html: str) -> list[str]:
    """Return workspace-backed image references declared by presentation HTML."""

    soup = BeautifulSoup(html, "html.parser")
    sources: list[str] = []
    seen: set[str] = set()

    def append_local_source(value: Any, *, preserve_empty: bool = False) -> None:
        image_src = str(value or "").strip()
        if not image_src:
            if preserve_empty and "" not in seen:
                sources.append("")
                seen.add("")
            return
        if image_src.startswith("#"):
            return
        parsed = urlparse(image_src)
        if parsed.scheme in {"data", "http", "https"} or parsed.netloc:
            return
        if parsed.scheme and parsed.scheme != "file":
            return
        if image_src not in seen:
            sources.append(image_src)
            seen.add(image_src)

    for image in soup.find_all("img"):
        append_local_source(image.get("src"), preserve_empty=True)

    css_sources = [style.get_text("\n") for style in soup.find_all("style")]
    css_sources.extend(
        str(element.get("style") or "") for element in soup.find_all(style=True)
    )
    for css in css_sources:
        without_comments = _CSS_COMMENT_PATTERN.sub("", css)
        for declaration in _CSS_IMAGE_DECLARATION_PATTERN.finditer(without_comments):
            for match in _CSS_URL_PATTERN.finditer(declaration.group(1)):
                append_local_source(
                    next(group for group in match.groups() if group is not None)
                )
    return sources


def _local_image_path(src_file: Path, image_src: str) -> Path | None:
    normalized = image_src.strip()
    if not normalized:
        return None
    if normalized.startswith(("data:", "http://", "https://")):
        return None
    parsed = urlparse(normalized)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    if parsed.scheme:
        return None
    path = Path(unquote(parsed.path))
    return path if path.is_absolute() else src_file.parent / path


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _required_text(value: Any, *, field: str, failures: list[str]) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        failures.append(f"{field} must be non-empty")
    return normalized


def _compact_visible_title(value: str) -> str:
    """Ignore markup-induced whitespace while preserving visible characters."""

    return re.sub(r"\s+", "", value)


def _css_pixel_value(value: Any) -> float | None:
    """Return a finite computed CSS pixel value without guessing other units."""

    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))px\s*",
        str(value or ""),
        re.IGNORECASE,
    )
    if match is None:
        return None
    parsed = float(match.group(1))
    return parsed if math.isfinite(parsed) else None


def _visual_node_has_substance(node: Any) -> bool:
    """Reject labels/placeholders that merely claim a visual was implemented."""

    media_tags = {"img", "svg", "canvas", "video"}
    if str(getattr(node, "name", "") or "").lower() in media_tags:
        return True
    if node.find(list(media_tags)) is not None:
        return True
    inline_style = str(node.get("style") or "").lower()
    if "background-image" in inline_style and "url(" in inline_style:
        return True

    meaningful_descendants = 0
    for descendant in node.find_all(True):
        if str(getattr(descendant, "name", "") or "").lower() in media_tags:
            return True
        descendant_style = str(descendant.get("style") or "").lower()
        if "background-image" in descendant_style and "url(" in descendant_style:
            return True
        if descendant.get_text(" ", strip=True):
            meaningful_descendants += 1
            if meaningful_descendants >= 2:
                return True
    return False


def _validate_visible_text_policy(
    visible_text: str,
    *,
    failures: list[str],
) -> None:
    """Reject release-blocking copy artifacts that prompts cannot reliably prevent."""

    for pattern in _UNRESOLVED_PLACEHOLDER_PATTERNS:
        match = pattern.search(visible_text)
        if match:
            failures.append(
                f"unresolved visible placeholder: {match.group(0).strip()}"
            )
            break

    rating_glyphs = sorted(set(visible_text) & _RATING_GLYPHS)
    if rating_glyphs:
        failures.append(
            "unsupported visible rating glyphs: " + " ".join(rating_glyphs)
        )

    serialization_escape = _VISIBLE_SERIALIZATION_ESCAPE_PATTERN.search(visible_text)
    if serialization_escape:
        failures.append(
            "visible serialization escape sequence: "
            + serialization_escape.group(0)
        )


def validate_presentation_visible_text(visible_text: str) -> None:
    """Validate copy extracted from a rendered presentation artifact."""

    failures: list[str] = []
    _validate_visible_text_policy(visible_text, failures=failures)
    if failures:
        raise ValueError(
            "Presentation visible content policy invalid: " + "; ".join(failures)
        )


def _css_color_rgba(value: Any) -> tuple[int, int, int, float] | None:
    """Parse a computed CSS color without discarding its alpha channel."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", normalized)
    if match:
        raw = match.group(1)
        if len(raw) == 3:
            raw = "".join(character * 2 for character in raw)
        red, green, blue = (int(raw[index : index + 2], 16) for index in (0, 2, 4))
        return red, green, blue, 1.0
    match = re.fullmatch(
        r"(rgb|rgba)\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})"
        r"\s*(?:,\s*([\d.]+)\s*)?\)",
        normalized,
    )
    if match:
        channels = tuple(int(match.group(index)) for index in (2, 3, 4))
        alpha = float(match.group(5)) if match.group(5) is not None else 1.0
        if all(channel <= 255 for channel in channels) and 0 <= alpha <= 1:
            return channels[0], channels[1], channels[2], alpha
    return None


def _css_color_rgb(
    value: Any,
    *,
    backdrop: tuple[int, int, int] | None = None,
) -> tuple[int, int, int] | None:
    """Resolve a computed CSS color against a known solid backdrop.

    Chromium reports an element without its own background as
    ``rgba(0, 0, 0, 0)``. Treating those RGB channels as opaque black creates
    false contrast failures. Preserving alpha also prevents transparent text
    from bypassing the quality gate.
    """

    parsed = _css_color_rgba(value)
    if parsed is None:
        return None
    red, green, blue, alpha = parsed
    if alpha >= 0.999:
        return red, green, blue
    if backdrop is None:
        return None
    return tuple(
        round(channel * alpha + backdrop_channel * (1 - alpha))
        for channel, backdrop_channel in zip((red, green, blue), backdrop)
    )  # type: ignore[return-value]


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        scaled = value / 255
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue = rgb
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast_ratio(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    lighter = max(_relative_luminance(foreground), _relative_luminance(background))
    darker = min(_relative_luminance(foreground), _relative_luminance(background))
    return round((lighter + 0.05) / (darker + 0.05), 4)


def _text_contrast_failure(
    item: Any,
    slide: Any,
    *,
    minimum_contrast_ratio: float,
) -> float | None:
    """Return the measured ratio when it violates the floor, else ``None``.

    Unparseable or missing colors carry no evidence and are skipped rather
    than guessed.
    """

    style = item.get("style") or {}
    slide_background = _css_color_rgb(slide.get("backgroundColor")) or (
        255,
        255,
        255,
    )
    background = (
        _css_color_rgb(
            style.get("backgroundColor"),
            backdrop=slide_background,
        )
        or slide_background
    )
    foreground = _css_color_rgb(style.get("color"), backdrop=background)
    if foreground is None:
        return None
    ratio = contrast_ratio(foreground, background)
    if ratio + 1e-9 < minimum_contrast_ratio:
        return ratio
    return None


def validate_browser_slide_text_bounds(
    layout: dict[str, Any],
    *,
    tolerance_px: float = 2.0,
    minimum_body_font_size_px: float | None = None,
    minimum_metadata_font_size_px: float | None = None,
    minimum_contrast_ratio: float | None = None,
) -> None:
    """Reject editable text that is clipped by a fixed presentation canvas.

    FR-P4: the v2 deck policy can additionally parameterize font-size floors
    and a contrast floor.  Defaults keep the historical bounds-only behavior
    so v1 decks and unmanaged callers are unchanged.
    """

    failures: list[str] = []
    for slide_index, slide in enumerate(layout.get("slides") or [], start=1):
        width = float(slide.get("width") or 0)
        height = float(slide.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        for item in slide.get("items") or []:
            if item.get("kind") != "text" or not str(item.get("text") or "").strip():
                continue
            x = float(item.get("x") or 0)
            y = float(item.get("y") or 0)
            item_width = float(item.get("w") or 0)
            item_height = float(item.get("h") or 0)
            if (
                x < -tolerance_px
                or y < -tolerance_px
                or x + item_width > width + tolerance_px
                or y + item_height > height + tolerance_px
            ):
                excerpt = " ".join(str(item.get("text") or "").split())[:60]
                failures.append(
                    f"slide {slide_index} text exceeds canvas: {excerpt}"
                )
                if len(failures) >= 5:
                    break
            excerpt = " ".join(str(item.get("text") or "").split())[:60]
            style = item.get("style") or {}
            font_size_px = _css_pixel_value(style.get("fontSize"))
            if font_size_px is not None and (
                minimum_body_font_size_px is not None
                or minimum_metadata_font_size_px is not None
            ):
                is_metadata = (
                    str(item.get("textRole") or "").strip().lower()
                    == _METADATA_TEXT_ROLE
                )
                floor = (
                    minimum_metadata_font_size_px
                    if is_metadata and minimum_metadata_font_size_px is not None
                    else minimum_body_font_size_px
                )
                if floor is not None and font_size_px + 0.01 < floor:
                    failures.append(
                        f"slide {slide_index} text is {font_size_px:g}px; "
                        f"minimum is {floor:g}px: {excerpt}"
                    )
            if minimum_contrast_ratio is not None:
                ratio = _text_contrast_failure(
                    item,
                    slide,
                    minimum_contrast_ratio=minimum_contrast_ratio,
                )
                if ratio is not None:
                    failures.append(
                        f"slide {slide_index} text contrast {ratio:g}:1 below "
                        f"{minimum_contrast_ratio:g}:1: {excerpt}"
                    )
            if len(failures) >= 5:
                break
        if len(failures) >= 5:
            break
    if failures:
        raise ValueError(
            "Presentation browser layout contains clipped or unreadable editable text: "
            + "; ".join(failures)
        )


def validate_browser_slide_visual_quality(
    layout: dict[str, Any],
    *,
    screenshot_key: str | None = None,
    tolerance_px: float = 2.0,
    minimum_body_font_size_px: float | None = None,
    minimum_metadata_font_size_px: float | None = None,
    minimum_contrast_ratio: float | None = None,
) -> dict[str, Any]:
    """Reject known commercial-render defects using measured browser geometry.

    FR-P4: v2 decks parameterize the font-size floors and add a contrast
    floor through the server-owned visual policy; defaults preserve the v1
    behavior exactly.
    """

    body_font_floor = (
        _MINIMUM_BODY_FONT_SIZE_PX
        if minimum_body_font_size_px is None
        else float(minimum_body_font_size_px)
    )
    metadata_font_floor = (
        _MINIMUM_METADATA_FONT_SIZE_PX
        if minimum_metadata_font_size_px is None
        else float(minimum_metadata_font_size_px)
    )

    failures: list[dict[str, Any]] = []
    slides = list(layout.get("slides") or [])
    if not slides:
        failures.append(
            {
                "code": "render_layout_missing",
                "message": "browser render produced no measurable slides",
            }
        )

    if screenshot_key is not None:
        screenshots = list(layout.get(screenshot_key) or [])
        if len(screenshots) != len(slides) or any(not item for item in screenshots):
            failures.append(
                {
                    "code": "render_evidence_incomplete",
                    "message": (
                        f"{screenshot_key} must contain one rendered image per slide"
                    ),
                }
            )

    for slide_index, slide in enumerate(slides, start=1):
        width = float(slide.get("width") or 0)
        height = float(slide.get("height") or 0)
        text_items = [
            item
            for item in (slide.get("items") or [])
            if item.get("kind") == "text"
            and str(item.get("text") or "").strip()
        ]
        for item_index, item in enumerate(text_items, start=1):
            text = " ".join(str(item.get("text") or "").split())
            x = float(item.get("x") or 0)
            y = float(item.get("y") or 0)
            item_width = float(item.get("w") or 0)
            item_height = float(item.get("h") or 0)
            style = item.get("style") or {}
            font_size_px = _css_pixel_value(style.get("fontSize"))
            text_role = str(item.get("textRole") or "").strip().lower()
            is_edge_metadata = (
                text_role == _METADATA_TEXT_ROLE
                and len(text) <= 40
                and height > 0
                and (
                    y <= max(80.0, height * 0.13)
                    or y + item_height >= height - max(64.0, height * 0.10)
                )
            )
            if text_role == _METADATA_TEXT_ROLE and not is_edge_metadata:
                failures.append(
                    {
                        "code": "invalid_metadata_text_role",
                        "slide": slide_index,
                        "item": item_index,
                        "excerpt": text[:80],
                        "message": (
                            f"slide {slide_index} metadata text role is only allowed "
                            f"for short edge labels: {text[:60]}"
                        ),
                    }
                )
            if font_size_px is not None:
                minimum_font_size_px = (
                    metadata_font_floor
                    if is_edge_metadata
                    else body_font_floor
                )
                if font_size_px + 0.01 < minimum_font_size_px:
                    failures.append(
                        {
                            "code": "text_too_small",
                            "slide": slide_index,
                            "item": item_index,
                            "excerpt": text[:80],
                            "font_size_px": round(font_size_px, 3),
                            "minimum_font_size_px": minimum_font_size_px,
                            "message": (
                                f"slide {slide_index} text is {font_size_px:g}px; "
                                f"minimum is {minimum_font_size_px:g}px: {text[:60]}"
                            ),
                        }
                    )
            if minimum_contrast_ratio is not None:
                observed_ratio = _text_contrast_failure(
                    item,
                    slide,
                    minimum_contrast_ratio=float(minimum_contrast_ratio),
                )
                if observed_ratio is not None:
                    failures.append(
                        {
                            "code": "text_contrast_below_minimum",
                            "slide": slide_index,
                            "item": item_index,
                            "excerpt": text[:80],
                            "contrast_ratio": observed_ratio,
                            "minimum_contrast_ratio": float(minimum_contrast_ratio),
                            "message": (
                                f"slide {slide_index} text contrast {observed_ratio:g}:1 is below "
                                f"{float(minimum_contrast_ratio):g}:1: {text[:60]}"
                            ),
                        }
                    )
            if (
                width > 0
                and height > 0
                and (
                    x < -tolerance_px
                    or y < -tolerance_px
                    or x + item_width > width + tolerance_px
                    or y + item_height > height + tolerance_px
                )
            ):
                failures.append(
                    {
                        "code": "text_exceeds_canvas",
                        "slide": slide_index,
                        "item": item_index,
                        "excerpt": text[:80],
                        "message": f"slide {slide_index} text exceeds canvas: {text[:60]}",
                    }
                )

            scroll_width = float(item.get("scrollWidth") or 0)
            scroll_height = float(item.get("scrollHeight") or 0)
            client_width = float(item.get("clientWidth") or item_width)
            client_height = float(item.get("clientHeight") or item_height)
            overflow_values = {
                str((style or {}).get(name) or "").strip().lower()
                for name in ("overflow", "overflowX", "overflowY")
            }
            # Chromium can report extra scroll height for an auto-sized text
            # element because glyph line boxes extend beyond the used line
            # height. That is not clipping when the element's own overflow is
            # visible (the default). Only treat the measurement as a defect
            # when the text container actually clips or scrolls its contents.
            clips_text = style is None or bool(
                overflow_values & {"hidden", "clip", "auto", "scroll"}
            )
            if clips_text and (
                scroll_width > client_width + tolerance_px
                or scroll_height > client_height + tolerance_px
            ):
                failures.append(
                    {
                        "code": "text_container_overflow",
                        "slide": slide_index,
                        "item": item_index,
                        "excerpt": text[:80],
                        "message": f"slide {slide_index} text container overflows: {text[:60]}",
                    }
                )

            tag = str(item.get("tag") or "").lower()
            lines = [
                line
                for line in (item.get("lines") or [])
                if str(line.get("text") or "").strip()
            ]
            closing_punctuation = "，。；：！？、）】》」』,.!?;:)\u201d\u2019"
            for line_number, line in enumerate(lines, start=1):
                line_text = str(line.get("text") or "").strip()
                if line_text and line_text[0] in closing_punctuation:
                    failures.append(
                        {
                            "code": "line_start_punctuation",
                            "slide": slide_index,
                            "item": item_index,
                            "line": line_number,
                            "excerpt": text[:80],
                            "message": (
                                f"slide {slide_index} line {line_number} starts with "
                                f"closing punctuation: {line_text[:60]}"
                            ),
                        }
                    )
            if tag in {"h1", "h2", "h3"} and len(lines) >= 2:
                compact_text = _compact_visible_title(text)
                final_line = _compact_visible_title(str(lines[-1].get("text") or ""))
                if len(compact_text) >= 8 and 0 < len(final_line) <= 2:
                    failures.append(
                        {
                            "code": "title_orphan_line",
                            "slide": slide_index,
                            "item": item_index,
                            "excerpt": text[:80],
                            "message": (
                                f"slide {slide_index} title leaves an orphan final line: "
                                f"{final_line}"
                            ),
                        }
                    )

        for left_index, left in enumerate(text_items):
            if left.get("allowOverlap"):
                continue
            left_x = float(left.get("x") or 0)
            left_y = float(left.get("y") or 0)
            left_w = max(0.0, float(left.get("w") or 0))
            left_h = max(0.0, float(left.get("h") or 0))
            left_area = left_w * left_h
            if left_area <= 0:
                continue
            for right in text_items[left_index + 1 :]:
                if right.get("allowOverlap"):
                    continue
                right_x = float(right.get("x") or 0)
                right_y = float(right.get("y") or 0)
                right_w = max(0.0, float(right.get("w") or 0))
                right_h = max(0.0, float(right.get("h") or 0))
                right_area = right_w * right_h
                if right_area <= 0:
                    continue
                overlap_w = max(
                    0.0,
                    min(left_x + left_w, right_x + right_w) - max(left_x, right_x),
                )
                overlap_h = max(
                    0.0,
                    min(left_y + left_h, right_y + right_h) - max(left_y, right_y),
                )
                overlap_area = overlap_w * overlap_h
                if (
                    overlap_w > tolerance_px
                    and overlap_h > tolerance_px
                    and overlap_area / min(left_area, right_area) >= 0.2
                ):
                    left_text = " ".join(str(left.get("text") or "").split())
                    right_text = " ".join(str(right.get("text") or "").split())
                    failures.append(
                        {
                            "code": "text_overlap",
                            "slide": slide_index,
                            "excerpt": f"{left_text[:35]} | {right_text[:35]}",
                            "message": (
                                f"slide {slide_index} editable text overlaps: "
                                f"{left_text[:28]} / {right_text[:28]}"
                            ),
                        }
                    )
                    if len(failures) >= 20:
                        break
            if len(failures) >= 20:
                break
        if len(failures) >= 20:
            break

    if failures:
        raise PresentationVisualQualityError(failures)
    checks = (
        "render_evidence",
        "canvas_bounds",
        "minimum_readable_font_size",
        "text_container_overflow",
        "line_start_punctuation",
        "title_orphan_line",
        "text_overlap",
    )
    if minimum_contrast_ratio is not None:
        checks = (*checks, "text_contrast")
    return {
        "version": 1,
        "gate": "presentation_render_quality",
        "status": "passed",
        "slide_count": len(slides),
        "checks": checks,
    }


def _required_text_list(value: Any, *, field: str, failures: list[str]) -> list[str]:
    if not isinstance(value, list):
        failures.append(f"{field} must be an array")
        return []
    normalized = [str(item or "").strip() for item in value]
    if any(not item for item in normalized):
        failures.append(f"{field} must contain only non-empty strings")
    return normalized


def _required_evidence(value: Any, *, field: str, failures: list[str]) -> list[str]:
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            failures.append(f"{field} must be non-empty text or an array")
            return []
        return [normalized]
    return _required_text_list(value, field=field, failures=failures)


def _required_source_refs(value: Any, *, field: str, failures: list[str]) -> list[str]:
    if not isinstance(value, list):
        failures.append(f"{field} must be an array")
        return []

    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            ref = item.strip()
        elif isinstance(item, dict):
            ref = str(item.get("ref") or "").strip()
        else:
            ref = ""
        if not ref:
            failures.append(
                f"{field} must contain only non-empty strings or objects with a non-empty ref"
            )
        normalized.append(ref)
    return normalized


def _required_int(
    value: Any,
    *,
    field: str,
    minimum: int,
    failures: list[str],
) -> int:
    if isinstance(value, bool):
        failures.append(f"{field} must be an integer")
        return minimum
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        failures.append(f"{field} must be an integer")
        return minimum
    if normalized < minimum:
        failures.append(f"{field} must be at least {minimum}")
    return normalized


def _required_float(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
    failures: list[str],
) -> float:
    if isinstance(value, bool):
        failures.append(f"{field} must be a number")
        return minimum
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        failures.append(f"{field} must be a number")
        return minimum
    if not math.isfinite(normalized):
        failures.append(f"{field} must be finite")
        return minimum
    if normalized < minimum or normalized > maximum:
        failures.append(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return normalized


def _normalize_local_asset_ref(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme or parsed.netloc:
        return normalized
    return unquote(parsed.path).replace("\\", "/").lstrip("./")


def _slide_local_image_sources(slide: Any) -> set[str]:
    sources: set[str] = set()
    for image in slide.find_all("img"):
        normalized = _normalize_local_asset_ref(image.get("src"))
        if normalized and not normalized.startswith(("data:", "http://", "https://")):
            sources.add(normalized)
    for node in (slide, *slide.find_all(True)):
        inline_style = str(node.get("style") or "")
        for match in re.finditer(r"background-image\s*:\s*url\(([^)]+)\)", inline_style, re.IGNORECASE):
            raw = match.group(1).strip().strip("\"'")
            normalized = _normalize_local_asset_ref(raw)
            if normalized and not normalized.startswith(("data:", "http://", "https://")):
                sources.add(normalized)
    return sources


def _validate_adaptive_visual_plan(
    *,
    slide_spec: dict[str, Any],
    spec_slides: list[dict[str, Any]],
    slides: list[Any],
    expected_page_count: int | None,
    required: bool,
    failures: list[str],
    source_inventory_entries: Sequence[Mapping[str, Any]] | None = None,
    semantic_gate: bool = False,
) -> None:
    """Validate page-level visual variety without prescribing a fixed template."""

    version = str(slide_spec.get("visual_plan_version") or "").strip()
    if not version:
        if required:
            failures.append(
                "slide_spec.visual_plan_version must be adaptive-v1 for managed deliverables"
            )
        return
    if version != _ADAPTIVE_VISUAL_PLAN_VERSION:
        failures.append(
            "slide_spec.visual_plan_version must be adaptive-v1"
        )
        return

    raw_policy = slide_spec.get("visual_policy")
    if not isinstance(raw_policy, dict):
        if required:
            failures.append(
                "slide_spec.visual_policy must be an object for managed deliverables"
            )
        failures.append("slide_spec.visual_policy must be an object")
        return
    page_count = expected_page_count or len(spec_slides)
    minimum_layouts = _required_int(
        raw_policy.get("minimum_distinct_layouts"),
        field="slide_spec.visual_policy.minimum_distinct_layouts",
        minimum=1,
        failures=failures,
    )
    minimum_distinct_images = _required_int(
        raw_policy.get("minimum_distinct_images"),
        field="slide_spec.visual_policy.minimum_distinct_images",
        minimum=0,
        failures=failures,
    )
    raw_minimum_image_slides = raw_policy.get("minimum_image_slides")
    if raw_minimum_image_slides is None and required:
        failures.append(
            "slide_spec.visual_policy.minimum_image_slides must be an integer "
            "for managed deliverables"
        )
        minimum_image_slides = 0
    elif raw_minimum_image_slides is None:
        # Legacy/provider-free fixtures may omit the newer distribution field;
        # production managed deliverables are required to include it above.
        minimum_image_slides = 0
    else:
        minimum_image_slides = _required_int(
            raw_minimum_image_slides,
            field="slide_spec.visual_policy.minimum_image_slides",
            minimum=0,
            failures=failures,
        )
    raw_minimum_picture_coverage = raw_policy.get(
        "minimum_picture_coverage_ratio"
    )
    coverage_policy_declared = raw_minimum_picture_coverage is not None
    if raw_minimum_picture_coverage is None:
        if required and minimum_distinct_images > 0:
            failures.append(
                "slide_spec.visual_policy.minimum_picture_coverage_ratio must be a number "
                "for managed image-led deliverables"
            )
        minimum_picture_coverage_ratio = 0.0
    else:
        minimum_picture_coverage_ratio = _required_float(
            raw_minimum_picture_coverage,
            field="slide_spec.visual_policy.minimum_picture_coverage_ratio",
            minimum=0.0,
            maximum=1.0,
            failures=failures,
        )
    if (
        minimum_distinct_images > 0
        and (coverage_policy_declared or required)
        and minimum_picture_coverage_ratio < MINIMUM_PICTURE_COVERAGE_RATIO
    ):
        failures.append(
            "slide_spec.visual_policy.minimum_picture_coverage_ratio "
            f"must be at least {MINIMUM_PICTURE_COVERAGE_RATIO:g} for image-led decks"
        )
    maximum_image_reuse = _required_int(
        raw_policy.get("maximum_uses_per_image"),
        field="slide_spec.visual_policy.maximum_uses_per_image",
        minimum=0,
        failures=failures,
    )
    minimum_editable_compositions = _required_int(
        raw_policy.get("minimum_editable_compositions"),
        field="slide_spec.visual_policy.minimum_editable_compositions",
        minimum=0,
        failures=failures,
    )
    # FR-P4 extended adaptive-v1 fields: only validated when present, so
    # legacy slide_specs without them keep the v1 contract untouched.
    if raw_policy.get("minimum_body_font_size_px") is not None:
        _required_float(
            raw_policy.get("minimum_body_font_size_px"),
            field="slide_spec.visual_policy.minimum_body_font_size_px",
            minimum=8.0,
            maximum=72.0,
            failures=failures,
        )
    if raw_policy.get("minimum_metadata_font_size_px") is not None:
        _required_float(
            raw_policy.get("minimum_metadata_font_size_px"),
            field="slide_spec.visual_policy.minimum_metadata_font_size_px",
            minimum=6.0,
            maximum=32.0,
            failures=failures,
        )
    if raw_policy.get("minimum_mean_text_chars_per_slide") is not None:
        _required_int(
            raw_policy.get("minimum_mean_text_chars_per_slide"),
            field="slide_spec.visual_policy.minimum_mean_text_chars_per_slide",
            minimum=0,
            failures=failures,
        )
    if raw_policy.get("maximum_text_chars_per_slide") is not None:
        _required_int(
            raw_policy.get("maximum_text_chars_per_slide"),
            field="slide_spec.visual_policy.maximum_text_chars_per_slide",
            minimum=1,
            failures=failures,
        )
    if raw_policy.get("maximum_shapes_per_slide") is not None:
        _required_int(
            raw_policy.get("maximum_shapes_per_slide"),
            field="slide_spec.visual_policy.maximum_shapes_per_slide",
            minimum=1,
            failures=failures,
        )
    if raw_policy.get("minimum_contrast_ratio") is not None:
        _required_float(
            raw_policy.get("minimum_contrast_ratio"),
            field="slide_spec.visual_policy.minimum_contrast_ratio",
            minimum=1.0,
            maximum=21.0,
            failures=failures,
        )
    if page_count >= 5:
        required_layouts = min(page_count, max(3, math.ceil(page_count / 2)))
        if minimum_layouts < required_layouts:
            failures.append(
                "slide_spec.visual_policy.minimum_distinct_layouts "
                f"must be at least {required_layouts} for a {page_count}-slide deck"
            )
        required_editable = max(1, page_count // 4)
        if minimum_editable_compositions < required_editable:
            failures.append(
                "slide_spec.visual_policy.minimum_editable_compositions "
                f"must be at least {required_editable} for a {page_count}-slide deck"
            )
    if minimum_distinct_images > 0:
        required_images = max(1, math.ceil(page_count / 3))
        if minimum_distinct_images < required_images:
            failures.append(
                "slide_spec.visual_policy.minimum_distinct_images "
                f"must be at least {required_images} when imagery is required"
            )
        required_image_slides = min(page_count, max(1, math.ceil(page_count / 2)))
        if minimum_image_slides < required_image_slides:
            failures.append(
                "slide_spec.visual_policy.minimum_image_slides "
                f"must be at least {required_image_slides} for a {page_count}-slide "
                "image-led deck"
            )
        maximum_allowed_reuse = max(2, math.ceil(page_count / minimum_distinct_images))
        if maximum_image_reuse > maximum_allowed_reuse:
            failures.append(
                "slide_spec.visual_policy.maximum_uses_per_image "
                f"must be at most {maximum_allowed_reuse}"
            )

    layout_sequence: list[str] = []
    image_uses: dict[str, int] = {}
    image_slide_count = 0
    editable_compositions = 0
    for index, (spec_slide, html_slide) in enumerate(
        zip(spec_slides, slides, strict=False),
        start=1,
    ):
        field_prefix = f"slide_spec.slides[{index}]"
        _required_text(
            spec_slide.get("slide_type"),
            field=f"{field_prefix}.slide_type",
            failures=failures,
        )
        visual_kind = _required_text(
            spec_slide.get("visual_kind"),
            field=f"{field_prefix}.visual_kind",
            failures=failures,
        )
        if visual_kind and visual_kind not in _VISUAL_KINDS:
            failures.append(
                f"{field_prefix}.visual_kind must be one of "
                + ", ".join(sorted(_VISUAL_KINDS))
            )
        layout = str(spec_slide.get("layout") or "").strip()
        layout_sequence.append(layout)
        if len(layout_sequence) >= 2 and layout_sequence[-1] == layout_sequence[-2]:
            failures.append(
                f"{field_prefix}.layout must not repeat the previous slide layout"
            )

        if visual_kind in _IMAGE_VISUAL_KINDS:
            image_slide_count += 1
            asset_ref = _required_text(
                spec_slide.get("asset_ref"),
                field=f"{field_prefix}.asset_ref",
                failures=failures,
            )
            normalized_ref = _normalize_local_asset_ref(asset_ref)
            if normalized_ref:
                image_uses[normalized_ref] = image_uses.get(normalized_ref, 0) + 1
                html_sources = _slide_local_image_sources(html_slide)
                if normalized_ref not in html_sources:
                    failures.append(
                        f"slide {spec_slide.get('slide_id') or index} does not render "
                        f"its declared asset_ref {asset_ref}"
                    )
        elif visual_kind in _EDITABLE_VISUAL_KINDS:
            editable_compositions += 1
            if str(spec_slide.get("asset_ref") or "").strip():
                failures.append(
                    f"{field_prefix}.asset_ref must be empty for {visual_kind}"
                )

    distinct_layouts = {layout for layout in layout_sequence if layout}
    if len(distinct_layouts) < minimum_layouts:
        failures.append(
            f"slide_spec uses {len(distinct_layouts)} distinct layouts; "
            f"visual_policy requires {minimum_layouts}"
        )
    if len(image_uses) < minimum_distinct_images:
        failures.append(
            f"slide_spec uses {len(image_uses)} distinct image assets; "
            f"visual_policy requires {minimum_distinct_images}"
        )
    if image_slide_count < minimum_image_slides:
        failures.append(
            f"slide_spec uses {image_slide_count} image slides; visual_policy "
            f"requires {minimum_image_slides}"
        )
    overused_assets = sorted(
        asset for asset, uses in image_uses.items() if uses > maximum_image_reuse
    )
    if maximum_image_reuse >= 0 and overused_assets:
        failures.append(
            "image assets exceed visual_policy.maximum_uses_per_image: "
            + ", ".join(overused_assets)
        )
    if editable_compositions < minimum_editable_compositions:
        failures.append(
            f"slide_spec uses {editable_compositions} editable visual compositions; "
            f"visual_policy requires {minimum_editable_compositions}"
        )

    if semantic_gate:
        # FR-P2/P5 hard gate (v2 only): every source_ref must resolve to the
        # registered, hash-bound inventory; every un-labelled fact assertion
        # must be traceable to it.  A violation fails the conversion before
        # any PPTX/PDF artifact can exist.
        entries: list[SourceInventoryEntry] = []
        for raw_entry in source_inventory_entries or ():
            if not isinstance(raw_entry, Mapping):
                continue
            try:
                entries.append(SourceInventoryEntry.model_validate(dict(raw_entry)))
            except ValueError:
                continue
        reconciliation = reconcile_slide_semantics(spec_slides, entries)
        for finding in reconciliation.findings[:10]:
            failures.append(f"semantic gate: {finding.message}")


def _planning_slides(
    value: Any,
    *,
    label: str,
    expected_page_count: int | None,
    required_text_fields: tuple[str, ...],
    failures: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        failures.append(f"{label}.slides must be an array")
        return [], []
    if expected_page_count is not None and len(value) != expected_page_count:
        failures.append(
            f"{label}.slides expected {expected_page_count} items, found {len(value)}"
        )

    slides: list[dict[str, Any]] = []
    slide_ids: list[str] = []
    for index, raw_slide in enumerate(value, start=1):
        if not isinstance(raw_slide, dict):
            failures.append(f"{label}.slides[{index}] must be an object")
            continue
        slide = raw_slide
        slide_id = _required_text(
            slide.get("slide_id"),
            field=f"{label}.slides[{index}].slide_id",
            failures=failures,
        )
        for field in required_text_fields:
            _required_text(
                slide.get(field),
                field=f"{label}.slides[{index}].{field}",
                failures=failures,
            )
        if label == "outline":
            _required_evidence(
                slide.get("evidence"),
                field=f"{label}.slides[{index}].evidence",
                failures=failures,
            )
        elif label == "slide_spec":
            _required_text_list(
                slide.get("body_points"),
                field=f"{label}.slides[{index}].body_points",
                failures=failures,
            )
            source_refs = _required_source_refs(
                slide.get("source_refs"),
                field=f"{label}.slides[{index}].source_refs",
                failures=failures,
            )
            if any(source_ref.startswith("builtin.") for source_ref in source_refs):
                failures.append(
                    f"{label}.slides[{index}].source_refs cannot cite an internal workflow id"
                )
        slides.append(slide)
        slide_ids.append(slide_id)
    if len(set(slide_ids)) != len(slide_ids):
        failures.append(f"{label}.slide_id values must be unique")
    return slides, slide_ids


def _validate_planning_contract(
    *,
    slides: list[Any],
    slide_ids: list[str],
    expected_page_count: int | None,
    outline_file: Path | None,
    slide_spec_file: Path | None,
    require_adaptive_visual_plan: bool,
    failures: list[str],
    source_inventory_entries: Sequence[Mapping[str, Any]] | None = None,
    semantic_gate: bool = False,
) -> None:
    if (outline_file is None) != (slide_spec_file is None):
        failures.append("outline_path and slide_spec_path must be provided together")
        return
    if outline_file is None or slide_spec_file is None:
        return

    try:
        outline = _read_json_object(outline_file, label="outline")
        slide_spec = _read_json_object(slide_spec_file, label="slide_spec")
    except ValueError as exc:
        failures.append(str(exc))
        return

    for field in ("deck_title", "audience", "core_message"):
        _required_text(outline.get(field), field=f"outline.{field}", failures=failures)
    outline_slides, outline_ids = _planning_slides(
        outline.get("slides"),
        label="outline",
        expected_page_count=expected_page_count,
        required_text_fields=("purpose", "headline", "visual_intent"),
        failures=failures,
    )
    spec_slides, spec_ids = _planning_slides(
        slide_spec.get("slides"),
        label="slide_spec",
        expected_page_count=expected_page_count,
        required_text_fields=("headline", "layout", "visual_asset"),
        failures=failures,
    )
    _validate_adaptive_visual_plan(
        slide_spec=slide_spec,
        spec_slides=spec_slides,
        slides=slides,
        expected_page_count=expected_page_count,
        required=require_adaptive_visual_plan,
        failures=failures,
        source_inventory_entries=source_inventory_entries,
        semantic_gate=semantic_gate,
    )
    if expected_page_count is not None and expected_page_count >= 5:
        layouts = {
            str(item.get("layout") or "").strip()
            for item in spec_slides
            if str(item.get("layout") or "").strip()
        }
        if len(layouts) < 3:
            failures.append("slide_spec must use at least 3 distinct layouts for decks of 5+ slides")

    if outline_ids != slide_ids:
        failures.append("outline.slide_id order must match HTML data-slide order")
    if spec_ids != slide_ids:
        failures.append("slide_spec.slide_id order must match HTML data-slide order")

    outline_by_id = {
        str(item.get("slide_id") or "").strip(): item for item in outline_slides
    }
    spec_by_id = {
        str(item.get("slide_id") or "").strip(): item for item in spec_slides
    }
    for slide, slide_id in zip(slides, slide_ids, strict=False):
        title_nodes = slide.select("[data-slide-title]")
        if len(title_nodes) != 1:
            failures.append(
                f"slide {slide_id or '<empty>'} must contain exactly one [data-slide-title]"
            )
            continue
        html_title = title_nodes[0].get_text(" ", strip=True)
        if not html_title:
            failures.append(f"slide {slide_id or '<empty>'} title must be non-empty")
        outline_title = str(outline_by_id.get(slide_id, {}).get("headline") or "").strip()
        spec_title = str(spec_by_id.get(slide_id, {}).get("headline") or "").strip()
        if outline_title and spec_title and outline_title != spec_title:
            failures.append(f"slide {slide_id} headline differs between outline and slide_spec")
        if (
            spec_title
            and html_title
            and _compact_visible_title(spec_title)
            != _compact_visible_title(html_title)
        ):
            failures.append(f"slide {slide_id} HTML title differs from slide_spec headline")
        expected_layout = str(spec_by_id.get(slide_id, {}).get("layout") or "").strip()
        html_layout = str(slide.get("data-layout") or "").strip()
        if expected_layout and html_layout != expected_layout:
            failures.append(f"slide {slide_id} data-layout differs from slide_spec layout")
        visual_nodes = slide.select("[data-visual]")
        if not visual_nodes:
            failures.append(
                f"slide {slide_id or '<empty>'} must implement its visual intent with [data-visual]"
            )
        elif not any(_visual_node_has_substance(node) for node in visual_nodes):
            failures.append(
                f"slide {slide_id or '<empty>'} [data-visual] must contain a real media asset "
                "or a multi-element editable composition"
            )


def validate_presentation_html_contract(
    src_file: Path,
    *,
    expected_page_count: int | None,
    outline_file: Path | None = None,
    slide_spec_file: Path | None = None,
    require_adaptive_visual_plan: bool = False,
    source_inventory_entries: Sequence[Mapping[str, Any]] | None = None,
    semantic_gate: bool = False,
) -> None:
    """Reject incomplete or structurally invalid presentation source files.

    ``semantic_gate``/``source_inventory_entries`` are the v2-only FR-P2
    seams; v1 callers never pass them and keep the historical contract.
    """

    if expected_page_count is not None and expected_page_count < 1:
        raise ValueError("expected_page_count must be at least 1")

    html = src_file.read_text(encoding="utf-8")
    lower = html.lower()
    failures: list[str] = []

    for tag in ("html", "body", "style"):
        if lower.count(f"<{tag}") != lower.count(f"</{tag}>"):
            failures.append(f"unclosed <{tag}> block")

    soup = BeautifulSoup(html, "html.parser")
    _validate_visible_text_policy(soup.get_text(" ", strip=True), failures=failures)
    slides = soup.select(".slide[data-slide]")
    if expected_page_count is not None and len(slides) != expected_page_count:
        failures.append(
            f"expected {expected_page_count} .slide[data-slide] nodes, found {len(slides)}"
        )

    slide_ids = [str(slide.get("data-slide") or "").strip() for slide in slides]
    if any(not slide_id for slide_id in slide_ids):
        failures.append("every slide must have a non-empty data-slide value")
    if len(set(slide_ids)) != len(slide_ids):
        failures.append("data-slide values must be unique")

    _validate_planning_contract(
        slides=slides,
        slide_ids=slide_ids,
        expected_page_count=expected_page_count,
        outline_file=outline_file,
        slide_spec_file=slide_spec_file,
        require_adaptive_visual_plan=require_adaptive_visual_plan,
        failures=failures,
        source_inventory_entries=source_inventory_entries,
        semantic_gate=semantic_gate,
    )

    missing_images: list[str] = []
    for image_src in local_image_sources(html):
        if not image_src:
            missing_images.append("<empty>")
            continue
        local_path = _local_image_path(src_file, image_src)
        if local_path is not None and not local_path.is_file():
            missing_images.append(image_src)
    if missing_images:
        failures.append(
            "missing local image files: " + ", ".join(sorted(set(missing_images)))
        )

    if failures:
        raise ValueError("Presentation HTML contract invalid: " + "; ".join(failures))
