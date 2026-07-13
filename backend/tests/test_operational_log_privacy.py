"""Regression contracts for privacy-safe operational logging."""

from __future__ import annotations

import ast
import contextlib
import io
import logging
import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from app.core.logging_config import (
    InterceptHandler,
    _privacy_safe_filter,
    _privacy_safe_format,
    get_trace_id,
    privacy_safe_shape,
    set_trace_id,
)
from app.core.middleware import TraceIdMiddleware
from app.services import agent_tools, production_issue_monitor
from app.services.agent_tools import (
    _minimax_operation_log_level,
    _record_minimax_tool_product_issue,
)


APP_ROOT = Path(__file__).parents[1] / "app"
LOGGING_CONFIG = APP_ROOT / "core" / "logging_config.py"
SENSITIVE_LOG_PATTERNS = (
    re.compile(
        r"(?:reply_text|user_text|assistant_response|recognition|raw_args|arguments|"
        r"messages_payload|stdout|stderr|x_api_key|app_key|app_secret|access_token|"
        r"info_data|resp_data|token_data)\s*\[:"
    ),
    re.compile(r"str\([^\n)]*\)\s*\[:"),
    re.compile(r"json\.dumps\((?:raw_args|messages_payload|arguments)"),
    re.compile(r"_safe_error\("),
    re.compile(r"(?:data\.)?session_id\s*\[:"),
    re.compile(r"repr\(raw_args"),
    re.compile(
        r"\{(?:data|payload|result|response|message|content|reply_text|user_text|"
        r"assistant_response|stdout|stderr|info_data|resp_data|token_data)\}"
    ),
)

SENSITIVE_VALUE_PARTS = (
    "prompt",
    "content",
    "reply_text",
    "user_text",
    "assistant_response",
    "raw_args",
    "raw_data",
    "arguments",
    "payload",
    "stdout",
    "stderr",
    "secret",
    "access_token",
    "api_key",
    "app_key",
    "app_secret",
    "email",
    "phone",
    "sender_name",
    "sender_id",
    "external_id",
    "open_id",
    "union_id",
    "service_url",
    "file_path",
    "workspace_path",
    "rel_path",
    "filename",
    "title",
    "recognition",
    "cookie",
    "error_message",
    "error_msg",
    "error_description",
    "message_id",
    "msg_id",
    "source_message_id",
    "image_id",
    "img_id",
    "connection_id",
    "conn_id",
    "response_text",
    "result_msg",
    "write_result",
    "execution_result",
    "primary_result",
)
SENSITIVE_VALUE_NAMES = {
    "api_error",
    "body",
    "content",
    "ctx",
    "data",
    "message",
    "name",
    "path",
    "payload",
    "reply",
    "resp",
    "response",
    "result",
    "text",
    "url",
}
SAFE_SHAPE_FUNCTIONS = {"bool", "isinstance", "len", "privacy_safe_shape", "type"}
SAFE_DIAGNOSTIC_NAMES = {
    "action_type",
    "agent_id",
    "cache_read_tokens",
    "channel_type",
    "command_name",
    "conversation_type",
    "error_code",
    "error_type",
    "media_type",
    "model_model",
    "model_provider",
    "msg_type",
    "name",  # Background task names are code-owned; ORM .name attributes remain checked.
    "output_tokens",
    "provider_type",
    "receive_id_type",
    "route_template",
    "token_limit",
    "tool_name",
    "total_tokens",
}
SAFE_GET_KEYS = {
    "action",
    "chat_type",
    "code",
    "errcode",
    "error",
    "error_code",
    "event_type",
    "exit_code",
    "has_more",
    "kind",
    "modality",
    "model",
    "operation",
    "provider",
    "receive_id_type",
    "reservation_id",
    "status",
    "status_code",
    "success",
    "task_id",
    "tier",
    "type",
}
SAFE_SUBSCRIPT_KEYS = {
    "error_code",
    "error_type",
    "has_output",
    "incomplete",
    "model",
    "status",
}


