#!/usr/bin/env python3
"""Deterministic loopback-only OpenAI-compatible server for local QA.

The server replaces only the external model provider. Astra still performs its
normal authentication, routing, credit accounting, Runtime, tool execution,
verification, persistence, and audit work.  It intentionally stores only
privacy-safe request-shape receipts and must never be exposed beyond loopback.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import UTC, datetime
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import time
from typing import Any
import uuid


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_RECEIPTS: deque[dict[str, Any]] = deque(maxlen=500)
_COUNTS: Counter[str] = Counter()


def _presentation_html_text(value: object) -> str:
    """Return safe, layout-stable visible copy for the local PPT fixture.

    The source-bound outline and slide spec retain the exact user text. The
    visible HTML uses presentation typography: UUIDs are abbreviated and a
    digit stays attached to the following Chinese classifier. CJK line-break
    behavior is governed by CSS and the production converter preserves the
    browser's measured lines in the editable PPTX.
    """

    rendered = html.escape(str(value), quote=True)
    rendered = re.sub(
        r"\b([0-9a-fA-F]{8})-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{6}([0-9a-fA-F]{6})\b",
        r"\1…\2",
        rendered,
    )
    rendered = re.sub(r"(?<=\d) (?=[\u3400-\u9fff])", "", rendered)
    return rendered


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict)
        )
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _tool_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in payload.get("messages") or [] if isinstance(item, dict)]


def _joined_messages(payload: dict[str, Any]) -> str:
    return "\n".join(_text(message.get("content")) for message in _messages(payload))


def _request_messages(
    payload: dict[str, Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """Return messages for the latest stage invocation of one request.

    A durable deliverable reuses its request id across outline, render, and
    revision runs. Runtime history therefore contains successful Tool calls
    from earlier stages. The latest server-owned DELIVERABLE_REQUEST marker is
    the invocation boundary; calls before it must never satisfy the new stage.
    """

    messages = _messages(payload)
    for index in range(len(messages) - 1, -1, -1):
        content = _text(messages[index].get("content"))
        if "DELIVERABLE_REQUEST=" in content and request_id in content:
            return messages[index:]
    return messages


def _called_tools(payload: dict[str, Any]) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for message in _messages(payload):
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                calls.append((str(call.get("id") or ""), function["name"]))
    return calls


def _tool_results(payload: dict[str, Any], tool_name: str) -> list[str]:
    call_ids = {call_id for call_id, name in _called_tools(payload) if name == tool_name}
    return [
        _text(message.get("content"))
        for message in _messages(payload)
        if message.get("role") == "tool"
        and str(message.get("tool_call_id") or "") in call_ids
    ]


def _request_tool_call_records(
    payload: dict[str, Any],
    request_id: str,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return Tool calls and parsed arguments for one deliverable request."""

    calls: list[tuple[str, str, dict[str, Any]]] = []
    for message in _request_messages(payload, request_id):
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                arguments_text = raw_arguments
                try:
                    decoded_arguments = json.loads(raw_arguments)
                except (json.JSONDecodeError, TypeError, ValueError):
                    decoded_arguments = {}
            elif isinstance(raw_arguments, dict):
                arguments_text = json.dumps(raw_arguments, ensure_ascii=False, sort_keys=True)
                decoded_arguments = raw_arguments
            else:
                arguments_text = ""
                decoded_arguments = {}
            if request_id in arguments_text:
                calls.append(
                    (
                        str(call.get("id") or ""),
                        function["name"],
                        decoded_arguments if isinstance(decoded_arguments, dict) else {},
                    )
                )
    return calls


def _request_tool_calls(
    payload: dict[str, Any],
    request_id: str,
) -> list[tuple[str, str]]:
    """Return only Tool call identifiers and names for one deliverable."""

    return [
        (call_id, name)
        for call_id, name, _arguments in _request_tool_call_records(payload, request_id)
    ]


def _request_tool_results(
    payload: dict[str, Any],
    request_id: str,
    tool_name: str,
) -> list[str]:
    call_ids = {
        call_id
        for call_id, name in _request_tool_calls(payload, request_id)
        if name == tool_name
    }
    return [
        _text(message.get("content"))
        for message in _request_messages(payload, request_id)
        if message.get("role") == "tool"
        and str(message.get("tool_call_id") or "") in call_ids
    ]


def _request_path_tool_results(
    payload: dict[str, Any],
    request_id: str,
    tool_name: str,
    path: str,
) -> list[str]:
    """Return results for one request-owned Tool path in call order."""

    call_ids = {
        call_id
        for call_id, name, arguments in _request_tool_call_records(payload, request_id)
        if name == tool_name and str(arguments.get("path") or "") == path
    }
    return [
        _text(message.get("content"))
        for message in _request_messages(payload, request_id)
        if message.get("role") == "tool"
        and str(message.get("tool_call_id") or "") in call_ids
    ]


