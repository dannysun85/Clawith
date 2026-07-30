"""Provider-neutral evaluation primitives for commercial creative deliverables.

The generator intentionally creates an open set of briefs instead of optimizing
for a fixed prompt list. Provider and model identities are kept out of public
evaluation packages so that quality judgments can be performed blind.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import random
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


CreativeModality = Literal["image", "video", "presentation"]
EvaluationSplit = Literal["regression", "development", "holdout"]
EvaluationStatus = Literal["blocked", "incomplete", "scored"]


class CreativeScenario(BaseModel):
    """One provider-independent commercial creative brief."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    fingerprint: str
    split: EvaluationSplit
    modality: CreativeModality
    language: str
    industry: str
    subject: str
    objective: str
    channel: str
    audience: str
    style: str
    source_mode: str
    constraint_profile: str
    aspect_ratio: str
    brief: str
    requirements: tuple[str, ...]
    hard_gates: tuple[str, ...]
    quality_dimensions: tuple[str, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreativeEvaluationManifest(BaseModel):
    """Public suite content; holdout bodies are deliberately excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    seed: int
    public_scenarios: tuple[CreativeScenario, ...]
    holdout_count: int
    holdout_commitment_sha256: str
    coverage: dict[str, dict[str, int]]


class CreativeHoldoutSuite(BaseModel):
    """Restricted holdout payload stored separately from the public suite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0.0"
    seed: int
    commitment_sha256: str
    scenarios: tuple[CreativeScenario, ...]


class CreativeEvaluationBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: CreativeEvaluationManifest
    holdout: CreativeHoldoutSuite


class CandidateDescriptor(BaseModel):
    """Private candidate metadata used to construct a blind comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    artifact_ref: str
    provider: str
    model: str | None = None


class BlindCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    opaque_artifact_id: str


class BlindComparisonPackage(BaseModel):
    """Evaluator-facing package without provider, model, or source path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    modality: CreativeModality
    brief: str
    requirements: tuple[str, ...]
    hard_gates: tuple[str, ...]
    quality_dimensions: tuple[str, ...]
    candidates: tuple[BlindCandidate, ...]


class BlindComparisonKey(BaseModel):
    """Private key that can be joined after judgments are finalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    candidates: dict[str, CandidateDescriptor]


class HardGateObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool | None = None
    evidence: tuple[str, ...] = ()


class QualityDimensionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float | None = Field(default=None, ge=1, le=5)
    evidence: tuple[str, ...] = ()


class CreativeQualityEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    status: EvaluationStatus
    hard_gate_failures: tuple[str, ...]
    missing_hard_gates: tuple[str, ...]
    missing_dimensions: tuple[str, ...]
    weighted_score: float | None = Field(default=None, ge=0, le=100)
    commercially_usable: bool


_INDUSTRIES: tuple[tuple[str, str], ...] = (
    ("consumer_electronics", "便携智能硬件新品"),
    ("beauty", "敏感肌护理新品"),
    ("food_and_beverage", "低糖即饮饮品"),
    ("saas", "企业协作软件"),
    ("manufacturing", "工业检测设备"),
    ("education", "职业技能课程"),
    ("cross_border_ecommerce", "跨境家居用品"),
    ("professional_services", "企业合规咨询服务"),
)
_OBJECTIVES = (
    "product_launch",
    "conversion",
    "brand_awareness",
    "sales_enablement",
    "education",
    "retention",
)
_AUDIENCES = (
    "urban_professionals",
    "families",
    "enterprise_buyers",
    "channel_partners",
    "creators",
    "students",
)
_STYLES = (
    "premium_editorial",
    "clean_technical",
    "warm_lifestyle",
    "bold_social",
    "minimal_corporate",
    "playful_youth",
)
_SOURCE_MODES = (
    "text_only",
    "single_reference",
    "multi_asset",
    "structured_facts",
)
_CONSTRAINT_PROFILES = (
    "open_creative",
    "exact_copy",
    "brand_locked",
    "fact_constrained",
    "accessibility_sensitive",
)
_LANGUAGES = ("zh-CN", "en-US", "bilingual")
_CHANNELS: dict[CreativeModality, tuple[str, ...]] = {
    "image": ("social_feed", "ecommerce_detail", "outdoor", "sales_deck"),
    "video": ("short_video_ad", "ecommerce_video", "brand_story", "sales_demo"),
    "presentation": ("sales_pitch", "internal_review", "customer_proposal", "training"),
}
_ASPECT_RATIOS: dict[CreativeModality, tuple[str, ...]] = {
    "image": ("1:1", "4:5", "3:2", "9:16", "16:9"),
    "video": ("9:16", "16:9", "1:1"),
    "presentation": ("16:9", "4:3"),
}
_MODALITY_REQUIREMENTS: dict[CreativeModality, tuple[str, ...]] = {
    "image": (
        "主体、背景和文案层级必须服务于指定传播目标",
        "不得虚构未提供的产品事实、认证或价格",
        "参考素材存在时必须保持主体身份与关键外观一致",
        "输出应可直接用于指定渠道，不依赖默认整图模糊或遮挡修补",
    ),
    "video": (
        "开场必须在渠道允许的合理时间内建立主体、价值或叙事方向，不得只做无目的素材轮播",
        "镜头、动作、口型、字幕与音频应保持连续和可理解",
        "不得虚构未提供的产品事实、认证或价格",
        "输出不得带平台水印，时长与画幅必须符合渠道要求",
    ),
    "presentation": (
        "叙事必须围绕受众决策展开，而不是简单堆砌页面",
        "关键结论、数字和来源必须可追溯，不得虚构事实",
        "必须同时交付可编辑源文件和可预览版本",
        "页面不得出现溢出、截断、不可读小字或无意义重复布局",
    ),
}
_HARD_GATES: dict[CreativeModality, tuple[str, ...]] = {
    "image": (
        "artifact_decodable",
        "aspect_ratio_match",
        "fact_safety",
        "reference_identity_when_required",
        "no_unrequested_watermark",
    ),
    "video": (
        "artifact_decodable",
        "duration_and_aspect_match",
        "fact_safety",
        "audio_contract_match",
        "no_unrequested_watermark",
    ),
    "presentation": (
        "pptx_and_preview_valid",
        "page_count_and_aspect_match",
        "fact_safety",
        "no_text_overflow",
        "source_traceability",
        "editability",
    ),
}
_QUALITY_DIMENSIONS: dict[CreativeModality, tuple[str, ...]] = {
    "image": (
        "brief_adherence",
        "visual_hierarchy",
        "subject_quality",
        "brand_and_style_fit",
        "commercial_readiness",
    ),
    "video": (
        "brief_adherence",
        "story_and_pacing",
        "character_and_motion_consistency",
        "audio_visual_coherence",
        "commercial_readiness",
    ),
    "presentation": (
        "brief_adherence",
        "narrative_quality",
        "information_design",
        "visual_system_consistency",
        "commercial_readiness",
    ),
}


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _permuted(values: Sequence[Any], *, seed: int, salt: str) -> list[Any]:
    result = list(values)
    salt_seed = int(hashlib.sha256(f"{seed}:{salt}".encode()).hexdigest()[:16], 16)
    random.Random(salt_seed).shuffle(result)
    return result


def _balanced_value(values: Sequence[Any], *, index: int, seed: int, salt: str) -> Any:
    ordered = _permuted(values, seed=seed, salt=salt)
    return ordered[index % len(ordered)]


def _scenario_payload(
    *,
    modality: CreativeModality,
    index: int,
    seed: int,
) -> dict[str, Any]:
    industry, subject = _balanced_value(
        _INDUSTRIES,
        index=index,
        seed=seed,
        salt=f"{modality}:industry",
    )
    objective = _balanced_value(_OBJECTIVES, index=index, seed=seed, salt=f"{modality}:objective")
    channel = _balanced_value(_CHANNELS[modality], index=index, seed=seed, salt=f"{modality}:channel")
    audience = _balanced_value(_AUDIENCES, index=index, seed=seed, salt=f"{modality}:audience")
    style = _balanced_value(_STYLES, index=index, seed=seed, salt=f"{modality}:style")
    source_mode = _balanced_value(_SOURCE_MODES, index=index, seed=seed, salt=f"{modality}:source")
    constraint_profile = _balanced_value(
        _CONSTRAINT_PROFILES,
        index=index,
        seed=seed,
        salt=f"{modality}:constraints",
    )
    language = _balanced_value(_LANGUAGES, index=index, seed=seed, salt=f"{modality}:language")
    aspect_ratio = _balanced_value(
        _ASPECT_RATIOS[modality],
        index=index,
        seed=seed,
        salt=f"{modality}:aspect",
    )
    requirements = (
        f"面向 {audience}，用于 {channel}，目标为 {objective}",
        f"采用 {style} 方向，语言为 {language}，画幅为 {aspect_ratio}",
        f"输入模式为 {source_mode}，约束类型为 {constraint_profile}",
        *_MODALITY_REQUIREMENTS[modality],
    )
    modality_name = {"image": "图片", "video": "视频", "presentation": "演示文稿"}[modality]
    brief = (
        f"为{industry}领域的“{subject}”制作一份可商用的{modality_name}内容。"
        f"传播目标是 {objective}，目标受众是 {audience}，投放/使用渠道是 {channel}。"
        "允许在不改变事实与硬性约束的前提下自主选择创意表达，不限定固定模板或单一视觉模式。"
    )
    return {
        "modality": modality,
        "language": language,
        "industry": industry,
        "subject": subject,
        "objective": objective,
        "channel": channel,
        "audience": audience,
        "style": style,
        "source_mode": source_mode,
        "constraint_profile": constraint_profile,
        "aspect_ratio": aspect_ratio,
        "brief": brief,
        "requirements": requirements,
        "hard_gates": _HARD_GATES[modality],
        "quality_dimensions": _QUALITY_DIMENSIONS[modality],
        "metadata": {
            "source": "synthetic_open_scenario",
            "generator_index": index,
        },
    }


def _assign_splits(
    scenarios: Sequence[CreativeScenario],
    *,
    seed: int,
) -> tuple[CreativeScenario, ...]:
    result: list[CreativeScenario] = []
    for modality in ("image", "video", "presentation"):
        group = [scenario for scenario in scenarios if scenario.modality == modality]
        order = _permuted(range(len(group)), seed=seed, salt=f"{modality}:split")
        holdout_count = max(1, round(len(group) * 0.25)) if len(group) >= 3 else 0
        regression_count = max(1, round(len(group) * 0.15)) if len(group) >= 5 else 0
        holdout_indexes = set(order[:holdout_count])
        regression_indexes = set(order[holdout_count : holdout_count + regression_count])
        for index, scenario in enumerate(group):
            split: EvaluationSplit = "development"
            if index in holdout_indexes:
                split = "holdout"
            elif index in regression_indexes:
                split = "regression"
            result.append(scenario.model_copy(update={"split": split}))
    return tuple(sorted(result, key=lambda scenario: scenario.scenario_id))


def coverage_report(scenarios: Sequence[CreativeScenario]) -> dict[str, dict[str, int]]:
    """Return reader-friendly coverage counts for suite review."""

    axes = (
        "modality",
        "split",
        "language",
        "industry",
        "objective",
        "channel",
        "audience",
        "style",
        "source_mode",
        "constraint_profile",
        "aspect_ratio",
    )
    return {
        axis: dict(
            sorted(
                Counter(str(getattr(scenario, axis)) for scenario in scenarios).items()
            )
        )
        for axis in axes
    }


def generate_evaluation_bundle(
    *,
    seed: int,
    count: int = 24,
    modalities: Sequence[CreativeModality] = ("image", "video", "presentation"),
) -> CreativeEvaluationBundle:
    """Generate a reproducible open-scene suite with a separately committed holdout."""

    normalized_modalities = tuple(dict.fromkeys(modalities))
    if not normalized_modalities:
        raise ValueError("At least one modality is required.")
    if count < len(normalized_modalities) * 3:
        raise ValueError("Count must provide at least three scenarios per modality.")

    modality_order = _permuted(
        normalized_modalities,
        seed=seed,
        salt="modality-order",
    )
    modality_indexes = {modality: 0 for modality in normalized_modalities}
    scenarios: list[CreativeScenario] = []
    for global_index in range(count):
        modality = modality_order[global_index % len(modality_order)]
        modality_index = modality_indexes[modality]
        modality_indexes[modality] += 1
        payload = _scenario_payload(
            modality=modality,
            index=modality_index,
            seed=seed,
        )
        fingerprint = _sha256(payload)
        scenarios.append(
            CreativeScenario(
                scenario_id=f"creative-{modality}-{fingerprint[:12]}",
                fingerprint=fingerprint,
                split="development",
                **payload,
            )
        )

    if len({scenario.fingerprint for scenario in scenarios}) != len(scenarios):
        raise ValueError("Generated duplicate scenarios; change seed or reduce count.")

    split_scenarios = _assign_splits(scenarios, seed=seed)
    public_scenarios = tuple(
        scenario for scenario in split_scenarios if scenario.split != "holdout"
    )
    holdout_scenarios = tuple(
        scenario for scenario in split_scenarios if scenario.split == "holdout"
    )
    commitment = _sha256(
        [scenario.model_dump(mode="json") for scenario in holdout_scenarios]
    )
    manifest = CreativeEvaluationManifest(
        seed=seed,
        public_scenarios=public_scenarios,
        holdout_count=len(holdout_scenarios),
        holdout_commitment_sha256=commitment,
        coverage=coverage_report(split_scenarios),
    )
    holdout = CreativeHoldoutSuite(
        seed=seed,
        commitment_sha256=commitment,
        scenarios=holdout_scenarios,
    )
    return CreativeEvaluationBundle(manifest=manifest, holdout=holdout)


def verify_holdout_commitment(holdout: CreativeHoldoutSuite) -> bool:
    return holdout.commitment_sha256 == _sha256(
        [scenario.model_dump(mode="json") for scenario in holdout.scenarios]
    )


def create_blind_comparison(
    scenario: CreativeScenario,
    candidates: Sequence[CandidateDescriptor],
    *,
    seed: int,
) -> tuple[BlindComparisonPackage, BlindComparisonKey]:
    """Create a public blind package and a private provider mapping."""

    if len(candidates) < 2:
        raise ValueError("Blind comparison requires at least two candidates.")
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("Candidate ids must be unique.")

    shuffled = _permuted(candidates, seed=seed, salt=f"{scenario.scenario_id}:blind")
    public_candidates: list[BlindCandidate] = []
    private_candidates: dict[str, CandidateDescriptor] = {}
    for index, candidate in enumerate(shuffled):
        label = chr(ord("A") + index)
        opaque_id = hashlib.sha256(
            f"{seed}:{scenario.scenario_id}:{candidate.candidate_id}".encode()
        ).hexdigest()[:20]
        public_candidates.append(
            BlindCandidate(label=label, opaque_artifact_id=opaque_id)
        )
        private_candidates[label] = candidate
    package = BlindComparisonPackage(
        scenario_id=scenario.scenario_id,
        modality=scenario.modality,
        brief=scenario.brief,
        requirements=scenario.requirements,
        hard_gates=scenario.hard_gates,
        quality_dimensions=scenario.quality_dimensions,
        candidates=tuple(public_candidates),
    )
    key = BlindComparisonKey(
        scenario_id=scenario.scenario_id,
        candidates=private_candidates,
    )
    return package, key


def score_quality_evaluation(
    scenario: CreativeScenario,
    *,
    hard_gates: Mapping[str, HardGateObservation],
    dimensions: Mapping[str, QualityDimensionObservation],
    weights: Mapping[str, float] | None = None,
    commercial_threshold: float = 80,
) -> CreativeQualityEvaluation:
    """Score one artifact without treating missing evidence as a pass."""

    unexpected_gates = set(hard_gates) - set(scenario.hard_gates)
    unexpected_dimensions = set(dimensions) - set(scenario.quality_dimensions)
    if unexpected_gates:
        raise ValueError(f"Unexpected hard gates: {sorted(unexpected_gates)}")
    if unexpected_dimensions:
        raise ValueError(
            f"Unexpected quality dimensions: {sorted(unexpected_dimensions)}"
        )

    failures = tuple(
        gate
        for gate in scenario.hard_gates
        if hard_gates.get(gate) is not None
        and hard_gates[gate].passed is False
    )
    missing_gates = tuple(
        gate
        for gate in scenario.hard_gates
        if hard_gates.get(gate) is None or hard_gates[gate].passed is None
    )
    missing_dimensions = tuple(
        dimension
        for dimension in scenario.quality_dimensions
        if dimensions.get(dimension) is None
        or dimensions[dimension].score is None
    )

    if failures:
        status: EvaluationStatus = "blocked"
    elif missing_gates or missing_dimensions:
        status = "incomplete"
    else:
        status = "scored"

    weighted_score: float | None = None
    commercially_usable = False
    if status == "scored":
        effective_weights = {
            dimension: float((weights or {}).get(dimension, 1))
            for dimension in scenario.quality_dimensions
        }
        if any(weight <= 0 for weight in effective_weights.values()):
            raise ValueError("Quality weights must be positive.")
        total_weight = sum(effective_weights.values())
        weighted_mean = sum(
            dimensions[dimension].score * effective_weights[dimension]  # type: ignore[operator]
            for dimension in scenario.quality_dimensions
        ) / total_weight
        weighted_score = round((weighted_mean - 1) / 4 * 100, 2)
        minimum_score = min(
            dimensions[dimension].score  # type: ignore[type-var]
            for dimension in scenario.quality_dimensions
        )
        commercially_usable = (
            weighted_score >= commercial_threshold and minimum_score >= 3
        )

    return CreativeQualityEvaluation(
        scenario_id=scenario.scenario_id,
        status=status,
        hard_gate_failures=failures,
        missing_hard_gates=missing_gates,
        missing_dimensions=missing_dimensions,
        weighted_score=weighted_score,
        commercially_usable=commercially_usable,
    )
