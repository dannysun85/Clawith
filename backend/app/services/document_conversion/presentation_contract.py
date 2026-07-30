"""Deterministic source checks for presentation HTML conversion."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


_UNRESOLVED_PLACEHOLDER_PATTERNS = (
    re.compile(
        r"\[\s*(?:your\s+)?(?:brand|company|client|logo|name|title|"
        r"placeholder|insert|tbd|todo)\b[^\]]*\]",
        re.IGNORECASE,
    ),
    re.compile(r"(?:待替换|待补充|请填写|占位符)\s*(?:品牌|公司|客户|名称|标题|Logo)?"),
)
_RATING_GLYPHS = frozenset({"★", "☆", "⭐"})
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
    for image in soup.find_all("img"):
        image_src = str(image.get("src") or "").strip()
        if not image_src:
            sources.append("")
            continue
        parsed = urlparse(image_src)
        if parsed.scheme in {"data", "http", "https"} or parsed.netloc:
            continue
        if parsed.scheme and parsed.scheme != "file":
            continue
        sources.append(image_src)
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


def validate_presentation_visible_text(visible_text: str) -> None:
    """Validate copy extracted from a rendered presentation artifact."""

    failures: list[str] = []
    _validate_visible_text_policy(visible_text, failures=failures)
    if failures:
        raise ValueError(
            "Presentation visible content policy invalid: " + "; ".join(failures)
        )


def validate_browser_slide_text_bounds(
    layout: dict[str, Any],
    *,
    tolerance_px: float = 2.0,
) -> None:
    """Reject editable text that is clipped by a fixed presentation canvas."""

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
        if len(failures) >= 5:
            break
    if failures:
        raise ValueError(
            "Presentation browser layout contains clipped editable text: "
            + "; ".join(failures)
        )


def validate_browser_slide_visual_quality(
    layout: dict[str, Any],
    *,
    screenshot_key: str | None = None,
    tolerance_px: float = 2.0,
) -> dict[str, Any]:
    """Reject known commercial-render defects using measured browser geometry."""

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
            if (
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
    return {
        "version": 1,
        "gate": "presentation_render_quality",
        "status": "passed",
        "slide_count": len(slides),
        "checks": (
            "render_evidence",
            "canvas_bounds",
            "text_container_overflow",
            "title_orphan_line",
            "text_overlap",
        ),
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
    failures: list[str],
) -> None:
    """Validate page-level visual variety without prescribing a fixed template."""

    version = str(slide_spec.get("visual_plan_version") or "").strip()
    if not version:
        return
    if version != _ADAPTIVE_VISUAL_PLAN_VERSION:
        failures.append(
            "slide_spec.visual_plan_version must be adaptive-v1"
        )
        return

    raw_policy = slide_spec.get("visual_policy")
    if not isinstance(raw_policy, dict):
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
        maximum_allowed_reuse = max(2, math.ceil(page_count / minimum_distinct_images))
        if maximum_image_reuse > maximum_allowed_reuse:
            failures.append(
                "slide_spec.visual_policy.maximum_uses_per_image "
                f"must be at most {maximum_allowed_reuse}"
            )

    layout_sequence: list[str] = []
    image_uses: dict[str, int] = {}
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
    failures: list[str],
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
        failures=failures,
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
) -> None:
    """Reject incomplete or structurally invalid presentation source files."""

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
        failures=failures,
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
