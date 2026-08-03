from __future__ import annotations

import pytest

from scripts.audit_creative_benchmark_run import (
    BenchmarkRunAudit,
    ModalityBenchmarkAudit,
)
from scripts.freeze_creative_benchmark_candidate import (
    CandidateFreezeError,
    build_candidate_manifest,
)


def _audit(status: str = "commercial_ready") -> BenchmarkRunAudit:
    modalities = ("image", "video", "presentation")
    return BenchmarkRunAudit(
        run_dir="/tmp/benchmark",
        run_fingerprint_sha256="a" * 64,
        status=status,
        expected_modalities=modalities,
        modality_audits=tuple(
            ModalityBenchmarkAudit(
                modality=modality,
                scenario_id=f"scenario-{modality}",
                status=status,
                candidate_count=2,
                verified_artifact_count=2,
                reviewer_template_count=3,
                completed_reviewer_file_count=3,
                formal_result_count=2,
                commercially_usable_count=2,
            )
            for modality in modalities
        ),
    )


def test_candidate_manifest_is_deterministic_and_content_addressed() -> None:
    first = build_candidate_manifest(
        _audit(),
        source_revision="1" * 40,
        expected_modalities=("image", "video", "presentation"),
    )
    second = build_candidate_manifest(
        _audit(),
        source_revision="1" * 40,
        expected_modalities=("image", "video", "presentation"),
    )

    assert first == second
    assert len(first.candidate_sha256) == 64
    assert first.run_fingerprint_sha256 == "a" * 64


@pytest.mark.parametrize(
    "status, expected_message",
    [
        ("awaiting_human_review", "sealed commercial review is required"),
        ("evaluated_not_commercial", "sealed commercial review is required"),
    ],
)
def test_candidate_freeze_rejects_unfinished_audit(
    status: str,
    expected_message: str,
) -> None:
    with pytest.raises(CandidateFreezeError, match=expected_message):
        build_candidate_manifest(
            _audit(status),
            source_revision="1" * 40,
            expected_modalities=("image", "video", "presentation"),
        )


def test_candidate_freeze_rejects_short_source_revision() -> None:
    with pytest.raises(CandidateFreezeError, match="full 40-character Git SHA"):
        build_candidate_manifest(
            _audit(),
            source_revision="1" * 39,
            expected_modalities=("image", "video", "presentation"),
        )