def _logger_call_sources() -> list[tuple[Path, int, str]]:
    calls: list[tuple[Path, int, str]] = []
    for path in APP_ROOT.rglob("*.py"):
        if "skill_creator_files" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
                continue
            if not _is_logger_owner(node.func.value):
                continue
            calls.append((path, node.lineno, ast.get_source_segment(source, node) or ""))
    return calls


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_logger_owner(node: ast.AST) -> bool:
    if not isinstance(node, ast.Name):
        return False
    return node.id == "logger" or node.id.endswith("_logger") or node.id.endswith("_log")


def _is_sensitive_name(value: str) -> bool:
    normalized = value.lower()
    result_like = normalized.endswith("_result") or normalized.startswith("result_")
    return (
        result_like
        or normalized in SENSITIVE_VALUE_NAMES
        or any(part in normalized for part in SENSITIVE_VALUE_PARTS)
    )


def _unsafe_logged_values(
    node: ast.AST,
    tainted_names: set[str] | None = None,
) -> list[str]:
    tainted_names = tainted_names or set()
    if isinstance(node, ast.IfExp):
        return _unsafe_logged_values(node.body, tainted_names) + _unsafe_logged_values(
            node.orelse,
            tainted_names,
        )

    if isinstance(node, ast.Call):
        if _node_name(node.func) in SAFE_SHAPE_FUNCTIONS:
            return []
        if isinstance(node.func, ast.Attribute) and node.func.attr == "json":
            return ["call:json"]
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            key_node = node.args[0]
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                key = key_node.value.lower()
                if key in SAFE_GET_KEYS:
                    return []
                return [f"get:{key}"] if _is_sensitive_name(key) else []

    if isinstance(node, ast.Subscript):
        key_node = node.slice
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            key = key_node.value.lower()
            if key in SAFE_SUBSCRIPT_KEYS:
                return []
            if _is_sensitive_name(key):
                return [f"key:{key}"]

    if isinstance(node, ast.Attribute):
        return [f"attr:{node.attr}"] if _is_sensitive_name(node.attr) else []

    if isinstance(node, ast.Name):
        if node.id in SAFE_DIAGNOSTIC_NAMES:
            return []
        if node.id in tainted_names:
            return [f"tainted:{node.id}"]
        if _is_sensitive_name(node.id):
            return [f"name:{node.id}"]
        return []

    violations: list[str] = []
    for child in ast.iter_child_nodes(node):
        violations.extend(_unsafe_logged_values(child, tainted_names))
    return violations


_SCOPE_NODES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    names: set[str] = set()
    for child in ast.iter_child_nodes(node):
        names.update(_target_names(child))
    return names


