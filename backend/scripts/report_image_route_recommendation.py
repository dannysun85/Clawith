#!/usr/bin/env python3
"""FR-I7: read-only image route recommendation report.

Aggregates the existing bounded A/B harness receipts
(``creative_provider_benchmark.py`` ``*.receipt.json``) and the blind-review
panel results (``sealed-panel-results.json`` /
``private-revealed-panel-results.json``) into a per-tier routing
recommendation report: first-pass usability rate, mean blind score, and
observed cost/latency evidence.

This script never calls a Provider, never writes outside its explicit output
paths, and never changes the default route — switching
``media_provider_order_for_image_strategy()`` defaults requires separate
governance authorization (rule 9).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


DEFAULT_BENCHMARK_DIR = REPO_DIR / "tmp" / "creative-benchmark"
DEFAULT_PANEL_DIR = REPO_DIR / "tmp" / "creative-evaluation"
REPORT_SCHEMA_VERSION = "image-route-recommendation-v1"
MINIMUM_SCORED_SAMPLES = 2

_COST_KEYS = ("credit_cost", "credits", "cost_credits", "quoted_credits")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def collect_image_receipts(benchmark_dirs: list[Path]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Read benchmark receipts; returns (image receipts, evidence hashes)."""

    receipts: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    for directory in benchmark_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.receipt.json")):
            payload = _load_json(path)
            evidence[str(path)] = _sha256_file(path)
            if not isinstance(payload, dict) or payload.get("modality") != "image":
                continue
            receipts.append({**payload, "_receipt_path": str(path)})
    return receipts, evidence


