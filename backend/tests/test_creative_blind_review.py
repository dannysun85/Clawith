from __future__ import annotations

import json

from PIL import Image
import pytest

from app.services.creative_artifact_evaluation import CreativeArtifactContract
from app.services.creative_blind_review import (
    BlindCandidateReviewSubmission,
    BlindReviewSourceCandidate,
    prepare_blind_review_package,
    reveal_scored_blind_reviews,
    score_blind_review_submissions,
)
from app.services.creative_evaluation import (
    HardGateObservation,
    QualityDimensionObservation,
    generate_evaluation_bundle,
)


@pytest.mark.asyncio
async def test_blind_review_copies_opaque_assets_and_hides_provider_paths(
    tmp_path,
) -> None:
    scenario = next(
        item
        for item in generate_evaluation_bundle(seed=20260727).manifest.public_scenarios
        if item.modality == "image"
    ).model_copy(update={"aspect_ratio": "1:1"})
    first_path = tmp_path / "volcengine-secret-name.png"
    second_path = tmp_path / "minimax-secret-name.png"
    Image.new("RGB", (512, 512), color=(120, 30, 30)).save(first_path)
    Image.new("RGB", (512, 512), color=(30, 120, 30)).save(second_path)

    package, key = await prepare_blind_review_package(
        scenario,
        CreativeArtifactContract(modality="image", aspect_ratio="1:1"),
        (
            BlindReviewSourceCandidate(
                candidate_id="first-private-id",
                provider="volcengine_agent_plan",
                model="seedream-private",
                artifacts={"image": first_path},
            ),
            BlindReviewSourceCandidate(
                candidate_id="second-private-id",
                provider="minimax",
                model="image-01",
                artifacts={"image": second_path},
            ),
        ),
        seed=42,
        public_assets_dir=tmp_path / "public" / "assets",
    )

    public_json = package.model_dump_json().lower()
    assert "volcengine" not in public_json
    assert "minimax" not in public_json
    assert "seedream" not in public_json
    assert "secret-name" not in public_json
    assert str(tmp_path).lower() not in public_json
    assert package.masking_scope == "manifest_and_filename_only"
    assert package.embedded_identity_review_required is True
    assert package.masking_limitations
    assert set(key.candidates) == {"A", "B"}
    assert {item.provider for item in key.candidates.values()} == {
        "volcengine_agent_plan",
        "minimax",
    }
    for candidate in package.candidates:
        assert candidate.artifacts["image"].startswith("assets/")
        copied = tmp_path / "public" / candidate.artifacts["image"]
        assert copied.is_file()
        assert copied.stat().st_mode & 0o777 == 0o600
        assert (
            candidate.structural_observation.hard_gates[
                "artifact_decodable"
            ].passed
            is True
        )


def test_public_model_does_not_define_provider_fields() -> None:
    from app.services.creative_blind_review import BlindReviewPublicCandidate

    fields = set(BlindReviewPublicCandidate.model_fields)
    assert "provider" not in fields
    assert "model" not in fields
    assert "source_artifacts" not in fields
    assert "artifact_path" not in json.dumps(sorted(fields))


@pytest.mark.asyncio
async def test_scores_before_revealing_provider_attribution(tmp_path) -> None:
    scenario = next(
        item
        for item in generate_evaluation_bundle(seed=20260727).manifest.public_scenarios
        if item.modality == "image"
    ).model_copy(update={"aspect_ratio": "1:1"})
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    Image.new("RGB", (512, 512), color=(120, 30, 30)).save(first_path)
    Image.new("RGB", (512, 512), color=(30, 120, 30)).save(second_path)
    package, key = await prepare_blind_review_package(
        scenario,
        CreativeArtifactContract(modality="image", aspect_ratio="1:1"),
        (
            BlindReviewSourceCandidate(
                candidate_id="first-private-id",
                provider="first-provider",
                artifacts={"image": first_path},
            ),
            BlindReviewSourceCandidate(
                candidate_id="second-private-id",
                provider="second-provider",
                artifacts={"image": second_path},
            ),
        ),
        seed=42,
        public_assets_dir=tmp_path / "public" / "assets",
    )
    submissions = tuple(
        BlindCandidateReviewSubmission(
            label=candidate.label,
            reviewer_receipt_ref=f"review-{candidate.label}",
            hard_gates={
                gate: HardGateObservation(passed=True, evidence=("reviewed",))
                for gate in scenario.hard_gates
            },
            dimensions={
                dimension: QualityDimensionObservation(
                    score=4,
                    evidence=("reviewed",),
                )
                for dimension in scenario.quality_dimensions
            },
        )
        for candidate in package.candidates
    )

    scored = score_blind_review_submissions(scenario, package, submissions)

    assert all(item.evaluation.status == "scored" for item in scored)
    assert "provider" not in json.dumps(
        [item.model_dump(mode="json") for item in scored],
        sort_keys=True,
    )
    revealed = reveal_scored_blind_reviews(scored, key)
    assert {item.provider for item in revealed} == {
        "first-provider",
        "second-provider",
    }


@pytest.mark.asyncio
async def test_rejects_partial_blind_review_batch(tmp_path) -> None:
    scenario = next(
        item
        for item in generate_evaluation_bundle(seed=20260727).manifest.public_scenarios
        if item.modality == "image"
    ).model_copy(update={"aspect_ratio": "1:1"})
    paths = (tmp_path / "first.png", tmp_path / "second.png")
    for path in paths:
        Image.new("RGB", (512, 512), color=(30, 30, 30)).save(path)
    package, _ = await prepare_blind_review_package(
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

    with pytest.raises(ValueError, match="exactly once"):
        score_blind_review_submissions(scenario, package, ())
