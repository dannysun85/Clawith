from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from app.api.work import _build_work_statement, _confirmation_fingerprint
from app.schemas.work import WorkTaskDraft
from app.services.product_information_architecture import (
    evaluate_product_navigation_claims,
    product_information_architecture_snapshot,
    render_product_information_architecture_prompt,
)


def test_catalog_is_version_bound_and_contains_only_real_product_routes() -> None:
    snapshot = product_information_architecture_snapshot()

    assert snapshot["version"] == 1
    assert snapshot["catalog_id"] == "astra-product-ia-1.12.1-r1"
    assert len(snapshot["catalog_sha256"]) == 64
    routes = {entry["route"] for entry in snapshot["entries"]}
    assert {
        "/work",
        "/groups",
        "/employees",
        "/dashboard",
        "/okr",
        "/plaza",
        "/company-admin/integrations/tools",
        "/company-admin/integrations/skills",
        "/company-admin/integrations/org",
        "/company-admin/integrations/douyin",
    } <= routes
    rendered = render_product_information_architecture_prompt(snapshot)
    assert "报告中心" not in rendered
    assert "工作台 → 组织管理" not in rendered
    assert "组织同步 (`/company-admin/integrations/org`" in rendered
    assert "页面存在也不代表当前用户有权限或功能已启用" in rendered


def test_explicit_product_breadcrumbs_must_match_the_catalog() -> None:
    snapshot = product_information_architecture_snapshot()

    valid = evaluate_product_navigation_claims(
        snapshot,
        "请进入公司管理 → 企业知识与集成 → 组织同步 (`/company-admin/integrations/org`)。",
    )
    invalid = evaluate_product_navigation_claims(
        snapshot,
        "1. 工作台 → 组织管理：同步企业通讯录。\n2. 工作台 → 报告中心",
    )
    ordinary_business_flow = evaluate_product_navigation_claims(
        snapshot,
        "需求澄清 → 方案评审 → 客户确认",
    )

    assert valid.valid is True
    assert valid.passed is True
    assert invalid.valid is True
    assert invalid.passed is False
    assert invalid.details["invalid_claims"] == [
        ["工作台", "组织管理"],
        ["工作台", "报告中心"],
    ]
    assert ordinary_business_flow.passed is True
    assert ordinary_business_flow.details["claim_count"] == 0

    wrong_route = evaluate_product_navigation_claims(
        snapshot,
        "公司管理 → 企业知识与集成 → 组织同步 (`/company-admin/reports`)",
    )
    assert wrong_route.passed is False
    assert wrong_route.details["invalid_routes"][0]["claimed_route"] == (
        "/company-admin/reports"
    )


def test_catalog_snapshot_fails_closed_after_tampering() -> None:
    snapshot = product_information_architecture_snapshot()
    tampered = deepcopy(snapshot)
    tampered["entries"][0]["route"] = "/report-center"

    result = evaluate_product_navigation_claims(tampered, "工作台 → 任务详情")

    assert result.required is True
    assert result.valid is False
    assert result.details["code"] == "invalid_product_information_architecture_snapshot"
    assert render_product_information_architecture_prompt(tampered) == ""


def test_new_work_statement_persists_the_exact_catalog_snapshot() -> None:
    draft = WorkTaskDraft(
        title="准备首周上线清单",
        intent="给出 Astra 的首周客户上线执行清单",
    )
    statement = _build_work_statement(
        draft,
        agent=SimpleNamespace(id=uuid.uuid4(), name="项目经理"),
        executor_snapshot={},
    )

    snapshot = statement["product_information_architecture"]
    assert snapshot == product_information_architecture_snapshot()
    assert statement["acceptance_contract"]["owner_review_required"] is True

    agent_id = uuid.uuid4()
    original_fingerprint = _confirmation_fingerprint(draft, agent_id=agent_id)
    changed_snapshot = deepcopy(snapshot)
    changed_snapshot["catalog_sha256"] = "f" * 64
    with patch(
        "app.api.work.product_information_architecture_snapshot",
        return_value=changed_snapshot,
    ):
        changed_fingerprint = _confirmation_fingerprint(draft, agent_id=agent_id)
    assert changed_fingerprint != original_fingerprint
