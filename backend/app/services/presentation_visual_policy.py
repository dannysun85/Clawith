"""Shared visual policy primitives for image-led presentations.

The prompt builder, source contract, and final PPTX reconciliation must agree
on what counts as an image-led deck.  Keeping the classifier and the minimum
coverage threshold here prevents a sparse deck from passing one layer while
being rejected by another.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


IMAGE_LED_BRIEF_KEYWORDS = (
    "图文并茂",
    "图片",
    "照片",
    "摄影",
    "主视觉",
    "人物广告",
    "故事板",
    "商业风",
    "image-rich",
    "image rich",
    "photography",
    "photo-led",
    "photo led",
    "storyboard",
    "commercial visual",
)

# A commercial proposal often describes the visual work indirectly (for
# example, "上市方案" and "人物广告创意") instead of literally saying
# "图文并茂".  Treating those briefs as text-only was the reason a real
# product-launch benchmark could pass with a sparse, card-heavy deck.  These
# markers are intentionally combined: a generic product strategy memo should
# remain editable/text-led, while a product/brand brief that also asks for
# launch, advertising, packaging, visual, or channel-material work must enter
# the image-led contract.
_PRODUCT_MARKERS = (
    "产品",
    "商品",
    "新品",
    "保温杯",
    "product",
    "packaging",
    "merchandise",
)
_BRAND_MARKERS = ("品牌", "brand", "campaign", "营销")
_VISUAL_INTENT_MARKERS = (
    "上市",
    "发布",
    "广告",
    "主视觉",
    "视觉",
    "外观",
    "包装",
    "交互设计",
    "渠道物料",
    "人物",
    "真人",
    "模特",
    "故事板",
    "分镜",
    "launch",
    "advertising",
    "visual",
    "packaging",
    "campaign",
    "storyboard",
    "channel material",
)
_CLIENT_PROPOSAL_MARKERS = (
    "客户级",
    "客户提案",
    "client-ready",
    "customer proposal",
)
_NATIVE_ONLY_BRIEF_MARKERS = (
    "只使用ppt原生",
    "仅使用ppt原生",
    "只用ppt原生",
    "必须只使用ppt原生",
    "不要调用任何图片",
    "不调用图片",
    "禁止调用图片",
    "严禁调用图片",
    "不得调用图片",
    "不要用图片",
    "不使用图片生成",
    "禁止使用图片生成",
    "严禁使用图片生成",
    "无需图片",
    "不需要图片",
    "只做原生矢量",
    "仅做原生矢量",
    "只用原生 shape",
    "native shapes only",
    "ppt native shapes only",
    "only native vector shapes",
)


def _brief_contains_marker(brief: str, marker: str) -> bool:
    """Match intent markers without making Chinese spacing significant.

    The deliverable form preserves user-authored spacing, so briefs such as
    ``只使用 PPT 原生文字`` must match the same native-only contract as
    ``只使用PPT原生文字``.  English markers still use their ordinary form.
    """

    if marker in brief:
        return True
    compact_marker = "".join(marker.split())
    return bool(compact_marker) and compact_marker in "".join(brief.split())

# A deck may still contain editable narrative slides.  The threshold is the
# mean picture coverage across the whole deck, so the existing minimum image
# slide distribution remains part of the policy and text/data decks remain
# unaffected.
MINIMUM_PICTURE_COVERAGE_RATIO = 0.35

# FR-P4: the v2 deck quality gates (font floor, information density band,
# contrast floor, editability/fact policies) are server-owned numbers carried
# by the visual policy — structural contract parameters, not prompt
# conventions.  ``_presentation_visual_policy`` only attaches them to v2
# requests; v1 slide_specs never see these keys and keep the v1 contract.
DECK_QUALITY_POLICY_VERSION = "adaptive-v1"


def deck_quality_policy() -> dict[str, float | int | str]:
    """Server-owned v2 deck quality parameters (defaults; page-count stable)."""

    return {
        "minimum_body_font_size_px": 16,
        "minimum_metadata_font_size_px": 10,
        # The density band fixes the observed "information density too low"
        # defect without punishing title/divider slides: the lower bound is a
        # deck-wide mean, the upper bounds are per-slide hard stops.
        "minimum_mean_text_chars_per_slide": 120,
        "maximum_text_chars_per_slide": 900,
        "maximum_shapes_per_slide": 40,
        "minimum_contrast_ratio": 4.5,
        # A full-bleed visual may be used as a background on a data slide, but
        # it must be accompanied by a material native/editable composition.
        # This distinguishes a hybrid slide from a single raster screenshot
        # with a token text label placed on top.
        "maximum_raster_only_picture_coverage_ratio": 0.9,
        "minimum_editable_shapes_on_picture_covered_data_slide": 4,
        "data_slide_editability": "editable_required",
        "image_slide_fact_policy": "no_fact_assertions",
    }


def presentation_brief_is_image_led(
    goal: str,
    spec: Mapping[str, Any] | None,
) -> bool:
    brief = " ".join(
        (
            str(goal or ""),
            json.dumps(dict(spec or {}), ensure_ascii=False, sort_keys=True),
        )
    ).casefold()
    # Negative intent is authoritative. A native/editable deck often names
    # images only to prohibit them (for example, "不要调用任何图片生成工具").
    # Keyword matching must not turn that explicit constraint into a paid
    # image-generation contract.
    if any(_brief_contains_marker(brief, marker) for marker in _NATIVE_ONLY_BRIEF_MARKERS):
        return False
    if any(keyword in brief for keyword in IMAGE_LED_BRIEF_KEYWORDS):
        return True

    has_product = any(marker in brief for marker in _PRODUCT_MARKERS)
    has_brand = any(marker in brief for marker in _BRAND_MARKERS)
    has_visual_intent = any(marker in brief for marker in _VISUAL_INTENT_MARKERS)
    if (has_product or has_brand) and has_visual_intent:
        return True

    # A client/customer proposal is not automatically image-led.  It becomes
    # image-led when the brief also describes a visual/launch deliverable;
    # this keeps ordinary board/investor decks on the editable text/data path.
    has_client_proposal = any(marker in brief for marker in _CLIENT_PROPOSAL_MARKERS)
    return has_client_proposal and has_visual_intent


__all__ = [
    "DECK_QUALITY_POLICY_VERSION",
    "IMAGE_LED_BRIEF_KEYWORDS",
    "MINIMUM_PICTURE_COVERAGE_RATIO",
    "deck_quality_policy",
    "presentation_brief_is_image_led",
]
