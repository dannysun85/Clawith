"""FR-I7: route recommendation report is read-only and hash-bound."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.services.media_provider_routing import media_provider_order_for_image_strategy


def _receipt(provider: str, case_key: str, tier: str, *, cost: float | None = None) -> dict:
    payload = {
        "artifact_path": f"tmp/fixture/{provider}-{case_key}.png",
        "artifact_sha256": hashlib.sha256(f"{provider}-{case_key}".encode()).hexdigest(),
        "benchmark_id": "fixture-benchmark",
        "bytes": 1024,
        "case_key": case_key,
        "completed_at": "2026-08-19T01:00:30+00:00",
        "modality": "image",
        "prompt_sha256": "p" * 64,
        "provider": provider,
        "provider_receipt": {"model": "m"},
        "saas_tier": tier,
        "started_at": "2026-08-19T01:00:00+00:00",
    }
    if cost is not None:
        payload["credit_cost"] = cost
    return payload


def _panel_result(provider: str, label: str, *, usable: bool, score: float) -> dict:
    return {
        "label": label,
        "candidate_id": label,
        "provider": provider,
        "model": "m",
        "reviewer_count": 3,
        "panel_status": "scored",
        "required_evidence_kinds": ["ocr", "human_visual"],
        "complete_evidence_kinds": ["ocr", "human_visual"],
        "missing_evidence_kinds": [],
        "disagreements": [],
        "evaluation": {
            "scenario_id": "scn",
            "status": "scored",
            "hard_gate_failures": [],
            "missing_hard_gates": [],
            "missing_dimensions": [],
            "weighted_score": score,
            "commercially_usable": usable,
        },
        "commercially_usable": usable,
    }


def _write_fixture_tree(root: Path) -> tuple[Path, Path]:
    benchmark_dir = root / "benchmark"
    benchmark_dir.mkdir(parents=True)
    for provider, cases in {
        "minimax": ["case_a", "case_b"],
        "volcengine_agent_plan": ["case_a", "case_b"],
    }.items():
        for case_key in cases:
            (benchmark_dir / f"{provider}-{case_key}.receipt.json").write_text(
                json.dumps(_receipt(provider, case_key, "pro", cost=2.0)),
                encoding="utf-8",
            )
    panel_dir = root / "panel"
    panel_dir.mkdir(parents=True)
    (panel_dir / "private-revealed-panel-results.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "scenario_id": "scn",
                "results": [
                    _panel_result("volcengine_agent_plan", "A", usable=True, score=88.0),
                    _panel_result("volcengine_agent_plan", "B", usable=True, score=84.0),
                    _panel_result("minimax", "C", usable=False, score=61.0),
                    _panel_result("minimax", "D", usable=True, score=72.0),
                ],
            }
        ),
        encoding="utf-8",
    )
    return benchmark_dir, panel_dir


def test_report_aggregates_and_never_changes_the_default_route(tmp_path) -> None:
    from scripts.report_image_route_recommendation import main

    benchmark_dir, panel_dir = _write_fixture_tree(tmp_path)
    route_before = media_provider_order_for_image_strategy("commercial_quality")
    output = tmp_path / "route-report.md"
    exit_code = main(
        [
            "--benchmark-dir",
            str(benchmark_dir),
            "--panel-dir",
            str(panel_dir),
            "--output",
            str(output),
        ]
    )
    assert exit_code == 0
    report = output.read_text(encoding="utf-8")
    assert "首轮可用率" in report
    assert "volcengine_agent_plan" in report and "minimax" in report
    assert "100.00%" in report  # volcengine 2/2 commercially usable
    assert "50.00%" in report  # minimax 1/2
    assert "recommend_primary" in report
    evidence = json.loads(
        (tmp_path / "route-report.md.evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["report_sha256"] == hashlib.sha256(report.encode()).hexdigest()
    receipt_entries = [
        value for key, value in evidence["input_sha256"].items() if key.endswith(".receipt.json")
    ]
    assert len(receipt_entries) == 4
    # Read-only discipline: the production default route is untouched.
    assert media_provider_order_for_image_strategy("commercial_quality") == route_before


def test_recommendation_requires_two_qualified_providers() -> None:
    from scripts.report_image_route_recommendation import recommend_route

    table = {
        "minimax": {
            "sample_count": 2,
            "tiers": ["pro"],
            "scored_candidates": 1,
            "first_pass_usable_rate": 1.0,
            "mean_weighted_score": 90.0,
            "mean_latency_seconds": 10.0,
            "total_observed_credits": 2.0,
            "cost_evidence": "receipts",
            "artifact_sha256": [],
        }
    }
    recommendation = recommend_route(table)
    assert recommendation["decision"] == "insufficient_evidence"
    assert recommendation["recommended_primary"] is None


def test_recommendation_no_clear_winner_keeps_default() -> None:
    from scripts.report_image_route_recommendation import recommend_route

    facts = {
        "sample_count": 2,
        "tiers": ["pro"],
        "scored_candidates": 2,
        "mean_latency_seconds": 10.0,
        "total_observed_credits": 2.0,
        "cost_evidence": "receipts",
        "artifact_sha256": [],
    }
    table = {
        "a": {**facts, "first_pass_usable_rate": 0.5, "mean_weighted_score": 80.0},
        "b": {**facts, "first_pass_usable_rate": 0.5, "mean_weighted_score": 70.0},
    }
    recommendation = recommend_route(table)
    assert recommendation["decision"] == "no_clear_winner"


def test_aggregate_marks_missing_cost_evidence_honestly() -> None:
    from scripts.report_image_route_recommendation import aggregate_by_tier

    receipts = [_receipt("minimax", "case_a", "lite")]
    table = aggregate_by_tier(receipts, [])
    assert table["minimax"]["cost_evidence"] == "unavailable"
    assert table["minimax"]["total_observed_credits"] is None
    assert table["minimax"]["mean_latency_seconds"] == 30.0
