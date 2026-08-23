#!/usr/bin/env python3
"""Production candidate commercial workflow smoke test.

This smoke intentionally requires real credentials. It validates the money
surface through HTTP instead of unit-test fixtures:

1. company owner can read aggregates and owner-only billing data;
2. ordinary member can read only entitlements and attributed personal usage;
3. ordinary member is rejected from every sensitive billing endpoint;
4. a real Work task executes, is reviewed, and remains idempotent for Credits;
5. a real collaboration Group is durable and visible to its ordinary member;
6. the workforce topology projects the completed task;
7. platform admin can run read-only ledger/payment reconciliation and exports;
8. release identity is bound to the isolated candidate slot.

The browser portion is intentionally implemented by the pinned, isolated
``deploy/browser-smoke`` image. This standard-library runner never injects
credentials into JavaScript or process arguments.

Environment variables:
  SMOKE_TENANT_EMAIL / SMOKE_TENANT_PASSWORD / SMOKE_TENANT_ID
  SMOKE_MEMBER_EMAIL / SMOKE_MEMBER_PASSWORD
  SMOKE_PLATFORM_ADMIN_EMAIL / SMOKE_PLATFORM_ADMIN_PASSWORD
  SMOKE_API_BASE (default: http://127.0.0.1:3008/api)
  SMOKE_FRONTEND_URL (default: http://127.0.0.1:3008)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = "http://127.0.0.1:3008/api"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:3008"
REQUIRED_CREDENTIAL_KEYS = {
    "SMOKE_TENANT_EMAIL",
    "SMOKE_TENANT_PASSWORD",
    "SMOKE_TENANT_ID",
    "SMOKE_MEMBER_EMAIL",
    "SMOKE_MEMBER_PASSWORD",
    "SMOKE_PLATFORM_ADMIN_EMAIL",
    "SMOKE_PLATFORM_ADMIN_PASSWORD",
}


class SmokeFailure(RuntimeError):
    def __init__(self, stage: str, detail: Any):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail!r}")


def json_print(payload: dict[str, Any], *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream or sys.stdout)


def call_api(
    method: str,
    api_base: str,
    path: str,
    data: dict[str, Any] | None = None,
    token: str | None = None,
    *,
    accept: str = "application/json",
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    body = None
    request_headers = {"Accept": accept, **(headers or {})}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        api_base.rstrip("/") + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            if "text/csv" in content_type or accept == "text/csv":
                return resp.status, raw.decode("utf-8")
            text = raw.decode("utf-8")
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise SmokeFailure("service_reachable", {"api_base": api_base, "error": str(exc)}) from exc


def require(condition: bool, stage: str, detail: Any) -> None:
    if not condition:
        raise SmokeFailure(stage, detail)


def summarize_reconciliation(
    payload: Any,
    *,
    checked_field: str,
    stage: str,
) -> dict[str, int]:
    """Fail closed on reconciliation drift without copying issue rows to logs."""
    require(
        isinstance(payload, dict),
        stage,
        {"code": "invalid_reconciliation_response"},
    )
    checked = payload.get(checked_field)
    issues = payload.get("issues")
    require(
        type(checked) is int and checked >= 0,
        stage,
        {"code": "invalid_checked_count", "field": checked_field},
    )
    require(
        isinstance(issues, list),
        stage,
        {"code": "invalid_issues_collection"},
    )
    require(
        not issues,
        stage,
        {
            "code": "reconciliation_issues_detected",
            "issue_count": len(issues),
        },
    )
    return {checked_field: checked, "issue_count": 0}


def summarize_work_executor_preflight(payload: Any) -> dict[str, Any]:
    """Require the real tenant's personal assistant to pass Work intake.

    The evidence intentionally excludes Agent IDs, names, and route details.
    This is a read-only preflight: it neither creates a Task nor calls a model.
    """

    require(
        isinstance(payload, dict),
        "work_executor_preflight",
        {"code": "invalid_work_preflight_response"},
    )
    capability_status = payload.get("capability_status")
    reasons = payload.get("reasons")
    fingerprint = payload.get("confirmation_fingerprint")
    require(
        capability_status == "available" and reasons == [],
        "work_executor_preflight",
        {
            "code": "personal_assistant_route_unavailable",
            "capability_status": capability_status,
            "reason_count": len(reasons) if isinstance(reasons, list) else None,
        },
    )
    require(
        isinstance(fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None,
        "work_executor_preflight",
        {"code": "invalid_confirmation_fingerprint"},
    )
    return {"capability_status": "available", "reason_count": 0}


def summarize_manual_billing_config(payload: Any) -> dict[str, Any]:
    """Require the production claim to match the deliberately manual provider."""

    require(
        isinstance(payload, dict),
        "billing_manual_semantics",
        {"code": "invalid_billing_config"},
    )
    expected = {
        "provider": "manual",
        "status": "manual",
        "checkout_enabled": True,
        "native_payment_enabled": False,
        "webhook_ready": False,
    }
    require(
        all(payload.get(key) == value for key, value in expected.items()),
        "billing_manual_semantics",
        {
            "code": "unexpected_billing_provider_contract",
            "provider": payload.get("provider"),
            "status": payload.get("status"),
        },
    )
    return expected


def _uuid_text(value: Any, stage: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SmokeFailure(stage, {"code": "valid_uuid_required"}) from exc


def _credit_snapshot(summary: Any, transactions: Any, *, stage: str) -> dict[str, Any]:
    """Return an identity-free ledger snapshot for replay comparison."""

    require(isinstance(summary, dict), stage, {"code": "invalid_subscription_summary"})
    numeric_fields = ("balance", "available_balance", "reserved", "consumed_credits")
    for field in numeric_fields:
        value = summary.get(field)
        require(
            type(value) is int and value >= 0,
            stage,
            {"code": "invalid_credit_field", "field": field},
        )
    require(isinstance(transactions, list), stage, {"code": "invalid_credit_transactions"})
    transaction_ids: list[str] = []
    for row in transactions:
        require(isinstance(row, dict), stage, {"code": "invalid_credit_transaction"})
        transaction_ids.append(_uuid_text(row.get("id"), stage))
    transaction_ids.sort()
    return {
        **{field: summary[field] for field in numeric_fields},
        "transaction_count": len(transaction_ids),
        "transaction_digest": hashlib.sha256("\n".join(transaction_ids).encode()).hexdigest(),
    }


def _read_credit_snapshot(api_base: str, token: str, *, stage: str) -> dict[str, Any]:
    summary_status, summary = call_api("GET", api_base, "/subscription/summary", token=token)
    require(
        summary_status == 200,
        stage,
        {
            "code": "credit_snapshot_unavailable",
            "summary_status": summary_status,
        },
    )
    transactions: list[Any] = []
    page_size = 100
    for page in range(1, 101):
        ledger_status, batch = call_api(
            "GET",
            api_base,
            f"/subscription/credit-transactions?page={page}&limit={page_size}",
            token=token,
        )
        require(
            ledger_status == 200 and isinstance(batch, list),
            stage,
            {
                "code": "credit_snapshot_unavailable",
                "ledger_status": ledger_status,
                "ledger_page": page,
            },
        )
        transactions.extend(batch)
        if len(batch) < page_size:
            break
    else:
        raise SmokeFailure(stage, {"code": "credit_snapshot_page_limit_exceeded"})
    return _credit_snapshot(summary, transactions, stage=stage)


def _poll_work_completion(
    api_base: str,
    token: str,
    task_id: str,
    marker: str,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        status, detail = call_api(
            "GET",
            api_base,
            f"/work/tasks/{task_id}/detail",
            token=token,
        )
        require(status == 200 and isinstance(detail, dict), "work_task_execution", {"status": status})
        axes = detail.get("status_axes")
        summary = detail.get("summary")
        require(
            isinstance(axes, dict) and isinstance(summary, dict),
            "work_task_execution",
            {"code": "invalid_work_detail"},
        )
        last_status = str(axes.get("execution") or "unknown")
        if last_status in {"failed", "cancelled"}:
            raise SmokeFailure(
                "work_task_execution",
                {"code": "terminal_failure", "execution": last_status},
            )
        if last_status == "completed":
            latest_update = summary.get("latest_update")
            require(
                isinstance(latest_update, str) and marker in latest_update,
                "work_task_output_marker",
                {"code": "expected_output_marker_missing"},
            )
            _uuid_text(summary.get("run_id"), "work_task_execution")
            return detail
        time.sleep(2)
    raise SmokeFailure(
        "work_task_execution",
        {"code": "timeout", "last_execution": last_status},
    )


def _poll_topology(
    api_base: str,
    token: str,
    *,
    agent_id: str,
    task_title: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, payload = call_api(
            "GET",
            api_base,
            "/workforce/topology?window_hours=24",
            token=token,
        )
        require(status == 200 and isinstance(payload, dict), "workforce_topology", {"status": status})
        nodes = payload.get("nodes")
        scope = payload.get("scope_contract")
        require(isinstance(nodes, list), "workforce_topology", {"code": "invalid_nodes"})
        assistant = next(
            (
                node
                for node in nodes
                if isinstance(node, dict) and str(node.get("id")) == agent_id
            ),
            None,
        )
        work = assistant.get("work") if isinstance(assistant, dict) else None
        if (
            isinstance(work, dict)
            and work.get("title") == task_title
            and work.get("stage") == "completed"
            and scope
            == {
                "execution": "company_visible_redacted",
                "work": "viewer_owned",
                "analytics": "governor_or_managed",
            }
        ):
            return {"node_count": len(nodes), "assistant_visible": True, "completed_work_visible": True}
        time.sleep(1)
    raise SmokeFailure(
        "workforce_topology",
        {"code": "completed_work_not_projected"},
    )


def login(
    api_base: str,
    email: str,
    password: str,
    stage: str,
    *,
    tenant_id: str | None = None,
    tenant_fallback_id: str | None = None,
) -> dict[str, Any]:
    request_data = {"login_identifier": email, "password": password}
    if tenant_id:
        request_data["tenant_id"] = tenant_id
    status, payload = call_api(
        "POST",
        api_base,
        "/auth/login",
        request_data,
    )
    if (
        status == 200
        and tenant_fallback_id
        and isinstance(payload, dict)
        and payload.get("requires_tenant_selection") is True
    ):
        tenants = payload.get("tenants")
        require(
            isinstance(tenants, list)
            and any(
                isinstance(candidate, dict)
                and str(candidate.get("tenant_id")) == tenant_fallback_id
                for candidate in tenants
            ),
            stage,
            {"code": "target_tenant_not_available"},
        )
        status, payload = call_api(
            "POST",
            api_base,
            "/auth/login",
            {
                "login_identifier": email,
                "password": password,
                "tenant_id": tenant_fallback_id,
            },
        )
    require(
        status == 200 and isinstance(payload, dict) and payload.get("access_token"),
        stage,
        {"code": "unexpected_login_response", "status": status},
    )
    return payload


def load_credentials(path_value: str | None) -> dict[str, str]:
    if not path_value:
        return {}
    path = Path(path_value)
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise SmokeFailure("credentials_file", {"present": False}) from exc
    require(
        path.is_file() and not path.is_symlink() and 0 < stat.st_size <= 16_384,
        "credentials_file",
        {"regular_file": path.is_file() and not path.is_symlink(), "size": stat.st_size},
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("credentials_file", {"format": "invalid_json"}) from exc
    require(isinstance(payload, dict), "credentials_file", {"format": "object_required"})
    require(set(payload) == REQUIRED_CREDENTIAL_KEYS, "credentials_file", {"keys": "exact_required_keys_only"})
    for key in REQUIRED_CREDENTIAL_KEYS:
        value = payload[key]
        require(
            isinstance(value, str) and 0 < len(value) <= 4096,
            "credentials_file",
            {"invalid_key": key},
        )
    return payload


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    credentials = load_credentials(args.credentials_file)
    tenant_email = args.tenant_email or credentials.get("SMOKE_TENANT_EMAIL") or os.getenv("SMOKE_TENANT_EMAIL")
    tenant_password = (
        args.tenant_password or credentials.get("SMOKE_TENANT_PASSWORD") or os.getenv("SMOKE_TENANT_PASSWORD")
    )
    tenant_id_value = args.tenant_id or credentials.get("SMOKE_TENANT_ID") or os.getenv("SMOKE_TENANT_ID")
    member_email = args.member_email or credentials.get("SMOKE_MEMBER_EMAIL") or os.getenv("SMOKE_MEMBER_EMAIL")
    member_password = (
        args.member_password or credentials.get("SMOKE_MEMBER_PASSWORD") or os.getenv("SMOKE_MEMBER_PASSWORD")
    )
    admin_email = (
        args.platform_admin_email
        or credentials.get("SMOKE_PLATFORM_ADMIN_EMAIL")
        or os.getenv("SMOKE_PLATFORM_ADMIN_EMAIL")
    )
    admin_password = (
        args.platform_admin_password
        or credentials.get("SMOKE_PLATFORM_ADMIN_PASSWORD")
        or os.getenv("SMOKE_PLATFORM_ADMIN_PASSWORD")
    )
    api_base = (args.api_base or os.getenv("SMOKE_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    frontend_url = (args.frontend_url or os.getenv("SMOKE_FRONTEND_URL") or DEFAULT_FRONTEND_URL).rstrip("/")

    require(
        bool(tenant_email and tenant_password),
        "tenant_credentials_configured",
        "Set SMOKE_TENANT_EMAIL and SMOKE_TENANT_PASSWORD",
    )
    try:
        tenant_id = str(uuid.UUID(str(tenant_id_value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SmokeFailure("tenant_id_configured", {"code": "valid_uuid_required"}) from exc
    require(
        bool(admin_email and admin_password),
        "platform_admin_credentials_configured",
        "Set SMOKE_PLATFORM_ADMIN_EMAIL and SMOKE_PLATFORM_ADMIN_PASSWORD",
    )
    require(
        bool(member_email and member_password),
        "member_credentials_configured",
        "Set SMOKE_MEMBER_EMAIL and SMOKE_MEMBER_PASSWORD",
    )

    expected_identity = (args.expected_version, args.expected_commit, args.expected_release_id)
    require(
        all(expected_identity) or not any(expected_identity),
        "release_identity_arguments",
        "expected version, commit, and release ID must be supplied together",
    )
    require(
        isinstance(args.evidence_nonce, str)
        and len(args.evidence_nonce) == 32
        and all(character in "0123456789abcdef" for character in args.evidence_nonce),
        "evidence_nonce",
        "evidence nonce must contain 32 lowercase hexadecimal characters",
    )
    evidence_namespace = uuid.UUID(args.evidence_nonce)
    evidence_prefix = f"RTQA-{args.evidence_nonce[:8].upper()}"

    summary: dict[str, Any] = {
        "evidence_schema_version": 3,
        "evidence_kind": "release_business_api",
        "api_base": api_base,
        "frontend_url": frontend_url,
        "evidence_nonce": args.evidence_nonce,
        "checks": [],
    }
    status, release_identity = call_api("GET", api_base, "/version")
    require(
        status == 200 and isinstance(release_identity, dict),
        "release_identity",
        {
            "status": status,
            "body": release_identity,
        },
    )
    if all(expected_identity):
        require(
            release_identity.get("version") == args.expected_version
            and release_identity.get("commit") == args.expected_commit
            and release_identity.get("release_id") == args.expected_release_id,
            "release_identity",
            release_identity,
        )
    summary["release_identity"] = release_identity
    summary["checks"].append("candidate_release_identity_ok")

    tenant_login = login(
        api_base,
        tenant_email,
        tenant_password,
        "tenant_login",
        tenant_id=tenant_id,
    )
    tenant_token = tenant_login["access_token"]
    summary["checks"].append("tenant_login_ok")

    for path, stage in [
        ("/auth/me", "tenant_me"),
        ("/subscription/config", "client_billing_config"),
        ("/subscription/plans", "client_plans"),
        ("/subscription/summary", "client_subscription_summary"),
        ("/subscription/credit-transactions", "client_credit_transactions"),
        ("/subscription/orders", "client_orders"),
        ("/subscription/credit-packs", "client_credit_packs"),
    ]:
        status, body = call_api("GET", api_base, path, token=tenant_token)
        require(
            status == 200,
            stage,
            {"code": "unexpected_http_status", "status": status},
        )
        if stage == "tenant_me":
            require(
                isinstance(body, dict) and str(body.get("tenant_id")) == tenant_id,
                "tenant_scope",
                {"code": "unexpected_tenant_context"},
            )
            summary["checks"].append("tenant_scope_ok")
            capabilities = set(body.get("effective_capabilities") or [])
            require(
                {"company.billing.view", "company.billing.manage"}.issubset(capabilities),
                "tenant_billing_manage_capability",
                {"code": "company_owner_billing_capabilities_required"},
            )
            summary["checks"].append("tenant_billing_manage_capability_ok")
        if stage == "client_billing_config":
            summary["billing_mode"] = summarize_manual_billing_config(body)
            summary["checks"].append("billing_manual_semantics_ok")
        summary["checks"].append(f"{stage}_ok")
        if stage == "client_subscription_summary":
            require(
                isinstance(body, dict) and "balance" in body and "available_balance" in body,
                stage,
                {"code": "invalid_subscription_summary"},
            )
            summary["subscription_summary"] = {
                "plan_code": body.get("plan_code"),
                "balance": body.get("balance"),
                "available_balance": body.get("available_balance"),
                "reserved": body.get("reserved"),
                "seats_used": body.get("seats_used"),
                "seats_total": body.get("seats_total"),
            }

    member_login = login(
        api_base,
        member_email,
        member_password,
        "member_login",
        tenant_id=tenant_id,
    )
    member_token = member_login["access_token"]
    summary["checks"].append("member_login_ok")

    status, member_me = call_api("GET", api_base, "/auth/me", token=member_token)
    member_capabilities = set(member_me.get("effective_capabilities") or []) if isinstance(member_me, dict) else set()
    require(
        status == 200
        and str(member_me.get("tenant_id")) == tenant_id
        and "company.billing.view" not in member_capabilities
        and "company.billing.manage" not in member_capabilities,
        "member_scope",
        {"code": "ordinary_member_without_billing_authority_required", "status": status},
    )
    for path in [
        "/subscription/plans",
        "/subscription/credit-packs",
        "/subscription/my-entitlements",
        "/subscription/usage/me",
    ]:
        status, body = call_api("GET", api_base, path, token=member_token)
        require(status == 200, "member_personal_usage", {"path": path, "status": status})
        if path == "/subscription/usage/me":
            require(
                isinstance(body, dict)
                and "consumed_credits" in body
                and not {"balance", "orders", "transactions", "tokens_used"}.intersection(body),
                "member_personal_usage",
                {"code": "unsafe_personal_usage_projection"},
            )
    summary["checks"].append("member_personal_usage_ok")

    sensitive_paths = [
        "/subscription/subscriptions",
        "/subscription/usage",
        "/subscription/credits",
        "/subscription/seats",
        "/subscription/summary",
        "/subscription/credit-transactions",
        "/subscription/orders",
        "/subscription/billing/profile",
    ]
    for path in sensitive_paths:
        status, _ = call_api("GET", api_base, path, token=member_token)
        require(status == 403, "member_sensitive_billing_denied", {"path": path, "status": status})
    summary["checks"].append("member_sensitive_billing_denied_ok")

    require(isinstance(member_me, dict), "member_scope", {"code": "invalid_member_identity"})
    member_user_id = _uuid_text(member_me.get("id"), "member_scope")

    work_marker = f"{evidence_prefix}-WORK"
    task_title = f"{work_marker} 商业发布验收"
    task_draft = {
        "title": task_title,
        "intent": (
            "完成一次真实的商业发布验收。第一行必须原样输出："
            f"{work_marker}\n随后用中文给出三条可执行的 SaaS 客户启用建议，每条一句。"
        ),
        "work_type": "general",
        "priority": "low",
        "routing_mode": "manual",
        "executor_kind": "personal_assistant",
        "acceptance_contract": {
            "version": 1,
            "criteria": [
                f"结果第一行原样包含 {work_marker}",
                "结果给出三条可执行的 SaaS 客户启用建议",
            ],
            "required_sections": [],
            "forbidden_terms": [],
            "result_language": "zh-CN",
            "length": {"unit": "cjk_characters", "maximum": 500},
            "evidence_required": False,
            "owner_review_required": True,
        },
    }
    credits_before = _read_credit_snapshot(
        api_base,
        tenant_token,
        stage="credits_before_work",
    )
    require(
        credits_before["reserved"] == 0,
        "credits_before_work",
        {"code": "preexisting_credit_reservation"},
    )

    status, body = call_api(
        "POST",
        api_base,
        "/work/tasks/preflight",
        task_draft,
        token=tenant_token,
    )
    require(
        status == 200,
        "work_executor_preflight",
        {"code": "unexpected_http_status", "status": status},
    )
    summary["work_executor_preflight"] = summarize_work_executor_preflight(body)
    summary["checks"].append("work_executor_preflight_ok")
    require(isinstance(body, dict), "work_executor_preflight", {"code": "invalid_response"})
    proposal = body.get("executor_proposal")
    require(isinstance(proposal, dict), "work_executor_preflight", {"code": "missing_proposal"})
    assistant_agent_id = _uuid_text(proposal.get("agent_id"), "work_executor_preflight")
    confirmation_fingerprint = str(body.get("confirmation_fingerprint") or "")

    work_request_id = str(uuid.uuid5(evidence_namespace, "release-work-task"))
    work_create_payload = {
        **task_draft,
        "client_request_id": work_request_id,
        "confirmation_fingerprint": confirmation_fingerprint,
    }
    status, created_work = call_api(
        "POST",
        api_base,
        "/work/tasks",
        work_create_payload,
        token=tenant_token,
    )
    require(
        status == 201 and isinstance(created_work, dict) and created_work.get("created") is True,
        "work_task_create",
        {"code": "task_not_created", "status": status},
    )
    created_item = created_work.get("item")
    require(isinstance(created_item, dict), "work_task_create", {"code": "missing_work_item"})
    task_id = _uuid_text(created_item.get("task_id") or created_item.get("id"), "work_task_create")
    completed_detail = _poll_work_completion(
        api_base,
        tenant_token,
        task_id,
        work_marker,
    )
    summary["checks"].extend(["work_task_executed_ok", "work_task_output_marker_ok"])

    completed_summary = completed_detail["summary"]
    run_id = _uuid_text(completed_summary.get("run_id"), "work_task_execution")
    credits_after_work = _read_credit_snapshot(
        api_base,
        tenant_token,
        stage="credits_after_work",
    )
    require(
        credits_after_work["reserved"] == 0
        and credits_after_work["consumed_credits"] > credits_before["consumed_credits"]
        and credits_after_work["balance"] < credits_before["balance"]
        and credits_after_work["transaction_count"] > credits_before["transaction_count"],
        "credits_after_work",
        {"code": "real_task_did_not_settle_positive_usage"},
    )

    status, replayed_work = call_api(
        "POST",
        api_base,
        "/work/tasks",
        work_create_payload,
        token=tenant_token,
    )
    require(
        status == 201
        and isinstance(replayed_work, dict)
        and replayed_work.get("created") is False
        and str((replayed_work.get("item") or {}).get("task_id")) == task_id,
        "work_task_create_idempotency",
        {"code": "task_create_replay_not_deduplicated", "status": status},
    )
    time.sleep(2)
    credits_after_replay = _read_credit_snapshot(
        api_base,
        tenant_token,
        stage="credits_after_replay",
    )
    require(
        credits_after_replay == credits_after_work,
        "credits_exactly_once",
        {"code": "task_replay_changed_credits_or_ledger"},
    )
    summary["checks"].extend(["work_task_create_idempotency_ok", "credits_exactly_once_ok"])

    review_request_id = str(uuid.uuid5(evidence_namespace, "release-work-review"))
    review_payload = {
        "run_id": run_id,
        "action": "approve",
        "comment": "Release QA verified the requested marker and actionable business output.",
        "client_request_id": review_request_id,
    }
    status, review = call_api(
        "POST",
        api_base,
        f"/work/tasks/{task_id}/result-review",
        review_payload,
        token=tenant_token,
    )
    require(
        status == 200 and isinstance(review, dict) and review.get("created") is True,
        "work_task_result_review",
        {"code": "review_not_created", "status": status},
    )
    status, replayed_review = call_api(
        "POST",
        api_base,
        f"/work/tasks/{task_id}/result-review",
        review_payload,
        token=tenant_token,
    )
    require(
        status == 200
        and isinstance(replayed_review, dict)
        and replayed_review.get("created") is False,
        "work_task_result_review",
        {"code": "review_replay_not_deduplicated", "status": status},
    )
    status, reviewed_detail = call_api(
        "GET",
        api_base,
        f"/work/tasks/{task_id}/detail",
        token=tenant_token,
    )
    require(
        status == 200
        and isinstance(reviewed_detail, dict)
        and isinstance(reviewed_detail.get("summary"), dict)
        and reviewed_detail["summary"].get("result_review_status") == "approved",
        "work_task_result_review",
        {"code": "approved_review_not_projected"},
    )
    summary["checks"].append("work_task_result_review_ok")

    topology_summary = _poll_topology(
        api_base,
        tenant_token,
        agent_id=assistant_agent_id,
        task_title=task_title,
    )
    summary["checks"].append("workforce_topology_refresh_ok")

    status, user_candidates = call_api(
        "GET",
        api_base,
        "/groups/member-candidates?participant_type=user&limit=100",
        token=tenant_token,
    )
    require(status == 200 and isinstance(user_candidates, list), "group_candidates", {"status": status})
    member_candidate = next(
        (
            candidate
            for candidate in user_candidates
            if isinstance(candidate, dict) and str(candidate.get("participant_ref_id")) == member_user_id
        ),
        None,
    )
    require(
        isinstance(member_candidate, dict),
        "group_candidates",
        {"code": "release_group_human_member_unavailable"},
    )
    member_participant_id = _uuid_text(member_candidate.get("participant_id"), "group_candidates")
    group_name = f"{evidence_prefix}-GROUP 发布验收协作组"
    status, group = call_api(
        "POST",
        api_base,
        "/groups",
        {
            "name": group_name,
            "description": "Release QA synthetic collaboration boundary.",
            "member_participant_ids": [member_participant_id],
        },
        token=tenant_token,
    )
    require(status == 201 and isinstance(group, dict), "group_create", {"status": status})
    group_id = _uuid_text(group.get("id"), "group_create")
    status, members = call_api("GET", api_base, f"/groups/{group_id}/members", token=tenant_token)
    require(
        status == 200
        and isinstance(members, list)
        and len(members) >= 2
        and all(
            isinstance(member, dict) and member.get("participant_type") == "user"
            for member in members
        ),
        "group_members",
        {"code": "expected_group_members_missing", "status": status},
    )
    status, primary_session = call_api(
        "POST",
        api_base,
        f"/groups/{group_id}/sessions",
        {"title": f"{evidence_prefix}-GROUP-SESSION 发布验收会话"},
        token=tenant_token,
    )
    require(
        status == 201
        and isinstance(primary_session, dict)
        and primary_session.get("is_primary") is True,
        "group_sessions",
        {"code": "primary_session_not_created", "status": status},
    )
    group_session_id = _uuid_text(primary_session.get("id"), "group_sessions")
    group_marker = f"{evidence_prefix}-GROUP-MESSAGE"
    group_message_id = str(uuid.uuid5(evidence_namespace, "release-group-message"))
    group_message_payload = {
        "content": f"{group_marker}：请确认本次发布验收的协作记录已持久化。",
        "mentions": [],
        "message_id": group_message_id,
    }
    group_message_path = f"/groups/{group_id}/sessions/{group_session_id}/messages"
    status, group_intake = call_api(
        "POST", api_base, group_message_path, group_message_payload, token=tenant_token
    )
    require(
        status == 201
        and isinstance(group_intake, dict)
        and group_intake.get("created") is True
        and group_intake.get("dispatch_kind") == "none",
        "group_message",
        {"code": "group_message_not_created", "status": status},
    )
    status, group_replay = call_api(
        "POST", api_base, group_message_path, group_message_payload, token=tenant_token
    )
    require(
        status == 201 and isinstance(group_replay, dict) and group_replay.get("created") is False,
        "group_message_idempotency",
        {"code": "group_message_replay_not_deduplicated", "status": status},
    )
    status, owner_messages = call_api("GET", api_base, group_message_path, token=tenant_token)
    require(
        status == 200
        and isinstance(owner_messages, list)
        and any(
            isinstance(message, dict)
            and str(message.get("id")) == group_message_id
            and group_marker in str(message.get("content") or "")
            for message in owner_messages
        ),
        "group_message_persistence",
        {"code": "owner_cannot_restore_group_message", "status": status},
    )
    status, member_groups = call_api("GET", api_base, "/groups", token=member_token)
    require(
        status == 200
        and isinstance(member_groups, list)
        and any(isinstance(item, dict) and str(item.get("id")) == group_id for item in member_groups),
        "group_member_visibility",
        {"code": "ordinary_member_cannot_see_group", "status": status},
    )
    status, member_messages = call_api("GET", api_base, group_message_path, token=member_token)
    require(
        status == 200
        and isinstance(member_messages, list)
        and any(
            isinstance(message, dict)
            and str(message.get("id")) == group_message_id
            and group_marker in str(message.get("content") or "")
            for message in member_messages
        ),
        "group_member_visibility",
        {"code": "ordinary_member_cannot_restore_group_message", "status": status},
    )
    summary["checks"].extend(
        ["group_persistence_ok", "group_member_visibility_ok", "group_message_idempotency_ok"]
    )

    status, final_subscription = call_api(
        "GET", api_base, "/subscription/summary", token=tenant_token
    )
    require(status == 200 and isinstance(final_subscription, dict), "subscription_summary_final", {"status": status})
    summary["subscription_summary"] = {
        "plan_code": final_subscription.get("plan_code"),
        "balance": final_subscription.get("balance"),
        "available_balance": final_subscription.get("available_balance"),
        "reserved": final_subscription.get("reserved"),
        "seats_used": final_subscription.get("seats_used"),
        "seats_total": final_subscription.get("seats_total"),
    }
    summary["business_flow"] = {
        "work": {
            "execution_status": "completed",
            "output_marker_verified": True,
            "create_replayed": True,
            "result_review_status": "approved",
            "review_replayed": True,
        },
        "group": {
            "member_count": len(members),
            "owner_message_persisted": True,
            "member_visibility": True,
            "message_replayed": True,
        },
        "topology": topology_summary,
        "credits": {
            "consumed_delta": credits_after_work["consumed_credits"] - credits_before["consumed_credits"],
            "transaction_delta": credits_after_work["transaction_count"] - credits_before["transaction_count"],
            "reserved_before": credits_before["reserved"],
            "reserved_after": credits_after_replay["reserved"],
            "replay_balance_delta": credits_after_replay["balance"] - credits_after_work["balance"],
            "replay_transaction_delta": (
                credits_after_replay["transaction_count"] - credits_after_work["transaction_count"]
            ),
        },
    }

    admin_login = login(
        api_base,
        admin_email,
        admin_password,
        "platform_admin_login",
        tenant_fallback_id=tenant_id,
    )
    admin_token = admin_login["access_token"]
    summary["checks"].append("platform_admin_login_ok")

    for path, stage, checked_field in [
        (
            "/saas/reconciliation/ledger",
            "saas_ledger_reconciliation",
            "checked_tenants",
        ),
        (
            "/saas/reconciliation/payments",
            "saas_payment_reconciliation",
            "checked_orders",
        ),
    ]:
        status, body = call_api("GET", api_base, path, token=admin_token)
        require(
            status == 200,
            stage,
            {"code": "unexpected_http_status", "status": status},
        )
        summary[stage] = summarize_reconciliation(
            body,
            checked_field=checked_field,
            stage=stage,
        )
        summary["checks"].append(f"{stage}_ok")

    for path, stage in [
        ("/saas/orders/export.csv", "orders_csv_export"),
        ("/saas/credit-transactions/export.csv", "credit_transactions_csv_export"),
    ]:
        status, body = call_api("GET", api_base, path, token=admin_token, accept="text/csv")
        require(
            status == 200 and isinstance(body, str) and "," in body,
            stage,
            {"code": "unexpected_export_response", "status": status},
        )
        summary["checks"].append(f"{stage}_ok")

    summary["ok"] = True
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Production subscription/billing smoke")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--frontend-url", default=None)
    parser.add_argument("--tenant-email", default=None)
    parser.add_argument("--tenant-password", default=None)
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--member-email", default=None)
    parser.add_argument("--member-password", default=None)
    parser.add_argument("--platform-admin-email", default=None)
    parser.add_argument("--platform-admin-password", default=None)
    parser.add_argument("--credentials-file", default=None)
    parser.add_argument("--expected-version", default=None)
    parser.add_argument("--expected-commit", default=None)
    parser.add_argument("--expected-release-id", default=None)
    parser.add_argument("--evidence-nonce", default=None)
    args = parser.parse_args()

    try:
        json_print(run_smoke(args))
        return 0
    except SmokeFailure as exc:
        json_print({"ok": False, "stage": exc.stage, "detail": exc.detail}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