def collect_panel_results(panel_dirs: list[Path]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Read revealed (provider-attributed) panel results, else sealed ones."""

    results: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    for directory in panel_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("private-revealed-panel-results.json")):
            payload = _load_json(path)
            evidence[str(path)] = _sha256_file(path)
            if not isinstance(payload, dict):
                continue
            for item in payload.get("results") or ():
                if isinstance(item, dict) and item.get("provider"):
                    results.append({**item, "_panel_path": str(path), "_sealed": False})
        for path in sorted(directory.rglob("sealed-panel-results.json")):
            payload = _load_json(path)
            evidence[str(path)] = _sha256_file(path)
            if not isinstance(payload, dict):
                continue
            for item in payload.get("results") or ():
                if isinstance(item, dict):
                    results.append({**item, "_panel_path": str(path), "_sealed": True})
    return results, evidence


def _receipt_cost(receipt: dict[str, Any]) -> float | None:
    for key in _COST_KEYS:
        value = receipt.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _receipt_latency_seconds(receipt: dict[str, Any]) -> float | None:
    try:
        started = datetime.fromisoformat(str(receipt["started_at"]))
        completed = datetime.fromisoformat(str(receipt["completed_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return max((completed - started).total_seconds(), 0.0)


def aggregate_by_tier(
    receipts: list[dict[str, Any]],
    panel_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-provider × per-tier first-pass rate, mean score, and cost facts."""

    providers: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        provider = str(receipt.get("provider") or "unknown")
        entry = providers.setdefault(
            provider,
            {
                "sample_count": 0,
                "tiers": set(),
                "latencies": [],
                "costs": [],
                "artifact_sha256": [],
            },
        )
        entry["sample_count"] += 1
        tier = str(receipt.get("saas_tier") or "unknown")
        entry["tiers"].add(tier)
        latency = _receipt_latency_seconds(receipt)
        if latency is not None:
            entry["latencies"].append(latency)
        cost = _receipt_cost(receipt)
        if cost is not None:
            entry["costs"].append(cost)
        if receipt.get("artifact_sha256"):
            entry["artifact_sha256"].append(str(receipt["artifact_sha256"]))

    scored: dict[str, dict[str, Any]] = {}
    for result in panel_results:
        evaluation = result.get("evaluation")
        if not isinstance(evaluation, dict):
            continue
        status = str(evaluation.get("status") or "")
        if status != "scored":
            continue
        provider = str(result.get("provider") or "")
        if not provider:
            # Sealed results carry no provider attribution; they count toward
            # evidence completeness but never toward per-provider scores.
            continue
        entry = scored.setdefault(provider, {"usable": 0, "total": 0, "scores": []})
        entry["total"] += 1
        if result.get("commercially_usable") is True:
            entry["usable"] += 1
        score = evaluation.get("weighted_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            entry["scores"].append(float(score))

    table: dict[str, Any] = {}
    for provider in sorted(set(providers) | set(scored)):
        generation = providers.get(provider, {"sample_count": 0, "tiers": set(), "latencies": [], "costs": [], "artifact_sha256": []})
        panel = scored.get(provider, {"usable": 0, "total": 0, "scores": []})
        table[provider] = {
            "sample_count": generation["sample_count"],
            "tiers": sorted(generation["tiers"]),
            "scored_candidates": panel["total"],
            "first_pass_usable_rate": (
                round(panel["usable"] / panel["total"], 4) if panel["total"] else None
            ),
            "mean_weighted_score": (
                round(sum(panel["scores"]) / len(panel["scores"]), 2) if panel["scores"] else None
            ),
            "mean_latency_seconds": (
                round(sum(generation["latencies"]) / len(generation["latencies"]), 2)
                if generation["latencies"]
                else None
            ),
            "total_observed_credits": (
                round(sum(generation["costs"]), 4) if generation["costs"] else None
            ),
            "cost_evidence": "receipts" if generation["costs"] else "unavailable",
            "artifact_sha256": sorted(generation["artifact_sha256"]),
        }
    return table


def recommend_route(table: dict[str, Any]) -> dict[str, Any]:
    """Advisory recommendation; the default route is never changed here."""

    candidates = {
        provider: facts
        for provider, facts in table.items()
        if facts["scored_candidates"] >= MINIMUM_SCORED_SAMPLES
        and facts["first_pass_usable_rate"] is not None
    }
    if len(candidates) < 2:
        return {
            "decision": "insufficient_evidence",
            "recommended_primary": None,
            "rationale": (
                "Fewer than two providers meet the minimum scored-sample bar "
                f"({MINIMUM_SCORED_SAMPLES}); keep the current default route."
            ),
        }
    ranked = sorted(
        candidates.items(),
        key=lambda item: (
            -(item[1]["first_pass_usable_rate"] or 0),
            -(item[1]["mean_weighted_score"] or 0),
            item[0],
        ),
    )
    winner, winner_facts = ranked[0]
    runner_up_facts = ranked[1][1]
    strictly_better = (
        (winner_facts["first_pass_usable_rate"] or 0)
        > (runner_up_facts["first_pass_usable_rate"] or 0)
        and (winner_facts["mean_weighted_score"] or 0)
        >= (runner_up_facts["mean_weighted_score"] or 0)
    )
    if not strictly_better:
        return {
            "decision": "no_clear_winner",
            "recommended_primary": None,
            "rationale": (
                "No provider is strictly better on first-pass usability with a "
                "non-inferior mean blind score; keep the current default route."
            ),
        }
    return {
        "decision": "recommend_primary",
        "recommended_primary": winner,
        "rationale": (
            f"{winner} leads on first-pass usability "
            f"({winner_facts['first_pass_usable_rate']}) with a non-inferior mean "
            f"blind score ({winner_facts['mean_weighted_score']}). Switching the "
            "default route still requires separate governance authorization."
        ),
    }


def render_markdown(
    *,
    table: dict[str, Any],
    recommendation: dict[str, Any],
    evidence: dict[str, str],
    generated_at: datetime,
) -> str:
    lines = [
        "# 图片路由建议报告（FR-I7）",
        "",
        f"- schema_version: {REPORT_SCHEMA_VERSION}",
        f"- generated_at: {generated_at.isoformat()}",
        "- 性质：只读证据汇总与建议；默认路由不因本报告改变。",
        "",
        "## 分 Provider 证据",
        "",
        "| provider | 样本数 | 档位 | 盲评候选 | 首轮可用率 | 盲评均分 | 平均耗时(s) | 观测成本(credits) | 成本证据 |",
        "|---|---|---|---|---|---|---|---|---|"]
    for provider, facts in table.items():
        lines.append(
            "| {provider} | {samples} | {tiers} | {scored} | {rate} | {score} | {latency} | {cost} | {cost_evidence} |".format(
                provider=provider,
                samples=facts["sample_count"],
                tiers=", ".join(facts["tiers"]) or "—",
                scored=facts["scored_candidates"],
                rate=(
                    f"{facts['first_pass_usable_rate']:.2%}"
                    if facts["first_pass_usable_rate"] is not None
                    else "—"
                ),
                score=(
                    f"{facts['mean_weighted_score']:.2f}"
                    if facts["mean_weighted_score"] is not None
                    else "—"
                ),
                latency=(
                    f"{facts['mean_latency_seconds']:.2f}"
                    if facts["mean_latency_seconds"] is not None
                    else "—"
                ),
                cost=(
                    f"{facts['total_observed_credits']:.4f}"
                    if facts["total_observed_credits"] is not None
                    else "—"
                ),
                cost_evidence=facts["cost_evidence"],
            )
        )
    lines += [
        "",
        "## 路由建议",
        "",
        f"- decision: `{recommendation['decision']}`",
        f"- recommended_primary: `{recommendation['recommended_primary']}`",
        f"- rationale: {recommendation['rationale']}",
        "",
        "## 证据清单（SHA-256 绑定）",
        "",
    ]
    for path, digest in sorted(evidence.items()):
        lines.append(f"- `{digest}` — {path}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        action="append",
        default=None,
        help="Directory tree scanned for *.receipt.json (repeatable).",
    )
    parser.add_argument(
        "--panel-dir",
        type=Path,
        action="append",
        default=None,
        help="Directory tree scanned for panel result JSON (repeatable).",
    )
    parser.add_argument("--output", type=Path, required=True, help="Markdown report path.")
    parser.add_argument(
        "--evidence-output",
        type=Path,
        default=None,
        help="Optional JSON evidence manifest path (defaults to <output>.evidence.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    benchmark_dirs = args.benchmark_dir or [DEFAULT_BENCHMARK_DIR]
    panel_dirs = args.panel_dir or [DEFAULT_PANEL_DIR]

    receipts, receipt_evidence = collect_image_receipts(benchmark_dirs)
    panel_results, panel_evidence = collect_panel_results(panel_dirs)
    evidence = {**receipt_evidence, **panel_evidence}
    table = aggregate_by_tier(receipts, panel_results)
    recommendation = recommend_route(table)
    generated_at = datetime.now(UTC)

    report = render_markdown(
        table=table,
        recommendation=recommendation,
        evidence=evidence,
        generated_at=generated_at,
    )
    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")

    evidence_path: Path = args.evidence_output or output_path.with_suffix(
        output_path.suffix + ".evidence.json"
    )
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat(),
                "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                "input_sha256": dict(sorted(evidence.items())),
                "recommendation": recommendation,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"report: {output_path}")
    print(f"evidence: {evidence_path}")
    print(f"decision: {recommendation['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