def _assignment_parts(node: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    if isinstance(node, ast.NamedExpr):
        return [node.target], node.value
    return [], None


def _scope_contents(scope: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    local_nodes: list[ast.AST] = []
    nested_scopes: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_NODES):
                nested_scopes.append(child)
                continue
            local_nodes.append(child)
            visit(child)

    visit(scope)
    return local_nodes, nested_scopes


def _scope_tainted_names(
    scope: ast.AST,
    local_nodes: list[ast.AST],
    inherited: set[str],
) -> set[str]:
    tainted = set(inherited)
    args = getattr(scope, "args", None)
    if args is not None:
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if _is_sensitive_name(arg.arg):
                tainted.add(arg.arg)
        if args.vararg and _is_sensitive_name(args.vararg.arg):
            tainted.add(args.vararg.arg)
        if args.kwarg and _is_sensitive_name(args.kwarg.arg):
            tainted.add(args.kwarg.arg)

    changed = True
    while changed:
        changed = False
        for node in local_nodes:
            targets, value = _assignment_parts(node)
            if value is None:
                continue
            names = set().union(*(_target_names(target) for target in targets))
            if not names:
                continue
            source_is_sensitive = bool(_unsafe_logged_values(value, tainted))
            newly_tainted = {
                name
                for name in names
                if source_is_sensitive or _is_sensitive_name(name)
            } - tainted
            if newly_tainted:
                tainted.update(newly_tainted)
                changed = True
    return tainted


def _tree_sensitive_log_violations(tree: ast.AST, label: str) -> list[str]:
    violations: list[str] = []

    def inspect_scope(scope: ast.AST, inherited: set[str]) -> None:
        local_nodes, nested_scopes = _scope_contents(scope)
        tainted = _scope_tainted_names(scope, local_nodes, inherited)
        for node in local_nodes:
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {
                "debug",
                "info",
                "warning",
                "error",
                "exception",
                "critical",
            }:
                continue
            if not _is_logger_owner(node.func.value):
                continue
            unsafe: list[str] = []
            for arg in node.args:
                unsafe.extend(_unsafe_logged_values(arg, tainted))
            for keyword in node.keywords:
                unsafe.extend(_unsafe_logged_values(keyword.value, tainted))
            if unsafe:
                violations.append(f"{label}:{node.lineno}: {sorted(set(unsafe))}")

        for nested_scope in nested_scopes:
            inspect_scope(nested_scope, tainted)

    inspect_scope(tree, set())
    return violations


def _direct_sensitive_log_violations() -> list[str]:
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "skill_creator_files" in path.parts or "scripts" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations.extend(
            _tree_sensitive_log_violations(tree, str(path.relative_to(APP_ROOT.parent)))
        )
    return violations


def test_privacy_safe_shape_never_contains_values_or_mapping_keys():
    secret = "customer prompt with private content"
    assert privacy_safe_shape(secret) == f"str_chars={len(secret)}"
    assert privacy_safe_shape({"api_key": secret, "prompt": secret}) == "mapping_items=2"
    assert secret not in privacy_safe_shape(secret)
    assert "api_key" not in privacy_safe_shape({"api_key": secret})


def test_trace_id_context_never_reuses_untrusted_header_content():
    untrusted_trace_id = "customer@example.com\nINJECTED_LOG_LINE"
    safe_trace_id = set_trace_id(untrusted_trace_id)
    assert safe_trace_id == get_trace_id()
    assert len(safe_trace_id) == 12
    assert all(char in "0123456789abcdef" for char in safe_trace_id)
    assert untrusted_trace_id not in safe_trace_id


def test_trace_middleware_uses_route_template_and_ignores_client_trace_content():
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.get("/privacy-probe/{item_id}")
    async def privacy_probe(item_id: str):
        return {"item_id": item_id}

    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    path_secret = "CUSTOMER_PATH_SECRET_82fe"
    query_secret = "QUERY_TOKEN_SECRET_31aa"
    client_trace_secret = "CLIENT_TRACE_SECRET_004d"
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/privacy-probe/{path_secret}?token={query_secret}",
                headers={"X-Trace-Id": client_trace_secret},
            )
        logger.complete()
    finally:
        logger.remove(sink_id)

    response_trace_id = response.headers["X-Trace-Id"]
    assert re.fullmatch(r"[0-9a-f]{12}", response_trace_id)
    assert response_trace_id != client_trace_secret
    rendered = output.getvalue()
    assert path_secret not in rendered
    assert query_secret not in rendered
    assert client_trace_secret not in rendered
    assert "GET /privacy-probe/{item_id} 200" in rendered


def test_bound_trace_extra_cannot_override_internal_trace_id():
    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    bound_secret = "BOUND_TRACE_SECRET@example.com"
    safe_trace_id = set_trace_id("123456789abc")
    try:
        logger.bind(trace_id=bound_secret).info("bound trace probe")
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert bound_secret not in rendered
    assert safe_trace_id in rendered


