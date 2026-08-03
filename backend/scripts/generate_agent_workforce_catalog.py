#!/usr/bin/env python3
"""Generate the pinned agency-agents-zh workforce decision catalog.

The generated catalog is a repository artifact, not a runtime installer. It
preserves the complete upstream inventory and the local import decision without
copying upstream role prompts into Agent souls or granting any capability.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


SOURCE_REPOSITORY = "https://github.com/jnMetaCode/agency-agents-zh"
SOURCE_COMMIT = "e7c3050dd94212832158e478f0f0af17409070f5"
EXPECTED_TOTAL = 268
ROLE_DIRECTORIES = (
    "academic",
    "design",
    "engineering",
    "finance",
    "game-development",
    "gis",
    "hr",
    "legal",
    "marketing",
    "paid-media",
    "product",
    "project-management",
    "sales",
    "security",
    "spatial-computing",
    "specialized",
    "supply-chain",
    "support",
    "testing",
)

UPGRADE_TARGETS = {
    "engineering-multi-agent-systems-architect": "multi-agent-systems-architect",
    "engineering-frontend-developer": "frontend-developer",
    "engineering-backend-architect": "backend-architect",
    "engineering-devops-automator": "devops-automator",
    "engineering-security-engineer": "security-engineer",
    "engineering-rapid-prototyper": "rapid-prototyper",
    "engineering-code-reviewer": "code-reviewer",
    "marketing-xiaohongshu-operator": "xiaohongshu-operator",
    "marketing-douyin-strategist": "douyin-operator",
    "marketing-tiktok-strategist": "tiktok-strategist",
    "marketing-growth-hacker": "growth-hacker",
    "marketing-content-creator": "content-creator",
    "marketing-seo-specialist": "seo-specialist",
    "marketing-linkedin-content-creator": "linkedin-content-creator",
    "product-manager": "product-manager",
    "project-manager-senior": "project-manager",
    "customer-success-manager": "customer-success-manager",
    "support-analytics-reporter": "support-analytics-reporter",
    "specialized-chief-of-staff": "chief-of-staff",
}

ADDITION_CANDIDATES = {
    "engineering-it-service-manager",
    "engineering-prompt-engineer",
    "engineering-ai-engineer",
    "engineering-mobile-app-builder",
    "engineering-data-engineer",
    "engineering-technical-writer",
    "engineering-autonomous-optimization-architect",
    "engineering-incident-response-commander",
    "engineering-database-optimizer",
    "engineering-git-workflow-master",
    "engineering-software-architect",
    "engineering-sre",
    "engineering-email-intelligence-engineer",
    "engineering-codebase-onboarding-engineer",
    "engineering-minimal-change-engineer",
    "engineering-feishu-integration-developer",
    "engineering-dingtalk-integration-developer",
    "design-persona-walkthrough",
    "design-ui-designer",
    "design-ux-researcher",
    "design-ux-architect",
    "design-brand-guardian",
    "design-image-prompt-engineer",
    "design-visual-storyteller",
    "design-whimsy-injector",
    "design-inclusive-visuals-specialist",
    "marketing-wechat-operator",
    "marketing-bilibili-strategist",
    "marketing-baidu-seo-specialist",
    "marketing-private-domain-operator",
    "marketing-livestream-commerce-coach",
    "marketing-cross-border-ecommerce",
    "marketing-short-video-editing-coach",
    "marketing-weibo-strategist",
    "marketing-podcast-strategist",
    "marketing-weixin-channels-strategist",
    "marketing-knowledge-commerce-strategist",
    "marketing-china-market-localization-strategist",
    "marketing-daily-news-briefing",
    "marketing-zhihu-strategist",
    "marketing-app-store-optimizer",
    "marketing-video-optimization-specialist",
    "marketing-x-twitter-intelligence-analyst",
    "marketing-aeo-foundations",
    "marketing-email-strategist",
    "marketing-pr-communications-manager",
    "marketing-social-media-strategist",
    "marketing-book-co-author",
    "marketing-agentic-search-optimizer",
    "marketing-ai-citation-strategist",
    "sales-offer-lead-gen-strategist",
    "sales-account-strategist",
    "sales-coach",
    "sales-deal-strategist",
    "sales-discovery-coach",
    "sales-engineer",
    "sales-outbound-strategist",
    "sales-pipeline-analyst",
    "sales-proposal-strategist",
    "finance-financial-analyst",
    "finance-financial-forecaster",
    "finance-fpa-analyst",
    "finance-investment-researcher",
    "hr-recruiter",
    "hr-performance-reviewer",
    "product-sprint-prioritizer",
    "product-trend-researcher",
    "product-feedback-synthesizer",
    "project-management-meeting-notes-specialist",
    "project-management-project-shepherd",
    "project-management-experiment-tracker",
    "project-management-studio-producer",
    "testing-evidence-collector",
    "testing-reality-checker",
    "testing-api-tester",
    "testing-performance-benchmarker",
    "testing-accessibility-auditor",
    "testing-test-results-analyzer",
    "support-support-responder",
    "support-executive-summary-generator",
    "business-strategist",
    "change-management-consultant",
    "operations-manager",
    "specialized-pricing-analyst",
    "agentic-identity-trust",
    "specialized-developer-advocate",
    "specialized-model-qa",
    "zk-steward",
    "corporate-training-designer",
    "specialized-mcp-builder",
    "specialized-workflow-architect",
    "specialized-meeting-assistant",
}

MERGE_OR_REJECT = {
    "marketing-xiaohongshu-specialist": {
        "action": "merge",
        "target": "xiaohongshu-operator",
        "reason": "Duplicates the governed Xiaohongshu operator role.",
    },
    "marketing-wechat-official-account": {
        "action": "merge",
        "target": "marketing-wechat-operator",
        "reason": "Duplicates the broader WeChat operator candidate.",
    },
    "marketing-ecommerce-operator": {
        "action": "merge",
        "target": "marketing-china-ecommerce-operator",
        "reason": "Duplicates the China ecommerce conditional role.",
    },
    "support-legal-compliance-checker": {
        "action": "merge",
        "target": "legal-policy-writer",
        "reason": "Legal compliance belongs to the governed legal capability pack.",
    },
    "support-finance-tracker": {
        "action": "merge",
        "target": "finance-fpa-analyst",
        "reason": "Financial tracking is part of FP&A accountability.",
    },
    "support-infrastructure-maintainer": {
        "action": "merge",
        "target": "engineering-sre",
        "reason": "Infrastructure reliability belongs to the SRE role.",
    },
    "support-recruitment-specialist": {
        "action": "merge",
        "target": "hr-recruiter",
        "reason": "Duplicates the governed recruiter candidate.",
    },
    "recruitment-specialist": {
        "action": "merge",
        "target": "hr-recruiter",
        "reason": "Duplicates the governed recruiter candidate.",
    },
    "prompt-engineer": {
        "action": "merge",
        "target": "engineering-prompt-engineer",
        "reason": "Duplicates the engineering prompt role.",
    },
    "sales-data-extraction-agent": {
        "action": "skill_only",
        "target": "engineering-data-engineer",
        "reason": "File extraction is a task capability, not a persistent employee.",
    },
    "marketing-carousel-growth-engine": {
        "action": "reject_default",
        "target": None,
        "reason": "Claims automatic generation and publishing without local approval readiness.",
    },
    "agents-orchestrator": {
        "action": "runtime_capability",
        "target": None,
        "reason": "Agent orchestration belongs to Clawith Task Runtime, not a prompt employee.",
    },
    "report-distribution-agent": {
        "action": "reject_default",
        "target": None,
        "reason": "External report distribution requires recipient authorization and receipts.",
    },
    "accounts-payable-agent": {
        "action": "reject_default",
        "target": None,
        "reason": "Autonomous payment execution is outside the default workforce risk boundary.",
    },
    "security-penetration-tester": {
        "action": "task_scoped_only",
        "target": None,
        "reason": "Offensive testing requires explicit target-by-target authorization.",
    },
}

CONDITIONAL_PACKS = {
    "工程部 (Engineering)": ("vertical-engineering", "project_and_toolchain_required"),
    "营销部 (Marketing)": ("channel-operations", "official_channel_tool_required"),
    "付费媒体部 (Paid Media)": ("paid-media", "account_budget_and_approval_required"),
    "金融部 (Finance)": ("regulated-finance", "financial_data_and_human_review_required"),
    "法务部 (Legal)": ("legal", "current_law_sources_and_human_review_required"),
    "供应链部 (Supply Chain)": ("supply-chain", "erp_or_domain_data_required"),
    "产品部 (Product)": ("behavioral-product", "ethics_review_required"),
    "项目管理部 (Project Management)": ("project-integrations", "project_system_connection_required"),
    "测试部 (Testing)": ("specialized-testing", "target_environment_required"),
    "专项部 (Specialized)": ("specialized-business", "vertical_contract_required"),
    "空间计算部 (Spatial Computing)": ("spatial-computing", "spatial_project_required"),
    "游戏开发部 (Game Development)": ("game-development", "game_project_required"),
    "学术部 (Academic)": ("academic", "education_or_research_scope_required"),
    "GIS 部 (GIS)": ("gis", "gis_data_tools_and_licenses_required"),
    "安全部 (Security)": ("security-specialist", "explicit_scope_and_security_tools_required"),
}

ROW_PATTERN = re.compile(
    r"^\| `(?P<role_id>[^`]+)` \| (?P<name>.*?) \| (?P<description>.*?) \| (?P<origin>.*?) \|$"
)


def _git_head(upstream: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=upstream, text=True
    ).strip()


def _source_paths(upstream: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for directory in ROLE_DIRECTORIES:
        for path in (upstream / directory).rglob("*.md"):
            role_id = path.stem
            if role_id in paths:
                raise ValueError(f"duplicate role file stem: {role_id}")
            paths[role_id] = path.relative_to(upstream).as_posix()
    return paths


def _parse_inventory(upstream: Path) -> list[dict[str, str]]:
    inventory_path = upstream / "AGENT-LIST.md"
    department = ""
    records: list[dict[str, str]] = []
    for raw_line in inventory_path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("## ") and raw_line not in {"## 项目概览", "## 统计摘要"}:
            department = raw_line.removeprefix("## ").strip()
            continue
        match = ROW_PATTERN.match(raw_line)
        if not match:
            continue
        records.append({"department": department, **match.groupdict()})
    return records


def _decision_for(role_id: str, department: str) -> dict[str, object]:
    if role_id in UPGRADE_TARGETS:
        return {
            "decision": "upgrade_existing",
            "lifecycle": "enabled_existing",
            "target_role_key": UPGRADE_TARGETS[role_id],
            "activation_gate": "v2_contract_and_recertification",
        }
    if role_id in ADDITION_CANDIDATES:
        return {
            "decision": "add_candidate",
            "lifecycle": "candidate_disabled",
            "target_role_key": role_id.removeprefix("engineering-")
            .removeprefix("marketing-")
            .removeprefix("project-management-")
            .removeprefix("testing-")
            .removeprefix("support-")
            .removeprefix("specialized-"),
            "activation_gate": "v2_contract_eval_and_capability_readiness",
        }
    if role_id in MERGE_OR_REJECT:
        item = MERGE_OR_REJECT[role_id]
        return {
            "decision": "merge_or_reject",
            "lifecycle": "not_recruitable",
            "resolution": item["action"],
            "target_role_key": item["target"],
            "reason": item["reason"],
            "activation_gate": "not_applicable",
        }
    pack, gate = CONDITIONAL_PACKS[department]
    return {
        "decision": "conditional_pack",
        "lifecycle": "conditional_disabled",
        "pack": pack,
        "target_role_key": None,
        "activation_gate": gate,
    }


def generate(upstream: Path, output: Path) -> None:
    head = _git_head(upstream)
    if head != SOURCE_COMMIT:
        raise ValueError(f"expected upstream {SOURCE_COMMIT}, got {head}")

    inventory = _parse_inventory(upstream)
    source_paths = _source_paths(upstream)
    if len(inventory) != EXPECTED_TOTAL:
        raise ValueError(f"expected {EXPECTED_TOTAL} inventory rows, got {len(inventory)}")

    records: list[dict[str, object]] = []
    for item in inventory:
        role_id = item["role_id"]
        source_path = source_paths.get(role_id)
        if source_path is None:
            raise ValueError(f"inventory role has no source file: {role_id}")
        records.append(
            {
                **item,
                "source_path": source_path,
                **_decision_for(role_id, item["department"]),
            }
        )

    ids = [str(record["role_id"]) for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("inventory contains duplicate role IDs")
    missing_files = sorted(set(source_paths) - set(ids))
    if missing_files:
        raise ValueError(f"source role files missing from inventory: {missing_files}")

    counts = {
        decision: sum(record["decision"] == decision for record in records)
        for decision in (
            "upgrade_existing",
            "add_candidate",
            "conditional_pack",
            "merge_or_reject",
        )
    }
    expected_counts = {
        "upgrade_existing": 19,
        "add_candidate": 92,
        "conditional_pack": 142,
        "merge_or_reject": 15,
    }
    if counts != expected_counts:
        raise ValueError(f"decision count mismatch: expected {expected_counts}, got {counts}")

    payload = {
        "schema_version": 1,
        "source": {
            "repository": SOURCE_REPOSITORY,
            "commit": SOURCE_COMMIT,
            "license": "MIT",
            "inventory_path": "AGENT-LIST.md",
        },
        "local_baseline": {
            "source_template_count": 33,
            "folder_template_count": 30,
            "legacy_template_count": 4,
            "folder_override_names": ["Project Manager"],
        },
        "summary": {"total": EXPECTED_TOTAL, **counts},
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("app/data/agent_workforce_catalog.v1.json"),
    )
    args = parser.parse_args()
    generate(args.upstream.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