def _tool_result_failed(result: str) -> bool:
    """Recognize the Runtime's human-readable typed-tool failure summaries."""

    normalized = result.casefold()
    return any(
        marker in normalized
        for marker in (
            "❌",
            "did not produce a valid",
            "content exceeds",
            "source file not found",
            '"ok":false',
            '"success":false',
        )
    )


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _candidate_ids(text: str) -> list[str]:
    marker = text.find('"candidate_agents"')
    scoped = text[marker:] if marker >= 0 else text
    values = re.findall(rf'"agent_id"\s*:\s*"({_UUID})"', scoped)
    return list(dict.fromkeys(value.lower() for value in values))


def _target_from_directory(results: list[str]) -> str | None:
    for result in reversed(results):
        matches = re.findall(rf'"target_agent_id"\s*:\s*"({_UUID})"', result)
        if matches:
            return matches[0].lower()
    return None


def _planning_response(text: str) -> str:
    candidates = _candidate_ids(text)
    if len(candidates) < 2:
        candidates = [
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        ]
    entries = [
        {
            "agent_id": candidate,
            "instruction": (
                "基于群组中的上市目标与约束，以自己的岗位视角给出可执行的四阶段里程碑、"
                "风险与验收标准；在群内公开回复，不触发外部发送或付费媒体生成。"
            ),
        }
        for candidate in candidates[:2]
    ]
    return json.dumps(
        {
            "version": 2,
            "mode": "advisory",
            "goal": "形成 ReefTotem AI 2026 秋季新品上市的跨岗位四阶段执行方案。",
            "plan_prompt": (
                "产品负责人负责定位、范围与里程碑，CEO 负责跨职能风险、资源边界和验收；"
                "两位参与者各自在群内公开提交结果。本地验收禁止外部发送、真实支付与付费媒体生成。"
            ),
            "entry_steps": entries,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normal_business_answer(text: str) -> str:
    if "四阶段" in text or "上市" in text or "ReefTotem" in text:
        return (
            "《ReefTotem AI 2026 秋季新品上市作战方案》\n\n"
            "1. 定位锁定（第1周）：面向 20–100 人的知识型团队，主张“可治理、可追溯的企业 Agent 协作”；"
            "交付 ICP、价值主张、禁用表述清单，负责人为产品经理。\n"
            "2. 方案验证（第2–3周）：用 5 家设计伙伴验证工作台、协作群组、CEO 调度和团队知识闭环；"
            "验收为每家至少完成 1 条带审计记录的任务。\n"
            "3. 发布准备（第4周）：完成销售话术、帮助中心、演示脚本和回滚预案；所有外部素材在发布前由人类审批。\n"
            "4. 上市复盘（发布后7天）：统计激活、任务完成、人工接管和失败恢复；形成继续、修正或停止决策。\n\n"
            "关键边界：本次本地验收不发送外部邮件、不创建真实支付、不调用付费图片/视频服务；"
            "任何高风险外部动作必须保留人工批准和执行收据。"
        )
    return "本地业务验证已完成：已形成可执行方案，并保留人工审批、成本和外部动作边界。"


def _deliverable_contract(text: str) -> dict[str, Any] | None:
    """Return the last server-owned deliverable contract in the prompt."""

    marker = "DELIVERABLE_REQUEST="
    decoder = json.JSONDecoder()
    start = text.rfind(marker)
    if start < 0:
        return None
    try:
        value, _ = decoder.raw_decode(text[start + len(marker) :].lstrip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _json_marker(text: str, marker: str) -> dict[str, Any] | None:
    """Return the last JSON object following a server-owned prompt marker."""

    decoder = json.JSONDecoder()
    start = text.rfind(marker)
    if start < 0:
        return None
    try:
        value, _ = decoder.raw_decode(text[start + len(marker) :].lstrip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _local_presentation_files(request_id: str) -> tuple[str, str, str]:
    headlines = [
        "ReefTotem AI 2026 秋季新品决策汇报",
        "客户问题与机会窗口",
        "产品定位与核心价值",
        "五家设计伙伴验证",
        "四阶段上市计划",
        "成功指标与审计证据",
        "风险、预算与停止条件",
        "所有者决策请求",
    ]
    bodies = [
        ["本地验收候选", "面向公司所有者、产品与市场负责人"],
        ["知识型团队跨岗位协作成本高", "任务、审批与结果证据容易割裂"],
        ["可治理、可追溯的企业 Agent 协作", "所有高风险外部动作保留人工批准"],
        ["每家完成一条工作台到审计的真实任务链", "覆盖工作台、Group、CEO 与团队知识"],
        ["定位锁定", "方案验证", "发布准备", "发布后七天复盘"],
        ["激活与任务完成", "人工接管与失败恢复", "审计收据与交付批准"],
        ["本地不发送邮件、不真实支付", "不调用付费图片或视频服务", "触发停止条件即回滚"],
        ["是否进入发布准备", "是否批准设计伙伴验证范围", "是否接受预算与停止边界"],
    ]
    layouts = ["hero", "split", "value", "partners", "timeline", "metrics", "risk", "decision"]
    visual_kinds = [
        "editable_typography",
        "editable_diagram",
        "editable_diagram",
        "editable_table",
        "editable_diagram",
        "editable_chart",
        "editable_table",
        "editable_typography",
    ]
    outline = {
        "deck_title": headlines[0],
        "audience": "公司所有者、产品与市场负责人",
        "core_message": "在受控边界内验证 ReefTotem AI 新品上市准备度。",
        "slides": [
            {
                "slide_id": f"s{index}",
                "purpose": "形成可验证的所有者决策依据",
                "headline": headline,
                "visual_intent": "使用可编辑卡片、指标或流程表达，不依赖外部媒体。",
                "evidence": ["本地工作台任务", "本地协作与审计记录"],
            }
            for index, headline in enumerate(headlines, start=1)
        ],
    }
    slide_spec = {
        "visual_plan_version": "adaptive-v1",
        "visual_policy": {
            "minimum_distinct_layouts": 8,
            "minimum_distinct_images": 0,
            "minimum_image_slides": 0,
            "minimum_picture_coverage_ratio": 0,
            "maximum_uses_per_image": 0,
            "minimum_editable_compositions": 8,
        },
        "slides": [
            {
                "slide_id": f"s{index}",
                "headline": headline,
                "slide_type": layout,
                "layout": layout,
                "visual_kind": visual_kinds[index - 1],
                "visual_asset": "editable_html_composition",
                "asset_ref": "",
                "body_points": bodies[index - 1],
                "source_refs": ["local-workbench", "local-audit"],
            }
            for index, (headline, layout) in enumerate(zip(headlines, layouts), start=1)
        ]
    }
    slides = []
    for index, (headline, body, layout) in enumerate(zip(headlines, bodies, layouts), start=1):
        cards = "".join(f"<div class='card'><b>{item}</b><span>本地可追溯证据</span></div>" for item in body)
        slides.append(
            f"<section class='slide' data-slide='s{index}' data-layout='{layout}'>"
            f"<header><small data-clawith-text-role='metadata'>REEFTOTEM AI · LOCAL RELEASE QA</small>"
            f"<h1 data-slide-title>{headline}</h1></header>"
            f"<div class='visual' data-visual>{cards}</div>"
            f"<footer data-clawith-text-role='metadata'>{index}/8 · request {request_id[:8]}</footer></section>"
        )
    html = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>"
        "*{box-sizing:border-box}body{margin:0;background:#eef1f7;font-family:Arial,'PingFang SC',sans-serif;color:#152038}"
        ".slide{width:1280px;height:720px;padding:66px 76px 46px;background:linear-gradient(135deg,#fbfcff,#edf1ff);"
        "display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}small{letter-spacing:3px;color:#5b5bd6}"
        "h1{font-size:45px;line-height:1.16;margin:18px 0 28px;max-width:1000px}.visual{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;flex:1;align-content:center}"
        ".card{min-height:105px;padding:24px;border:1px solid #cfd5ea;border-radius:18px;background:#fff;box-shadow:0 10px 30px #39426912;display:flex;flex-direction:column;gap:12px}"
        ".card b{font-size:25px}.card span{font-size:16px;color:#65708a}.slide[data-layout='timeline'] .visual,.slide[data-layout='metrics'] .visual{grid-template-columns:repeat(4,1fr)}"
        ".slide[data-layout='decision'] .visual{grid-template-columns:1fr}.slide[data-layout='risk']{background:linear-gradient(135deg,#fffaf2,#f3f0ff)}"
        "footer{font-size:13px;color:#7b849b;margin-top:24px}</style></head><body>"
        + "".join(slides)
        + "</body></html>"
    )
    return (
        json.dumps(outline, ensure_ascii=False, separators=(",", ":")),
        json.dumps(slide_spec, ensure_ascii=False, separators=(",", ":")),
        html,
    )


def _local_presentation_v2_files(
    request_id: str,
    contract: dict[str, Any],
    *,
    visual_policy: dict[str, Any],
    source_id: str,
) -> tuple[str, str, str]:
    """Build a source-bound, editable deck fixture for the v2 browser journey."""

    spec = contract.get("spec") if isinstance(contract.get("spec"), dict) else {}
    page_count = max(5, min(int(spec.get("page_count") or 8), 15))
    audience = str(spec.get("audience") or "公司所有者、产品与市场负责人").strip()
    base_headlines = [
        "ReefTotem AI 秋季新品决策汇报",
        "客户问题与业务机会",
        "产品定位与治理边界",
        "设计伙伴验证方案",
        "从定位到复盘的上市路径",
        "成功指标与审计证据",
        "风险、预算与停止条件",
        "所有者决策请求",
    ]
    while len(base_headlines) < page_count:
        base_headlines.append(f"补充决策依据 {len(base_headlines) + 1}")
    headlines = base_headlines[:page_count]
    normalized_key_points = (
        str(spec.get("key_points") or "")
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )
    evidence_lines = [
        line.strip()
        for line in normalized_key_points.splitlines()
        if line.strip()
    ]
    if not evidence_lines:
        evidence_lines = [
            "工作台、协作群组、CEO 调度、团队知识与审计共同构成业务闭环。",
            "所有外部动作都必须经过人工确认并保留执行收据。",
            "本地验收不发送邮件、不创建真实支付、不调用图片或视频供应商。",
        ]
    narrative = [
        "当前信息分散在任务、对话与交付文件中，需要统一成可追溯的经营决策链。",
        "工作台承接单一责任任务，协作群组承接跨岗位讨论，二者通过任务与审计关联。",
        "CEO 负责拆解、委派和汇总，目标 Agent 的工具能力与权限仍由服务端校验。",
        "设计伙伴验证只采信真实任务结果、人工接管记录和可复查的审计证据。",
        "上市路径按定位、验证、准备和复盘推进，每一步都定义负责人、验收与回滚条件。",
        "经营看板同时观察任务完成、失败恢复、人工批准和证据完整度，避免只看调用次数。",
        "预算边界禁止真实扣款与付费媒体调用；供应商未配置时必须明确降级或阻断。",
        "建议仅在本地业务闭环与独立测试通过后进入发布准备，生产仍需单独验收。",
    ]
    while len(narrative) < page_count:
        narrative.append("补充页面只记录已核验的本地事实、开放问题与下一步人工决策。")
    layouts = [
        "hero",
        "problem-map",
        "governance-stack",
        "partner-matrix",
        "milestone-lanes",
        "evidence-scorecard",
        "risk-guardrails",
        "decision-gate",
    ]
    while len(layouts) < page_count:
        layouts.append(f"appendix-{len(layouts) + 1}")
    visual_kinds = [
        "editable_typography",
        "editable_diagram",
        "editable_diagram",
        "editable_table",
        "editable_diagram",
        "editable_chart",
        "editable_table",
        "editable_typography",
    ]
    while len(visual_kinds) < page_count:
        visual_kinds.append("editable_diagram")

    slide_rows: list[dict[str, Any]] = []
    outline_rows: list[dict[str, Any]] = []
    html_slides: list[str] = []
    for index, headline in enumerate(headlines, start=1):
        slide_id = f"slide-{index:02d}"
        evidence = evidence_lines[(index - 1) % len(evidence_lines)]
        body_points = [
            narrative[index - 1],
            evidence,
            "页面结论必须能够回到本地任务、审批、交付或审计记录进行复核。",
        ]
        data_slide = index in {4, 6, 7}
        visual_kind = visual_kinds[index - 1]
        if data_slide and visual_kind not in {"editable_chart", "editable_table", "editable_diagram"}:
            visual_kind = "editable_table"
        layout = layouts[index - 1]
        outline_rows.append(
            {
                "slide_id": slide_id,
                "purpose": "形成可验证的所有者决策依据",
                "headline": headline,
                "evidence": [evidence],
                "visual_intent": "使用可编辑卡片、指标或流程表达，不依赖外部媒体。",
            }
        )
        slide_rows.append(
            {
                "slide_id": slide_id,
                "headline": headline,
                "layout": layout,
                "body_points": body_points,
                "visual_asset": "editable_html_composition",
                "source_refs": [source_id],
                "slide_type": layout,
                "visual_kind": visual_kind,
                "data_slide": data_slide,
                "asset_ref": "",
            }
        )
        cards = "".join(
            "<article class='card'><span class='index'>"
            f"{position:02d}</span><p>{_presentation_html_text(item)}</p></article>"
            for position, item in enumerate(body_points, start=1)
        )
        html_slides.append(
            f"<section class='slide' data-slide='{slide_id}' data-layout='{layout}'>"
            "<header><small data-clawith-text-role='metadata'>REEFTOTEM AI · LOCAL V2 CANARY</small>"
            f"<h1 data-slide-title>{headline}</h1></header>"
            f"<div class='visual' data-visual>{cards}</div>"
            f"<footer data-clawith-text-role='metadata'>{index}/{page_count} · request {request_id[:8]}</footer>"
            "</section>"
        )

    outline = {
        "deck_title": headlines[0],
        "audience": audience,
        "core_message": "在受控边界内判断 ReefTotem AI 是否进入新品发布准备。",
        "one_sentence_claim": "只有可追溯业务证据和人工审批同时成立，才进入下一阶段。",
        "storyline": ["明确问题", "验证定位", "审查证据", "控制风险", "请求决策"],
        "slides": outline_rows,
    }
    slide_spec = {
        "visual_plan_version": "adaptive-v1",
        "visual_policy": visual_policy,
        "slides": slide_rows,
    }
    html = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>"
        "*{box-sizing:border-box}body{margin:0;background:#e9edf5;font-family:Arial,'PingFang SC',sans-serif;color:#13213a}"
        ".slide{width:1280px;height:720px;padding:56px 72px 42px;background:linear-gradient(135deg,#fcfdff,#edf2ff);"
        "display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;page-break-after:always}"
        ".slide:last-child{page-break-after:auto}small{font-size:12px;letter-spacing:3px;color:#4d58c8}"
        "h1{font-size:42px;line-height:1.18;margin:14px 0 22px;max-width:1080px}.visual{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;flex:1;align-content:center}"
        ".card{min-height:230px;padding:23px;border:1px solid #ccd5ec;border-radius:18px;background:#fff;box-shadow:0 12px 34px #33426014}"
        ".card .index{display:inline-flex;width:34px;height:34px;align-items:center;justify-content:center;border-radius:10px;background:#5b5bd6;color:#fff;font-size:16px}"
        ".card p{font-size:19px;line-height:1.5;margin:18px 0 0;line-break:strict;word-break:normal;overflow-wrap:break-word}.slide[data-layout='hero'] .visual,.slide[data-layout='decision-gate'] .visual{grid-template-columns:1.2fr 1fr 1fr}"
        ".slide[data-layout='risk-guardrails']{background:linear-gradient(135deg,#fffaf0,#f0efff)}footer{font-size:12px;color:#69758e;margin-top:18px}"
        "</style></head><body>" + "".join(html_slides) + "</body></html>"
    )
    return (
        json.dumps(outline, ensure_ascii=False, separators=(",", ":")),
        json.dumps(slide_spec, ensure_ascii=False, separators=(",", ":")),
        html,
    )


def _local_presentation_v2_response(
    payload: dict[str, Any],
    contract: dict[str, Any],
    text: str,
) -> tuple[str, str, list[dict[str, Any]]] | None:
    if contract.get("work_type") != "presentation" or contract.get("workflow_id") != "builtin.presentation.v2":
        return None
    request_id = str(contract.get("request_id") or "").strip()
    if not re.fullmatch(_UUID, request_id):
        return None
    tools = set(_tool_names(payload))
    if "write_file" not in tools:
        return "deliverable_tools_missing", "本地 V2 交付工具不完整，未写入大纲或声称交付。", []

    source_inventory = _json_marker(text, "SOURCE_INVENTORY=") or {}
    entries = source_inventory.get("entries") if isinstance(source_inventory.get("entries"), list) else []
    source_id = next(
        (
            str(entry.get("source_id"))
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("source_id") or "").strip()
        ),
        "src-01",
    )
    visual_policy = _json_marker(text, "PRESENTATION_VISUAL_POLICY=") or {
        "minimum_distinct_layouts": 4,
        "minimum_distinct_images": 0,
        "minimum_image_slides": 0,
        "minimum_picture_coverage_ratio": 0,
        "maximum_uses_per_image": 0,
        "minimum_editable_compositions": int((contract.get("spec") or {}).get("page_count") or 8),
    }
    outline, slide_spec, html = _local_presentation_v2_files(
        request_id,
        contract,
        visual_policy=visual_policy,
        source_id=source_id,
    )
    base = f"workspace/deliverables/{request_id}"
    request_calls = _request_tool_calls(payload, request_id)
    called = [name for _, name in request_calls]
    stage_markers = (
        (text.rfind("In this run you only plan the deck"), "outline_draft"),
        (text.rfind("You are producing an approved deck"), "slide_render"),
        (
            text.rfind("You are executing a page-targeted revision of an approved deck"),
            "slide_revision",
        ),
    )
    marker_position, stage = max(stage_markers, key=lambda item: item[0])
    if marker_position < 0:
        stage = "outline_draft"

    if stage == "outline_draft":
        write_steps = [
            (f"{base}/outline.json", outline),
            (f"{base}/slide_spec.json", slide_spec),
        ]
        writes = called.count("write_file")
        if writes < len(write_steps):
            path, content = write_steps[writes]
            return "deliverable_v2_outline_write", "", [
                _tool_call("write_file", {"path": path, "content": content, "mode": "overwrite"})
            ]
        return (
            "deliverable_v2_outline_complete",
            f"V2 大纲与页面规格已写入 {base}/outline.json 和 {base}/slide_spec.json；尚未渲染，等待人工批准。",
            [],
        )

    required = {"read_file", "write_file", "convert_html_to_pptx"}
    if not required <= tools:
        return "deliverable_tools_missing", "本地 V2 渲染工具不完整，未声称已经生成 PPT。", []
    read_paths = [f"{base}/outline.json", f"{base}/slide_spec.json"]
    if stage == "slide_revision":
        # The production revision contract requires the current source to be
        # read before applying the page-scoped update.  The deterministic QA
        # fixture still re-emits a complete source for reproducibility, but it
        # must exercise the same read gate as the real Agent prompt.
        read_paths.append(f"{base}/presentation.html")
    read_calls = called.count("read_file")
    if read_calls < len(read_paths):
        path = read_paths[read_calls]
        return "deliverable_v2_plan_read", "", [_tool_call("read_file", {"path": path})]
    html_path = f"{base}/presentation.html"
    html_chunks = [html[index : index + 5000] for index in range(0, len(html), 5000)]
    html_results = _request_path_tool_results(
        payload,
        request_id,
        "write_file",
        html_path,
    )
    successful_html_chunks = sum(
        not _tool_result_failed(result) for result in html_results
    )
    if successful_html_chunks < len(html_chunks):
        chunk_index = successful_html_chunks
        return "deliverable_v2_render_write", "", [
            _tool_call(
                "write_file",
                {
                    "path": html_path,
                    "content": html_chunks[chunk_index],
                    "mode": "overwrite" if chunk_index == 0 else "append",
                },
            )
        ]
    conversion_results = _request_tool_results(payload, request_id, "convert_html_to_pptx")
    conversion_failed = bool(conversion_results and _tool_result_failed(conversion_results[-1]))
    explicit_recovery_requested = any(
        marker in text.casefold()
        for marker in (
            "重新运行 v2 演示文稿转换",
            "重新校验 v2 演示文稿",
            "rerun the v2 presentation conversion",
            "revalidate the v2 presentation",
        )
    )
    if "convert_html_to_pptx" not in called or (
        (conversion_failed or explicit_recovery_requested)
        and called.count("convert_html_to_pptx") < 4
    ):
        editability = str((contract.get("spec") or {}).get("editability_contract") or "editable")
        render_mode = "visual" if editability == "visual_fidelity" else "hybrid_editable"
        return "deliverable_v2_convert", "", [
            _tool_call(
                "convert_html_to_pptx",
                {
                    "source_path": f"{base}/presentation.html",
                    "target_path": f"{base}/result.pptx",
                    "design_width": 1280,
                    "design_height": 720,
                    "render_mode": render_mode,
                    "render_scale": 2,
                    "expected_page_count": int((contract.get("spec") or {}).get("page_count") or 8),
                    "outline_path": f"{base}/outline.json",
                    "slide_spec_path": f"{base}/slide_spec.json",
                },
            )
        ]
    if not conversion_results or conversion_failed:
        return (
            "deliverable_v2_conversion_failed",
            "V2 本地转换未通过结构与语义校验，不能声称正式交付成功。",
            [],
        )
    return (
        "deliverable_v2_complete",
        f"V2 本地正式交付文件已生成：{base}/result.pptx。请以产物校验和人工批准状态为准。",
        [],
    )


def _local_presentation_response(
    payload: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]]] | None:
    if contract.get("work_type") != "presentation" or contract.get("workflow_id") != "builtin.presentation.v1":
        return None
    request_id = str(contract.get("request_id") or "").strip()
    if not re.fullmatch(_UUID, request_id):
        return None
    tools = set(_tool_names(payload))
    required = {"list_files", "write_file", "convert_html_to_pptx"}
    if not required <= tools:
        return "deliverable_tools_missing", "本地交付工具不完整，未声称已经生成 PPT。", []
    base = f"workspace/deliverables/{request_id}"
    called = [name for _, name in _request_tool_calls(payload, request_id)]
    outline, slide_spec, html = _local_presentation_files(request_id)
    if "list_files" not in called:
        return "deliverable_inspect", "", [_tool_call("list_files", {"path": base})]
    writes = called.count("write_file")
    write_steps = [
        (f"{base}/outline.json", outline),
        (f"{base}/slide_spec.json", slide_spec),
        (f"{base}/presentation.html", html),
    ]
    if writes < len(write_steps):
        path, content = write_steps[writes]
        return "deliverable_write", "", [
            _tool_call("write_file", {"path": path, "content": content, "mode": "overwrite"})
        ]
    conversion_results = _request_tool_results(
        payload,
        request_id,
        "convert_html_to_pptx",
    )
    conversion_failed = bool(conversion_results and _tool_result_failed(conversion_results[-1]))
    if "convert_html_to_pptx" not in called or (
        conversion_failed and called.count("convert_html_to_pptx") < 2
    ):
        return "deliverable_convert", "", [
            _tool_call(
                "convert_html_to_pptx",
                {
                    "source_path": f"{base}/presentation.html",
                    "target_path": f"{base}/result.pptx",
                    "design_width": 1280,
                    "design_height": 720,
                    "render_mode": "hybrid_editable",
                    "render_scale": 2,
                    "expected_page_count": int((contract.get("spec") or {}).get("page_count") or 8),
                    "outline_path": f"{base}/outline.json",
                    "slide_spec_path": f"{base}/slide_spec.json",
                },
            )
        ]
    if not conversion_results or conversion_failed:
        return (
            "deliverable_conversion_failed",
            "本地转换工具未生成通过校验的 PPTX；已保留源文件，但不能声称正式交付成功。",
            [],
        )
    return (
        "deliverable_complete",
        f"本地正式交付文件已由内置转换工具生成：{base}/result.pptx。请以 Astra 的产物校验与人工批准状态为准。",
        [],
    )