def test_exception_logging_omits_exception_values_and_local_variables():
    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    exception_secret = "PROVIDER_RESPONSE_SECRET_7d45"
    local_secret = "CUSTOMER_PROMPT_SECRET_2f31"
    try:
        try:
            assert local_secret
            raise RuntimeError(exception_secret)
        except RuntimeError:
            logger.exception("privacy probe: {}", exception_secret)
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert exception_secret not in rendered
    assert local_secret not in rendered
    assert "<redacted_exception>" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_loguru_sink_failure_report_cannot_reveal_raw_exception_value():
    stderr = io.StringIO()
    exception_secret = "SINK_FAILURE_EXCEPTION_SECRET_91c4"

    def failing_sink(_message):
        raise OSError("synthetic sink failure")

    sink_id = logger.add(
        failing_sink,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
        catch=True,
        enqueue=True,
    )
    try:
        with contextlib.redirect_stderr(stderr):
            try:
                raise RuntimeError(exception_secret)
            except RuntimeError:
                logger.exception("sink failure probe: {}", exception_secret)
            logger.complete()
    finally:
        logger.remove(sink_id)

    rendered = stderr.getvalue()
    assert exception_secret not in rendered
    assert "sink failure probe: <redacted_exception>" in rendered
    assert "'exception': None" in rendered


def test_non_exception_log_inside_except_also_omits_exception_value():
    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    exception_secret = "OAUTH_RESPONSE_SECRET_8ab1"
    try:
        try:
            raise ValueError(exception_secret)
        except ValueError as exc:
            logger.warning("oauth fallback failed: {}", exc)
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert exception_secret not in rendered
    assert "<redacted_exception>" in rendered
    assert "exception_type=ValueError" in rendered


def test_exception_with_broken_string_representation_stays_diagnostic_safe():
    class BrokenStringError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("SECRET_FROM_EXCEPTION_STRING")

    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    try:
        try:
            raise BrokenStringError()
        except BrokenStringError:
            logger.exception("broken exception probe")
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert "SECRET_FROM_EXCEPTION_STRING" not in rendered
    assert "broken exception probe" in rendered
    assert "exception_type=BrokenStringError" in rendered


def test_malformed_standard_log_does_not_emit_raw_message_or_arguments():
    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    argument_secret = "STANDARD_LOG_ARGUMENT_SECRET_f32a"
    record = logging.LogRecord(
        "privacy-probe",
        logging.ERROR,
        __file__,
        1,
        "provider secret=%d",
        (argument_secret,),
        None,
    )
    try:
        InterceptHandler().emit(record)
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert argument_secret not in rendered
    assert "provider secret" not in rendered
    assert "Standard log formatting failed" in rendered
    assert "args_shape=sequence_items=1" in rendered


def test_third_party_standard_log_emits_only_safe_diagnostic_shape():
    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    secret_url = "https://provider.example/callback/private-token?code=OAUTH_SECRET"
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: GET %s",
        (secret_url,),
        None,
    )
    record.status_code = 503
    try:
        InterceptHandler().emit(record)
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert secret_url not in rendered
    assert "OAUTH_SECRET" not in rendered
    assert "Standard log event source=httpx" in rendered
    assert "message_chars=" in rendered
    assert "level=INFO" in rendered
    assert "args_shape=sequence_items=1" in rendered
    assert "status_code=503" in rendered
    assert "exception_type=none" in rendered


def test_application_named_standard_log_cannot_bypass_boundary():
    output = io.StringIO()
    sink_id = logger.add(
        output,
        format=_privacy_safe_format,
        filter=_privacy_safe_filter,
        backtrace=False,
        diagnose=False,
    )
    application_secret = "APP_STANDARD_LOG_SECRET_7e91"
    record = logging.LogRecord(
        "app.privacy_probe",
        logging.WARNING,
        __file__,
        1,
        "provider payload=%s",
        (application_secret,),
        None,
    )
    try:
        InterceptHandler().emit(record)
    finally:
        logger.remove(sink_id)

    rendered = output.getvalue()
    assert application_secret not in rendered
    assert "provider payload" not in rendered
    assert "Standard log event source=app.privacy_probe" in rendered


