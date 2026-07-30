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
from app.services.creative_review_panel import CreativeEvidenceReceipt


@pytest.mark.asyncio
async def test_panel_assembler_rejects_placeholder_reviewer_receipt(
    tmp_path,
) -> None:
    from scripts.assemble_creative_review_panel import _load_reviewer

    scenario = next(
        item
        for item in generate_evaluation_bundle(seed=20260727).manifest.public_scenarios
        if item.modality == "image"
    ).model_copy(update={"aspect_ratio": "1:1"})
    image_paths = (tmp_path / "first.png", tmp_path / "second.png")
    for path in image_paths:
        Image.new("RGB", (64, 64), color=(20, 20, 20)).save(path)
    package, _ = await prepare_blind_review_package(
        scenario,
        CreativeArtifactContract(modality="image", aspect_ratio="1:1"),
        tuple(
            BlindReviewSourceCandidate(
                candidate_id=f"candidate-{index}",
                provider=f"provider-{index}",
                artifacts={"image": path},
            )
            for index, path in enumerate(image_paths)
        ),
        seed=1,
        public_assets_dir=tmp_path / "assets",
    )
    reviewer = create_reviewer_submission_template(
        package,
        reviewer_receipt_ref="reviewer-01-replace-with-independent-receipt",
    )
    path = tmp_path / "submission.json"
    path.write_text(
        json.dumps(
            {
                "scenario_id": scenario.scenario_id,
                "reviewer": reviewer.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="placeholder"):
        _load_reviewer(path)


def test_quality_receipt_builder_accepts_exact_finding_and_rejects_possible_match() -> None:
    from scripts.build_deliverable_quality_receipt import build_blocked_payload

    exact = CreativeEvidenceReceipt(
        receipt_ref="frame-ocr-1",
        kind="frame_ocr",
        status="complete",
        artifact_hashes={"mp4": "a" * 64},
        source="tesseract:tsv",
        findings=("prohibited_term_detected=豆包",),
    )

    payload = build_blocked_payload(exact)

    assert payload["receipt"]["status"] == "blocked"
    assert payload["receipt"]["hard_gate_failures"] == [
        "no_unrequested_watermark"
    ]

    possible = exact.model_copy(
        update={"findings": ("prohibited_term_possible_match=AI生成",)}
    )
    with pytest.raises(ValueError, match="no exact prohibited-term"):
        build_blocked_payload(possible)
