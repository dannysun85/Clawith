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

# A deck may still contain editable narrative slides.  The threshold is the
# mean picture coverage across the whole deck, so the existing minimum image
# slide distribution remains part of the policy and text/data decks remain
# unaffected.
MINIMUM_PICTURE_COVERAGE_RATIO = 0.35


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
    "IMAGE_LED_BRIEF_KEYWORDS",
    "MINIMUM_PICTURE_COVERAGE_RATIO",
    "presentation_brief_is_image_led",
]
