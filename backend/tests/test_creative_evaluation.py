from __future__ import annotations

import json

import pytest

from app.services.creative_evaluation import (
    CandidateDescriptor,
    HardGateObservation,
    QualityDimensionObservation,
    create_blind_comparison,
    generate_evaluation_bundle,
    score_quality_evaluation,
    verify_holdout_commitment,
)


def _first_scenario(seed: int = 20260726):
    bundle = generate_evaluation_bundle(seed=seed)
    return bundle.manifest.public_scenarios[0]


def test_evaluation_bundle_is_stable_for_same_seed() -> None:
    first = generate_evaluation_bundle(seed=20260726)
    second = generate_evaluation_bundle(seed=20260726)

    assert first == second
    assert verify_holdout_commitment(first.holdout)


def test_evaluation_bundle_changes_with_seed_and_covers_modalities() -> None:
    first = generate_evaluation_bundle(seed=20260726)
    second = generate_evaluation_bundle(seed=20260727)

    assert first.manifest.holdout_commitment_sha256 != second.manifest.holdout_commitment_sha256
    assert first.manifest.coverage["modality"] == {
        "image": 8,
        "presentation": 8,
        "video": 8,
    }
    assert len(first.manifest.coverage["industry"]) == 8
    assert len(first.manifest.coverage["objective"]) == 6
    assert len(first.manifest.coverage["source_mode"]) == 4


def test_holdout_content_is_not_exposed_in_public_manifest() -> None:
    bundle = generate_evaluation_bundle(seed=20260726)
    public_fingerprints = {
        scenario.fingerprint for scenario in bundle.manifest.public_scenarios
    }
    holdout_fingerprints = {
        scenario.fingerprint for scenario in bundle.holdout.scenarios
    }

    assert bundle.manifest.holdout_count == len(bundle.holdout.scenarios)
    assert public_fingerprints.isdisjoint(holdout_fingerprints)
    public_json = bundle.manifest.model_dump_json()
    assert all(
        scenario.brief not in public_json for scenario in bundle.holdout.scenarios
    )


def test_generated_briefs_are_provider_neutral() -> None:
    bundle = generate_evaluation_bundle(seed=20260726)
    serialized = json.dumps(
        bundle.manifest.model_dump(mode="json"),
        ensure_ascii=False,
    ).lower()

    assert "minimax" not in serialized
    assert "volcengine" not in serialized
    assert "seedance" not in serialized
    assert "doubao" not in serialized


def test_blind_comparison_masks_provider_model_and_artifact_path() -> None:
    scenario = _first_scenario()
    package, key = create_blind_comparison(
        scenario,
        (
            CandidateDescriptor(
                candidate_id="candidate-volcengine",
                artifact_ref="/private/volcengine/output.png",
                provider="volcengine_agent_plan",
                model="doubao-seedream",
            ),
            CandidateDescriptor(
                candidate_id="candidate-minimax",
                artifact_ref="/private/minimax/output.png",
                provider="minimax",
                model="image-01",
            ),
        ),
        seed=99,
    )

    public_json = package.model_dump_json().lower()
    assert "volcengine" not in public_json
    assert "minimax" not in public_json
    assert "/private/" not in public_json
    assert set(key.candidates) == {"A", "B"}


def test_missing_evidence_is_incomplete_not_optimistic_pass() -> None:
    scenario = _first_scenario()

    result = score_quality_evaluation(
        scenario,
        hard_gates={},
        dimensions={},
    )

    assert result.status == "incomplete"
    assert result.weighted_score is None
    assert result.commercially_usable is False
    assert result.missing_hard_gates == scenario.hard_gates
    assert result.missing_dimensions == scenario.quality_dimensions


def test_hard_gate_failure_blocks_high_quality_scores() -> None:
    scenario = _first_scenario()
    hard_gates = {
        gate: HardGateObservation(passed=True) for gate in scenario.hard_gates
    }
    hard_gates[scenario.hard_gates[0]] = HardGateObservation(passed=False)
    dimensions = {
        dimension: QualityDimensionObservation(score=5)
        for dimension in scenario.quality_dimensions
    }

    result = score_quality_evaluation(
        scenario,
        hard_gates=hard_gates,
        dimensions=dimensions,
    )

    assert result.status == "blocked"
    assert result.weighted_score is None
    assert result.commercially_usable is False


def test_complete_evidence_can_reach_commercial_threshold() -> None:
    scenario = _first_scenario()

    result = score_quality_evaluation(
        scenario,
        hard_gates={
            gate: HardGateObservation(passed=True) for gate in scenario.hard_gates
        },
        dimensions={
            dimension: QualityDimensionObservation(score=4.5)
            for dimension in scenario.quality_dimensions
        },
    )

    assert result.status == "scored"
    assert result.weighted_score == pytest.approx(87.5)
    assert result.commercially_usable is True
