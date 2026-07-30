from __future__ import annotations

from PIL import Image
import pytest

from app.services.creative_artifact_evaluation import CreativeArtifactContract
from app.services.creative_blind_review import (
    BlindCandidateReviewSubmission,
    BlindReviewSourceCandidate,
    prepare_blind_review_package,
)
from app.services.creative_evaluation import (
    HardGateObservation,
    QualityDimensionObservation,
    generate_evaluation_bundle,
)
from app.services.creative_review_panel import (
    BlindPanelCandidateSubmission,
    BlindPanelReviewerBatch,
    BlindPanelSubmission,
    CreativeEvidenceReceipt,
    create_reviewer_submission_template,
    score_blind_review_panel,
)


async def _image_package(tmp_path):
    scenario = next(
        item
        for item in generate_evaluation_bundle(seed=20260727).manifest.public_scenarios
        if item.modality == "image"
    ).model_copy(update={"aspect_ratio": "1:1"})
    paths = (tmp_path / "first.png", tmp_path / "second.png")
    for index, path in enumerate(paths):
        Image.new("RGB", (512, 512), color=(30 + index * 60, 30, 30)).save(path)
    package, key = await prepare_blind_review_package(
        scenario,
        CreativeArtifactContract(modality="image", aspect_ratio="1:1"),
        tuple(
            BlindReviewSourceCandidate(
                candidate_id=f"candidate-{index}",
                provider=f"provider-{index}",
                artifacts={"image": path},
            )
            for index, path in enumerate(paths)
        ),
        seed=42,
        public_assets_dir=tmp_path / "public" / "assets",
    )
    return scenario, package, key


def _panel(
    scenario,
    package,
    *,
    reviewer_count: int = 3,
    disagree_gate: str | None = None,
    omit_ocr: bool = False,
):
    reviewer_batches = []
    for reviewer_index in range(reviewer_count):
        candidate_submissions = []
        for candidate in package.candidates:
            hashes = {
                artifact_type: artifact.content_sha256
                for artifact_type, artifact in candidate.structural_observation.files.items()
            }
            hard_gates = {
                gate: HardGateObservation(
                    passed=not (
                        disagree_gate == gate and reviewer_index == reviewer_count - 1
                    ),
                    evidence=(f"reviewer-{reviewer_index}",),
                )
                for gate in scenario.hard_gates
            }
            evidence = [
                CreativeEvidenceReceipt(
                    receipt_ref=f"human-{reviewer_index}-{candidate.label}",
                    kind="human_visual",
                    status="complete",
                    artifact_hashes=hashes,
                    source="independent_human_review",
                )
            ]
            if not omit_ocr and reviewer_index == 0:
                evidence.append(
                    CreativeEvidenceReceipt(
                        receipt_ref=f"ocr-{candidate.label}",
                        kind="ocr",
                        status="complete",
                        artifact_hashes=hashes,
                        source="tesseract",
                        language_coverage=("eng",),
                    )
                )
            candidate_submissions.append(
                BlindPanelCandidateSubmission(
                    review=BlindCandidateReviewSubmission(
                        label=candidate.label,
                        reviewer_receipt_ref=(
                            f"review-{reviewer_index}-{candidate.label}"
                        ),
                        hard_gates=hard_gates,
                        dimensions={
                            dimension: QualityDimensionObservation(
                                score=4.5,
                                evidence=(f"reviewer-{reviewer_index}",),
                            )
                            for dimension in scenario.quality_dimensions
                        },
                    ),
                    evidence_receipts=tuple(evidence),
                )
            )
        reviewer_batches.append(
            BlindPanelReviewerBatch(
                reviewer_receipt_ref=f"reviewer-{reviewer_index}",
                candidates=tuple(candidate_submissions),
            )
        )
    return BlindPanelSubmission(
        scenario_id=scenario.scenario_id,
        reviewers=tuple(reviewer_batches),
    )


@pytest.mark.asyncio
async def test_three_reviewer_panel_can_reach_formal_commercial_result(
    tmp_path,
) -> None:
    scenario, package, _ = await _image_package(tmp_path)

    results = score_blind_review_panel(
        scenario,
        package,
        _panel(scenario, package),
    )

    assert len(results) == 2
    assert all(result.reviewer_count == 3 for result in results)
    assert all(result.panel_status == "scored" for result in results)
    assert all(result.commercially_usable is True for result in results)
    assert all(
        result.complete_evidence_kinds == ("ocr", "human_visual")
        for result in results
    )


@pytest.mark.asyncio
async def test_reviewer_template_is_provider_free_and_incomplete(tmp_path) -> None:
    scenario, package, _ = await _image_package(tmp_path)

    template = create_reviewer_submission_template(
        package,
        reviewer_receipt_ref="independent-reviewer-receipt",
    )

    assert len(template.candidates) == len(package.candidates)
    assert all(
        set(item.review.hard_gates) == set(scenario.hard_gates)
        for item in template.candidates
    )
    assert all(
        observation.passed is None
        for item in template.candidates
        for observation in item.review.hard_gates.values()
    )
    serialized = template.model_dump_json().lower()
    assert "provider-0" not in serialized
    assert "provider-1" not in serialized