def _choose_response(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    text = _joined_messages(payload)
    tools = set(_tool_names(payload))
    system_text = "\n".join(
        _text(message.get("content"))
        for message in _messages(payload)
        if message.get("role") == "system"
    )

    if "commit_thread_summary" in tools:
        return "thread_compact", "", [
            _tool_call(
                "commit_thread_summary",
                {
                    "task_goal_and_constraints": (
                        "继续当前 Astra 本地业务请求；本地验证不等同于生产验证，"
                        "不得执行真实支付、外部邮件或付费供应商动作。"
                    ),
                    "completed_work_and_results": (
                        "保留 Thread 中已有工具收据所证明的安全完成结果；"
                        "具体文件、状态和标识仍以服务端记录为准。"
                    ),
                    "key_decisions_and_evidence": (
                        "只采信服务器状态、工具结果、产物校验和人工审批；"
                        "文件生成不等同于正式交付。"
                    ),
                    "unfinished_or_blocked": (
                        "当前业务请求仍在运行，未完成事项由未压缩消息和服务端状态继续承载。"
                    ),
                    "next_actions": (
                        "只继续当前请求的下一步；Runtime 路由、权限和审批仍由服务端决定。"
                    ),
                },
            )
        ]

    if "commit_session_context" in tools:
        return "session_compact", "", [
            _tool_call(
                "commit_session_context",
                {
                    "summary": "继续当前 Astra 本地验收，所有结论以可追溯服务端证据为准。",
                    "requirements": [
                        "本地验证不得表述为生产或外部供应商验证。",
                        "真实支付、外部邮件和付费媒体不在本地验收授权范围。",
                    ],
                    "decisions": ["文件生成与人工确认交付是两个独立状态。"],
                    "open_items": ["继续处理当前会话中尚未完成的业务步骤。"],
                    "evidence_refs": [],
                    "workspace_refs": [],
                },
            )
        ]

    if "Astra's internal multi-Agent planning component" in system_text:
        return "group_planning", _planning_response(text), []

    if "capability_probe" in tools:
        return "capability_probe", "", [_tool_call("capability_probe", {"value": "ok"})]

    marker = "LOCAL_QA_CEO_DELEGATE"
    is_ceo = "# CEO Runtime Authority" in system_text
    is_delegated = '"a2a_mode":"task_delegate"' in text.replace(" ", "")
    called = [name for _, name in _called_tools(payload)]

    if marker in text and is_ceo:
        if "query_directory" not in called:
            return "ceo_directory", "", [
                _tool_call(
                    "query_directory",
                    {"query": "产品经理", "member_type": "agent", "limit": 10},
                )
            ]
        if "send_message_to_agent" not in called:
            target = _target_from_directory(_tool_results(payload, "query_directory"))
            if target:
                return "ceo_delegate", "", [
                    _tool_call(
                        "send_message_to_agent",
                        {
                            "target_agent_id": target,
                            "message": (
                                "LOCAL_QA_CEO_DELEGATE：请给出秋季新品的核心定位、目标客户、"
                                "两项验收标准；先用 query_directory 核验组织上下文，再回复结论。"
                            ),
                            "msg_type": "task_delegate",
                            "delegation_contract": {
                                "version": 1,
                                "title": "秋季新品定位验证",
                                "objective": "形成可供 CEO 汇总的新品定位与两项验收标准。",
                                "required_capabilities": ["query_directory"],
                                "acceptance_criteria": [
                                    "明确目标客户与核心价值主张",
                                    "给出两项可检验且不依赖外部付费服务的验收标准",
                                ],
                                "expected_artifacts": [],
                                "requires_artifact_delivery": False,
                            },
                        },
                    )
                ]
            return "ceo_directory_repair", "未找到可联系且具备所需能力的产品经理，未执行委派。", []
        delegation_results = _tool_results(payload, "send_message_to_agent")
        delegation_text = "\n".join(delegation_results)
        if "ceo_coordination_required" in delegation_text:
            return (
                "ceo_dispatch_blocked",
                "委派被服务端拒绝，CEO 仍为观察型，未联系产品经理。",
                [],
            )
        if "ceo_target_capability_missing" in delegation_text:
            return (
                "ceo_target_capability_blocked",
                "CEO 协调权限已生效，但目标产品经理缺少委派合同要求的工具授权；未创建目标任务。",
                [],
            )
        if "[A2A:" in delegation_text or "❌" in delegation_text:
            return (
                "ceo_dispatch_rejected",
                "CEO 协调权限已生效，但本次委派被服务端前置检查拒绝；未创建目标任务。",
                [],
            )
        return (
            "ceo_synthesis",
            "CEO 已收到产品经理的可验证委派结果：核心客户为 20–100 人知识型团队，核心价值为可治理、可追溯的企业 Agent 协作；验收以完成真实任务闭环和保留审计收据为准。",
            [],
        )

    if marker in text and is_delegated:
        if "query_directory" not in called:
            return "delegate_directory", "", [
                _tool_call("query_directory", {"query": "CEO", "member_type": "agent", "limit": 10})
            ]
        return (
            "delegate_result",
            "定位结论：目标客户是 20–100 人、需要跨岗位协作与审计的知识型团队；核心价值是把任务、Agent 委派、人工审批和交付证据统一在可追溯闭环中。验收标准：一是完成一次工作台到任务完成的全链路并可查审计；二是 CEO 委派必须产生目标 Agent 的工具收据和结构化结果。",
            [],
        )

    deliverable_contract = _deliverable_contract(text)
    if deliverable_contract is not None:
        deliverable_response = _local_presentation_v2_response(
            payload,
            deliverable_contract,
            text,
        )
        if deliverable_response is not None:
            return deliverable_response
        deliverable_response = _local_presentation_response(payload, deliverable_contract)
        if deliverable_response is not None:
            return deliverable_response

    if len(text.strip()) < 80 and "Say 'ok' and nothing else" in text:
        return "connectivity", "ok", []
    return "business_answer", _normal_business_answer(text), []


def _completion(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind, content, tool_calls = _choose_response(payload)
    if kind in {"delegate_directory", "delegate_result"}:
        # Give browser QA enough time to observe the target employee's live state.
        time.sleep(8.0)
    now = int(time.time())
    response: dict[str, Any] = {
        "id": f"chatcmpl-local-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": now,
        "model": str(payload.get("model") or "astra-local-qa"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content or None,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 64, "completion_tokens": 64, "total_tokens": 128},
    }
    return kind, response


def _receipt(payload: dict[str, Any], kind: str) -> None:
    tools = _tool_names(payload)
    item = {
        "at": datetime.now(UTC).isoformat(),
        "kind": kind,
        "model": str(payload.get("model") or ""),
        "stream": bool(payload.get("stream")),
        "message_count": len(_messages(payload)),
        "tool_names": tools,
    }
    _RECEIPTS.append(item)
    _COUNTS[kind] += 1


class _Handler(BaseHTTPRequestHandler):
    server_version = "AstraLocalQA/1.0"

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "scope": "loopback-only", "requests": sum(_COUNTS.values())})
            return
        if self.path.rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "astra-local-qa",
                            "object": "model",
                            "created": 0,
                            "owned_by": "local-qa",
                        }
                    ],
                },
            )
            return
        if self.path == "/__qa/receipts":
            self._json(200, {"counts": dict(_COUNTS), "receipts": list(_RECEIPTS)})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        kind, response = _completion(payload)
        _receipt(payload, kind)
        if not payload.get("stream"):
            self._json(200, response)
            return

        message = response["choices"][0]["message"]
        delta = {key: value for key, value in message.items() if key != "role"}
        event = {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": response["choices"][0]["finish_reason"],
                }
            ],
            "usage": response["usage"],
        }
        frames = (
            f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
            "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(frames)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(frames)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("local QA stub may only bind to loopback")
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"Astra local QA model stub listening on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
