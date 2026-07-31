from __future__ import annotations

import json

from PIL import Image
import pytest

from app.services.creative_artifact_evaluation import CreativeArtifactContract
from app.services.creative_blind_review import (
    BlindReviewSourceCandidate,
    prepare_blind_review_package,
)
from app.services.creative_evaluation import generate_evaluation_bundle
from app.services.creative_review_panel import create_reviewer_submission_template
from scripts.audit_creative_benchmark_run import audit_benchmark_run


async def _prepare_image_run(tmp_path):
    scenario = next(
        item
        for item in generate_evaluation_bundle(
            seed=20260731
        ).manifest.public_scenarios
        if item.modality == "image"
    ).model_copy(update={"aspect_ratio": "1:1"})
    source_paths = (tmp_path / "source-a.png", tmp_path / "source-b.png")
    for index, source_path in enumerate(source_paths):
        Image.new(
            "RGB",
            (64, 64),
            color=(20 + index * 40, 30, 40),
        ).save(source_path)
    candidates = tuple(
        BlindReviewSourceCandidate(
            candidate_id=f"private-candidate-{index}",
            provider=f"private-provider-{index}",
            model=f"private-model-{index}",
            artifacts={"image": source_path},
        )
        for index, source_path in enumerate(source_paths)
    )
    contract = CreativeArtifactContract(modality="image", aspect_ratio="1:1")
    package, key = await prepare_blind_review_package(
        scenario,
        contract,
        candidates,
        seed=11,
        public_assets_dir=tmp_path / "image" / "public" / "assets",
    )
    (tmp_path / "image-batch-spec.json").write_text(
        json.dumps(
            {
                "seed": 11,
                "scenario": scenario.model_dump(mode="json"),
                "contract": contract.model_dump(mode="json"),
                "candidates": [
                    item.model_dump(mode="json") for item in candidates
                ],
            }
        ),
        encoding="utf-8",
    )
    package_path = tmp_path / "image" / "public" / "review-package.json"
    package_path.write_text(package.model_dump_json(indent=2), encoding="utf-8")
    key_path = tmp_path / "image" / "private" / "review-key.json"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key.model_dump_json(indent=2), encoding="utf-8")
    for index in range(1, 4):
        reviewer_ref = (
            f"reviewer-{index:02d}-replace-with-independent-receipt"
        )
        reviewer = create_reviewer_submission_template(
            package,
            reviewer_receipt_ref=reviewer_ref,
        )
        reviewer_path = (
            tmp_path
            / "image"
            / "panel"
            / f"reviewer-{index:02d}"
            / "submission.json"
        )
        reviewer_path.parent.mkdir(parents=True, exist_ok=True)
        reviewer_path.write_text(
            json.dumps(
                {
                    "scenario_id": scenario.scenario_id,
                    "reviewer": reviewer.model_dump(mode="json"),
                }
            ),
            encoding="utf-8",
        )
    return package


@pytest.mark.asyncio
async def test_audit_marks_hash_verified_templates_as_awaiting_humans(
    tmp_path,
) -> None:
    package = await _prepare_image_run(tmp_path)

    audit = audit_benchmark_run(
        tmp_path,
        expected_modalities=("image",),
    )

    assert audit.status == "awaiting_human_review"
    assert audit.issues == ()
    image_audit = audit.modality_audits[0]
    assert image_audit.candidate_count == 2
    assert image_audit.verified_artifact_count == 2
    assert image_audit.reviewer_template_count == 3
    assert image_audit.completed_reviewer_file_count == 0
    assert image_audit.formal_result_count == 0
    assert image_audit.commercially_usable_count == 0
    public_json = (
        tmp_path / "image" / "public" / "review-package.json"
    ).read_text(encoding="utf-8")
    assert all(
        token not in public_json
        for token in ("private-provider", "private-model", "private-candidate")
    )
    assert len(package.candidates) == 2


@pytest.mark.asyncio
async def test_audit_fails_closed_when_public_artifact_hash_changes(
    tmp_path,
) -> None:
    package = await _prepare_image_run(tmp_path)
    first_ref = package.candidates[0].artifacts["image"]
    changed_path = tmp_path / "image" / "public" / first_ref
    Image.new("RGB", (64, 64), color=(255, 255, 255)).save(changed_path)

    audit = audit_benchmark_run(
        tmp_path,
        expected_modalities=("image",),
    )

    assert audit.status == "invalid"
    assert any("hash mismatch" in issue for issue in audit.issues)


@pytest.mark.asyncio
async def test_audit_rejects_unbound_formal_result_file(tmp_path) -> None:
    package = await _prepare_image_run(tmp_path)
    results_path = tmp_path / "image" / "results" / "sealed-panel-results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "scenario_id": package.scenario_id,
                "results": [],
            }
        ),
        encoding="utf-8",
    )

    audit = audit_benchmark_run(
        tmp_path,
        expected_modalities=("image",),
    )

    assert audit.status == "invalid"
    assert any(
        "not bound to this batch spec" in issue for issue in audit.issues
    )
    assert any(
        "not bound to this review package" in issue
        for issue in audit.issues
    )
    assert any(
        "not bound to current artifact hashes" in issue
        for issue in audit.issues
    )


def test_audit_requires_every_declared_modality(tmp_path) -> None:
    audit = audit_benchmark_run(
        tmp_path,
        expected_modalities=("image", "video", "presentation"),
    )

    assert audit.status == "invalid"
    assert {item.modality for item in audit.modality_audits} == {
        "image",
        "video",
        "presentation",
    }
    assert all(item.status == "invalid" for item in audit.modality_audits)