def test_operational_logger_calls_do_not_embed_known_sensitive_payloads():
    violations: list[str] = []
    for path, line, call_source in _logger_call_sources():
        if any(pattern.search(call_source) for pattern in SENSITIVE_LOG_PATTERNS):
            violations.append(f"{path.relative_to(APP_ROOT.parent)}:{line}: {call_source}")
    assert not violations, "\n".join(violations)


def test_operational_logger_calls_only_emit_safe_shapes_for_sensitive_values():
    violations = _direct_sensitive_log_violations()
    assert not violations, "\n".join(violations)


def test_sensitive_value_aliases_cannot_bypass_source_contract():
    tree = ast.parse(
        """
def emit(provider_payload):
    renamed_value = provider_payload
    logger.info("provider result={}", renamed_value)
"""
    )
    violations = _tree_sensitive_log_violations(tree, "synthetic")
    assert violations == ["synthetic:4: ['tainted:renamed_value']"]


def test_runtime_does_not_bypass_logger_with_raw_traceback_prints():
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "skill_creator_files" in path.parts or "scripts" in path.parts:
            continue
        if "traceback.print_" in path.read_text(encoding="utf-8"):
            violations.append(str(path.relative_to(APP_ROOT.parent)))
    assert not violations, "\n".join(violations)


def test_background_audit_logs_do_not_persist_exception_values():
    background_services = (
        APP_ROOT / "services" / "heartbeat.py",
        APP_ROOT / "services" / "scheduler.py",
        APP_ROOT / "services" / "supervision_reminder.py",
    )
    unsafe_error_value = re.compile(
        r"write_audit_log\([\s\S]{0,240}?[\"']error[\"']\s*:\s*str\("
    )
    violations = [
        str(path.relative_to(APP_ROOT.parent))
        for path in background_services
        if unsafe_error_value.search(path.read_text(encoding="utf-8"))
    ]
    assert not violations, "\n".join(violations)


def test_central_sink_disables_raw_exception_diagnostics():
    source = LOGGING_CONFIG.read_text(encoding="utf-8")
    tree = ast.parse(source)
    logger_add = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
        and node.func.attr == "add"
    )
    keywords = {keyword.arg: keyword.value for keyword in logger_add.keywords}
    assert isinstance(keywords["diagnose"], ast.Constant) and keywords["diagnose"].value is False
    assert isinstance(keywords["backtrace"], ast.Constant) and keywords["backtrace"].value is False
    assert isinstance(keywords["format"], ast.Name) and keywords["format"].id == "_privacy_safe_format"
    assert isinstance(keywords["filter"], ast.Name) and keywords["filter"].id == "_privacy_safe_filter"


def test_minimax_plan_capacity_is_warning_but_unknown_failure_is_error():
    assert _minimax_operation_log_level(ValueError("MiniMax API error (2056): quota")) == "warning"
    assert _minimax_operation_log_level(RuntimeError("provider socket closed")) == "error"


@pytest.mark.asyncio
async def test_minimax_plan_capacity_uses_warning_severity_in_issue_center(monkeypatch):
    captured: list[dict] = []

    async def fake_tenant(_agent_id):
        return None

    async def fake_record_production_issue(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(agent_tools, "_get_minimax_tenant_uuid", fake_tenant)
    monkeypatch.setattr(
        production_issue_monitor,
        "record_production_issue",
        fake_record_production_issue,
    )

    agent_id = uuid.uuid4()
    await _record_minimax_tool_product_issue(
        agent_id,
        "video",
        error=ValueError("MiniMax API error (2056): quota"),
    )
    await _record_minimax_tool_product_issue(
        agent_id,
        "video",
        error=RuntimeError("provider socket closed"),
    )

    assert captured[0]["error_code"] == "2056"
    assert captured[0]["severity"] == "warning"
    assert captured[1]["severity"] == "error"