@pytest.mark.asyncio
async def test_panel_disagreement_is_incomplete_not_optimistic_pass(tmp_path) -> None:
    scenario, package, _ = await _image_package(tmp_path)
    gate = scenario.hard_gates[0]

    results = score_blind_review_panel(
        scenario,
        package,
        _panel(scenario, package, disagree_gate=gate),
    )

    assert all(result.panel_status == "incomplete" for result in results)
    assert all(result.commercially_usable is False for result in results)
    assert all(f"hard_gate:{gate}" in result.disagreements for result in results)


@pytest.mark.asyncio
async def test_missing_reviewer_gate_is_incomplete_not_optimistic_pass(
    tmp_path,
) -> None:
    scenario, package, _ = await _image_package(tmp_path)
    panel = _panel(scenario, package)
    gate = scenario.hard_gates[0]
    reviewer = panel.reviewers[0]
    candidate = reviewer.candidates[0]
    missing_gate_review = candidate.review.model_copy(
        update={
            "hard_gates": {
                name: observation
                for name, observation in candidate.review.hard_gates.items()
                if name != gate
            }
        }
    )
    missing_gate_candidate = candidate.model_copy(
        update={"review": missing_gate_review}
    )
    missing_gate_reviewer = reviewer.model_copy(
        update={
            "candidates": (
                missing_gate_candidate,
                *reviewer.candidates[1:],
            )
        }
    )
    missing_gate_panel = panel.model_copy(
        update={"reviewers": (missing_gate_reviewer, *panel.reviewers[1:])}
    )

    results = score_blind_review_panel(
        scenario,
        package,
        missing_gate_panel,
    )

    first_label = reviewer.candidates[0].review.label
    first_result = next(result for result in results if result.label == first_label)
    assert first_result.panel_status == "incomplete"
    assert first_result.commercially_usable is False
    assert f"hard_gate:{gate}" in first_result.disagreements


@pytest.mark.asyncio
async def test_missing_ocr_blocks_formal_panel_release(tmp_path) -> None:
    scenario, package, _ = await _image_package(tmp_path)

    results = score_blind_review_panel(
        scenario,
        package,
        _panel(scenario, package, omit_ocr=True),
    )

    assert all(result.evaluation.status == "scored" for result in results)
    assert all(result.panel_status == "incomplete" for result in results)
    assert all(result.missing_evidence_kinds == ("ocr",) for result in results)
    assert all(result.commercially_usable is False for result in results)


@pytest.mark.asyncio
async def test_exact_prohibited_ocr_term_overrides_optimistic_panel(
    tmp_path,
) -> None:
    scenario, package, _ = await _image_package(tmp_path)
    panel = _panel(scenario, package)
    first_reviewer = panel.reviewers[0]
    first_candidate = first_reviewer.candidates[0]
    receipts = tuple(
        receipt.model_copy(
            update={"findings": ("prohibited_term_detected=豆包",)}
        )
        if receipt.kind == "ocr"
        else receipt
        for receipt in first_candidate.evidence_receipts
    )
    updated_candidate = first_candidate.model_copy(
        update={"evidence_receipts": receipts}
    )
    updated_reviewer = first_reviewer.model_copy(
        update={
            "candidates": (
                updated_candidate,
                *first_reviewer.candidates[1:],
            )
        }
    )
    updated_panel = panel.model_copy(
        update={"reviewers": (updated_reviewer, *panel.reviewers[1:])}
    )

    results = score_blind_review_panel(
        scenario,
        package,
        updated_panel,
    )

    blocked = next(
        result
        for result in results
        if result.label == updated_candidate.review.label
    )
    assert blocked.panel_status == "blocked"
    assert blocked.commercially_usable is False
    assert "no_unrequested_watermark" in blocked.evaluation.hard_gate_failures


@pytest.mark.asyncio
async def test_duplicate_reviewer_receipts_are_rejected(tmp_path) -> None:
    scenario, package, _ = await _image_package(tmp_path)
    panel = _panel(scenario, package)
    duplicate = panel.model_copy(
        update={
            "reviewers": (
                panel.reviewers[0],
                panel.reviewers[0],
                panel.reviewers[2],
            )
        }
    )

    with pytest.raises(ValueError, match="unique"):
        score_blind_review_panel(scenario, package, duplicate)


@pytest.mark.asyncio
async def test_stale_evidence_hash_is_rejected(tmp_path) -> None:
    scenario, package, _ = await _image_package(tmp_path)
    panel = _panel(scenario, package)
    first_reviewer = panel.reviewers[0]
    first_candidate = first_reviewer.candidates[0]
    stale_receipt = first_candidate.evidence_receipts[0].model_copy(
        update={"artifact_hashes": {"image": "0" * 64}}
    )
    stale_candidate = first_candidate.model_copy(
        update={
            "evidence_receipts": (
                stale_receipt,
                *first_candidate.evidence_receipts[1:],
            )
        }
    )
    stale_reviewer = first_reviewer.model_copy(
        update={
            "candidates": (
                stale_candidate,
                *first_reviewer.candidates[1:],
            )
        }
    )
    stale_panel = panel.model_copy(
        update={"reviewers": (stale_reviewer, *panel.reviewers[1:])}
    )

    with pytest.raises(ValueError, match="hashes"):
        score_blind_review_panel(scenario, package, stale_panel)
