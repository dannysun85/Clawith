"""Contracts for the loopback-only model stub used by local release QA."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_stub() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_openai_qa_stub.py"
    spec = importlib.util.spec_from_file_location("astra_local_openai_qa_stub", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _append_tool_result(
    payload: dict[str, Any],
    response: tuple[str, str, list[dict[str, Any]]],
    *,
    content: str = '{"ok":true}',
) -> None:
    _, _, calls = response
    assert len(calls) == 1
    call = calls[0]
    payload["messages"].append({"role": "assistant", "content": None, "tool_calls": [call]})
    payload["messages"].append(
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": content,
        }
    )


def test_local_qa_stub_honors_thread_compact_tool_contract() -> None:
    stub = _load_stub()
    payload = {
        "tools": [{"function": {"name": "commit_thread_summary"}}],
        "messages": [
            {
                "role": "system",
                "content": "Call commit_thread_summary exactly once.",
            }
        ],
    }

    kind, content, calls = stub._choose_response(payload)

    assert kind == "thread_compact"
    assert content == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "commit_thread_summary"
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert set(arguments) == {
        "task_goal_and_constraints",
        "completed_work_and_results",
        "key_decisions_and_evidence",
        "unfinished_or_blocked",
        "next_actions",
    }
    assert all(isinstance(value, str) and value for value in arguments.values())


def test_local_qa_stub_honors_session_compact_tool_contract() -> None:
    stub = _load_stub()
    payload = {
        "tools": [{"function": {"name": "commit_session_context"}}],
        "messages": [
            {
                "role": "system",
                "content": "Call commit_session_context exactly once.",
            }
        ],
    }

    kind, content, calls = stub._choose_response(payload)

    assert kind == "session_compact"
    assert content == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "commit_session_context"
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert set(arguments) == {
        "summary",
        "requirements",
        "decisions",
        "open_items",
        "evidence_refs",
        "workspace_refs",
    }
    assert arguments["summary"]


def test_local_presentation_qa_uses_real_workspace_and_conversion_tools() -> None:
    stub = _load_stub()
    request_id = "3685a7db-24a0-4d83-bebb-a9f058fc115d"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "spec": {"page_count": 8},
    }
    tool_names = ["list_files", "write_file", "convert_html_to_pptx"]
    payload: dict[str, Any] = {
        "model": "astra-local-qa",
        "tools": [{"function": {"name": name}} for name in tool_names],
        "messages": [
            {
                "role": "system",
                "content": f"DELIVERABLE_REQUEST={json.dumps(contract)}",
            }
        ],
    }

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_inspect"
    inspect_arguments = json.loads(response[2][0]["function"]["arguments"])
    assert inspect_arguments["path"] == f"workspace/deliverables/{request_id}"
    _append_tool_result(payload, response)

    expected_paths = ["outline.json", "slide_spec.json", "presentation.html"]
    for expected_path in expected_paths:
        response = stub._choose_response(payload)
        assert response[0] == "deliverable_write"
        arguments = json.loads(response[2][0]["function"]["arguments"])
        assert arguments["path"] == f"workspace/deliverables/{request_id}/{expected_path}"
        assert 0 < len(arguments["content"]) <= 6000
        if expected_path == "slide_spec.json":
            slide_spec = json.loads(arguments["content"])
            assert slide_spec["visual_plan_version"] == "adaptive-v1"
            assert slide_spec["visual_policy"]["minimum_editable_compositions"] == 8
            assert all(slide["visual_kind"].startswith("editable_") for slide in slide_spec["slides"])
        _append_tool_result(payload, response)

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_convert"
    arguments = json.loads(response[2][0]["function"]["arguments"])
    assert arguments["target_path"] == f"workspace/deliverables/{request_id}/result.pptx"
    assert arguments["render_mode"] == "hybrid_editable"
    assert arguments["expected_page_count"] == 8
    assert arguments["outline_path"].endswith("/outline.json")
    assert arguments["slide_spec_path"].endswith("/slide_spec.json")
    _append_tool_result(payload, response)

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_complete"
    assert response[2] == []
    assert f"workspace/deliverables/{request_id}/result.pptx" in response[1]


def test_local_presentation_revision_rebuilds_from_the_business_evidence_contract() -> None:
    stub = _load_stub()
    request_id = "3685a7db-24a0-4d83-bebb-a9f058fc115d"
    task_ids = (
        "73649f8b-beca-4678-9084-b4ad9b6653ca",
        "114ad3c0-da6d-47c0-a868-06fbf929978a",
    )
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "spec": {"page_count": 8},
        "revision_instruction": (
            "整份重做为 30 天设计伙伴试运营管理层决策汇报；"
            f"每页回链 Task {task_ids[0]} 或 Task {task_ids[1]}"
        ),
        "revision_targets": [],
    }

    outline_raw, slide_spec_raw, rendered_html = stub._local_presentation_files(
        request_id,
        contract,
    )
    outline = json.loads(outline_raw)
    slide_spec = json.loads(slide_spec_raw)

    assert outline["deck_title"] == "30 天设计伙伴试运营管理层决策汇报"
    assert "2026 秋季新品" not in rendered_html
    assert "覆盖工作台、Group、CEO" not in rendered_html
    assert "CEO、Creative V2" in rendered_html
    assert "伙伴 A–B" in rendered_html
    assert "伙伴 C–D" in rendered_html
    assert "伙伴 E" in rendered_html
    assert "余额必须等于全量 ledger delta" in rendered_html
    assert "987 Credits" not in rendered_html
    assert "13 笔" not in rendered_html
    assert "不映射为生产结论" in rendered_html
    assert "footer{font-size:16px" in rendered_html
    assert "<footer data-clawith-text-role='metadata'>" not in rendered_html
    expected_refs = [f"Task {task_id}" for task_id in task_ids]
    assert all(slide["source_refs"] == expected_refs for slide in slide_spec["slides"])
    assert all(rendered_html.count(task_id) == 8 for task_id in task_ids)


def test_local_presentation_revision_applies_approved_billing_correction_source() -> None:
    stub = _load_stub()
    request_id = "3685a7db-24a0-4d83-bebb-a9f058fc115d"
    original_task_ids = (
        "73649f8b-beca-4678-9084-b4ad9b6653ca",
        "114ad3c0-da6d-47c0-a868-06fbf929978a",
    )
    correction_task_id = "5a5d3f98-95d9-4a22-b190-40a450f182fa"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "spec": {"page_count": 8},
        "revision_instruction": (
            "按已批准的 Credits 计费纠错任务修订伙伴 D 与账本页面；"
            f"保留来源 Task {original_task_ids[0]}、Task {original_task_ids[1]}，"
            f"并增加纠错依据 Task {correction_task_id}"
        ),
        "revision_targets": ["slide-04", "slide-06"],
    }

    _outline_raw, slide_spec_raw, rendered_html = stub._local_presentation_files(
        request_id,
        contract,
    )
    slide_spec = json.loads(slide_spec_raw)

    assert "每次 Runtime 仅 1 笔扣减" not in rendered_html
    assert "一个 Run 可含多次调用" in rendered_html
    assert "同一 reservation 重放不得重复扣费" in rendered_html
    assert "4 个 llm_round reservation，全部 finalized" in rendered_html
    assert "4 个 reservation 各有 1 条 consume；终态 reserved=0" in rendered_html
    assert "987 Credits" not in rendered_html
    assert "13 笔" not in rendered_html
    assert ".slide[data-layout='ledger'] .visual{grid-template-columns:repeat(3,1fr)" in rendered_html
    assert len(rendered_html) <= 6000
    assert correction_task_id in rendered_html
    assert all(
        f"Task {correction_task_id}" in slide["source_refs"]
        for slide in slide_spec["slides"]
    )
    assert all(rendered_html.count(task_id) == 8 for task_id in original_task_ids)


def test_local_presentation_revision_does_not_promote_run_id_to_task_source() -> None:
    stub = _load_stub()
    request_id = "3685a7db-24a0-4d83-bebb-a9f058fc115d"
    original_task_ids = (
        "73649f8b-beca-4678-9084-b4ad9b6653ca",
        "114ad3c0-da6d-47c0-a868-06fbf929978a",
    )
    run_id = "6b8a36d8-fb97-42ad-939f-d865b80fbd5b"
    correction_task_id = "3c75c913-0150-45de-8fcd-b0b5872d1ce5"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "spec": {"page_count": 8},
        "revision_instruction": (
            f"Run {run_id} 已完成账本核对；"
            f"保留来源 Task {original_task_ids[0]}、Task {original_task_ids[1]}，"
            f"并增加纠错依据 Task {correction_task_id}"
        ),
        "revision_targets": ["slide-04", "slide-06"],
    }

    _outline_raw, slide_spec_raw, rendered_html = stub._local_presentation_files(
        request_id,
        contract,
    )
    slide_spec = json.loads(slide_spec_raw)

    expected_refs = [
        f"Task {original_task_ids[0]}",
        f"Task {original_task_ids[1]}",
        f"Task {correction_task_id}",
    ]
    assert all(slide["source_refs"] == expected_refs for slide in slide_spec["slides"])
    assert f"Task {run_id}" not in rendered_html
    assert f"Task {correction_task_id}" in rendered_html


def test_local_business_answer_corrects_credits_acceptance_boundary() -> None:
    stub = _load_stub()

    answer = stub._normal_business_answer(
        "Credits 计费验收口径纠正：请明确 Run、invocation、reservation 和账本关系"
    )

    assert "一个 Agent Run 可以包含多次 billable LLM invocation" in answer
    assert "最多产生 1 条 reason=consume" in answer
    assert "同一 reservation 重放不得重复扣费" in answer
    assert "reserved 应为 0" in answer
    assert "每个 Runtime 尝试只扣 1 笔" in answer
    cjk_count = sum("\u3400" <= character <= "\u9fff" for character in answer)
    assert 300 <= cjk_count <= 900


def test_local_qa_stub_exercises_product_navigation_repair_without_faking_success() -> None:
    stub = _load_stub()

    invalid = stub._normal_business_answer("LOCAL_QA_PRODUCT_IA_GROUNDING")
    repaired = stub._normal_business_answer(
        "LOCAL_QA_PRODUCT_IA_GROUNDING\n"
        "The result cites an Astra breadcrumb that does not exist in the confirmed product catalog."
    )

    assert "工作台 → 报告中心" in invalid
    assert "`/reports`" in invalid
    assert "公司管理 → 企业知识与集成 → 组织同步" in repaired
    assert "`/company-admin/integrations/org`" in repaired
    assert "不能声称已经完成组织同步" in repaired


def test_local_qa_stub_emits_two_bounded_continuations_then_stops() -> None:
    stub = _load_stub()
    payload: dict[str, Any] = {
        "model": "astra-local-qa",
        "messages": [
            {
                "role": "user",
                "content": "LOCAL_QA_BOUNDED_CONTINUATION：生成三段正式验收报告。",
            }
        ],
    }

    kind, first = stub._completion(payload)
    assert kind == "business_answer"
    assert first["choices"][0]["finish_reason"] == "length"
    first_content = first["choices"][0]["message"]["content"]
    assert "【片段一】" in first_content
    payload["messages"].extend(
        [
            {"role": "assistant", "content": first_content},
            {"role": "user", "content": "Continue exactly from the previous response."},
        ]
    )

    _, second = stub._completion(payload)
    assert second["choices"][0]["finish_reason"] == "length"
    second_content = second["choices"][0]["message"]["content"]
    assert "【片段二】" in second_content
    payload["messages"].extend(
        [
            {"role": "assistant", "content": second_content},
            {"role": "user", "content": "Continue exactly from the previous response."},
        ]
    )

    _, final = stub._completion(payload)
    assert final["choices"][0]["finish_reason"] == "stop"
    assert "【片段三】" in final["choices"][0]["message"]["content"]


def test_local_business_correction_reads_the_rejected_deck_as_evidence() -> None:
    stub = _load_stub()
    deliverable_id = "1eb3bbb5-7b3f-475e-b949-7c78231978b9"
    payload: dict[str, Any] = {
        "tools": [
            {"function": {"name": "list_files"}},
            {"function": {"name": "read_file"}},
            {"function": {"name": "read_document"}},
        ],
        "messages": [
            {
                "role": "system",
                "content": (
                    "Credits 计费验收口径纠正；必须使用真实工具取得可验证证据回执"
                ),
            }
        ],
    }

    response = stub._choose_response(payload)
    assert response[0] == "billing_correction_list_evidence"
    assert json.loads(response[2][0]["function"]["arguments"]) == {
        "path": "workspace/deliverables"
    }
    _append_tool_result(
        payload,
        response,
        content=json.dumps({"entries": [{"name": deliverable_id}]}),
    )

    response = stub._choose_response(payload)
    assert response[0] == "billing_correction_read_evidence"
    assert json.loads(response[2][0]["function"]["arguments"]) == {
        "path": f"workspace/deliverables/{deliverable_id}/slide_spec.json"
    }
    _append_tool_result(
        payload,
        response,
        content='{"body_points":["每次 Runtime 仅 1 笔扣减"]}',
    )

    response = stub._choose_response(payload)
    assert response[0] == "billing_correction_extract_evidence"
    assert json.loads(response[2][0]["function"]["arguments"]) == {
        "path": f"workspace/deliverables/{deliverable_id}/result.pptx",
        "max_chars": 8000,
    }
    _append_tool_result(payload, response, content="当前 PPT 可读文本")

    response = stub._choose_response(payload)
    assert response[0] == "business_answer"
    assert response[2] == []
    assert "一个 Agent Run 可以包含多次 billable LLM invocation" in response[1]


def test_local_presentation_qa_retries_once_and_never_claims_failed_conversion() -> None:
    stub = _load_stub()
    request_id = "3685a7db-24a0-4d83-bebb-a9f058fc115d"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v1",
        "spec": {"page_count": 8},
    }
    payload: dict[str, Any] = {
        "tools": [
            {"function": {"name": name}}
            for name in ("list_files", "write_file", "convert_html_to_pptx")
        ],
        "messages": [
            {"role": "system", "content": f"DELIVERABLE_REQUEST={json.dumps(contract)}"}
        ],
    }

    for _ in range(4):
        response = stub._choose_response(payload)
        _append_tool_result(payload, response)

    first_conversion = stub._choose_response(payload)
    assert first_conversion[0] == "deliverable_convert"
    _append_tool_result(
        payload,
        first_conversion,
        content="convert_html_to_pptx did not produce a valid PPTX artifact. ❌ contract rejected",
    )

    retry = stub._choose_response(payload)
    assert retry[0] == "deliverable_convert"
    retry_arguments = json.loads(retry[2][0]["function"]["arguments"])
    assert retry_arguments["render_mode"] == "hybrid_editable"
    _append_tool_result(payload, retry, content="❌ deterministic render gate still failed")

    terminal = stub._choose_response(payload)
    assert terminal[0] == "deliverable_conversion_failed"
    assert "不能声称正式交付成功" in terminal[1]
    assert "已由内置转换工具生成" not in terminal[1]


def test_local_presentation_qa_isolates_multiple_requests_in_one_session() -> None:
    stub = _load_stub()
    old_request_id = "3685a7db-24a0-4d83-bebb-a9f058fc115d"
    new_request_id = "4c4a5470-383c-4e3b-81ef-72c30a3db0a1"
    old_call = stub._tool_call(
        "convert_html_to_pptx",
        {
            "source_path": f"workspace/deliverables/{old_request_id}/presentation.html",
            "target_path": f"workspace/deliverables/{old_request_id}/result.pptx",
        },
    )
    payload: dict[str, Any] = {
        "tools": [
            {"function": {"name": name}}
            for name in ("list_files", "write_file", "convert_html_to_pptx")
        ],
        "messages": [
            {
                "role": "system",
                "content": "DELIVERABLE_REQUEST="
                + json.dumps(
                    {
                        "request_id": new_request_id,
                        "work_type": "presentation",
                        "workflow_id": "builtin.presentation.v1",
                        "spec": {"page_count": 8},
                    }
                ),
            },
            {"role": "assistant", "content": None, "tool_calls": [old_call]},
            {
                "role": "tool",
                "tool_call_id": old_call["id"],
                "content": "convert_html_to_pptx did not produce a valid PPTX artifact. ❌",
            },
        ],
    }

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_inspect"
    arguments = json.loads(response[2][0]["function"]["arguments"])
    assert arguments["path"].endswith(new_request_id)


def test_local_presentation_v2_stops_at_outline_then_renders_after_approval_prompt() -> None:
    stub = _load_stub()
    request_id = "a9d5b849-56a3-4f58-a96e-ccdf4b1006ef"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v2",
        "spec": {
            "audience": "公司所有者、产品与市场负责人",
            "page_count": 8,
            "key_points": r"5 家设计伙伴完成可追溯任务验证。\n所有外部动作必须人工批准。",
            "editability_contract": "editable",
        },
    }
    visual_policy = {
        "version": "adaptive-v1",
        "minimum_distinct_layouts": 4,
        "minimum_distinct_images": 0,
        "minimum_image_slides": 0,
        "minimum_picture_coverage_ratio": 0,
        "maximum_uses_per_image": 0,
        "minimum_editable_compositions": 8,
        "minimum_body_font_size_px": 16,
        "minimum_metadata_font_size_px": 10,
        "minimum_mean_text_chars_per_slide": 120,
        "maximum_text_chars_per_slide": 900,
        "maximum_shapes_per_slide": 40,
        "minimum_contrast_ratio": 4.5,
        "data_slide_editability": "editable_required",
        "image_slide_fact_policy": "no_fact_assertions",
    }
    source_inventory = {
        "schema_version": "source-inventory-v1",
        "entries": [
            {
                "source_id": "src-01",
                "kind": "brief",
                "extracted_facts": [
                    "5 家设计伙伴完成可追溯任务验证。",
                    "所有外部动作必须人工批准。",
                ],
            }
        ],
    }
    payload: dict[str, Any] = {
        "model": "astra-local-qa",
        "tools": [
            {"function": {"name": name}}
            for name in ("read_file", "write_file", "convert_html_to_pptx")
        ],
        "messages": [
            {
                "role": "system",
                "content": (
                    "In this run you only plan the deck. "
                    f"SOURCE_INVENTORY={json.dumps(source_inventory)} "
                    f"PRESENTATION_VISUAL_POLICY={json.dumps(visual_policy)} "
                    f"DELIVERABLE_REQUEST={json.dumps(contract)}"
                ),
            }
        ],
    }

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_v2_outline_write"
    outline_args = json.loads(response[2][0]["function"]["arguments"])
    outline = json.loads(outline_args["content"])
    assert [slide["slide_id"] for slide in outline["slides"]] == [
        f"slide-{index:02d}" for index in range(1, 9)
    ]
    _append_tool_result(payload, response)

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_v2_outline_write"
    spec_args = json.loads(response[2][0]["function"]["arguments"])
    slide_spec = json.loads(spec_args["content"])
    assert slide_spec["visual_policy"] == visual_policy
    assert all(slide["source_refs"] == ["src-01"] for slide in slide_spec["slides"])
    assert all(slide["asset_ref"] == "" for slide in slide_spec["slides"])
    assert slide_spec["slides"][0]["body_points"][1] == "5 家设计伙伴完成可追溯任务验证。"
    _append_tool_result(payload, response)

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_v2_outline_complete"
    assert response[2] == []
    assert "等待人工批准" in response[1]

    payload["messages"].append(
        {
            "role": "system",
            "content": (
                "You are producing an approved deck. "
                f"PRESENTATION_VISUAL_POLICY={json.dumps(visual_policy)} "
                f"DELIVERABLE_REQUEST={json.dumps(contract)}"
            ),
        }
    )
    for expected_path in ("outline.json", "slide_spec.json"):
        response = stub._choose_response(payload)
        assert response[0] == "deliverable_v2_plan_read"
        read_args = json.loads(response[2][0]["function"]["arguments"])
        assert read_args["path"].endswith(expected_path)
        _append_tool_result(payload, response, content="{}")

    html_chunks: list[str] = []
    while True:
        response = stub._choose_response(payload)
        if response[0] != "deliverable_v2_render_write":
            break
        html_args = json.loads(response[2][0]["function"]["arguments"])
        assert html_args["path"].endswith("presentation.html")
        assert 0 < len(html_args["content"]) <= 5000
        assert html_args["mode"] == ("overwrite" if not html_chunks else "append")
        html_chunks.append(html_args["content"])
        _append_tool_result(payload, response)

    assert len(html_chunks) >= 2
    rendered_html = "".join(html_chunks)
    assert "data-slide='slide-08'" in rendered_html
    assert r"\n" not in rendered_html
    assert "5家设计伙伴完成可追溯任务验证。" in rendered_html
    assert "所有外部动作必须人工批准。" in rendered_html
    assert "line-break:strict" in rendered_html
    assert ".card p{font-size:19px" in rendered_html
    assert "&#8288;" not in rendered_html

    assert response[0] == "deliverable_v2_convert"
    conversion_args = json.loads(response[2][0]["function"]["arguments"])
    assert conversion_args["render_mode"] == "hybrid_editable"
    assert conversion_args["expected_page_count"] == 8
    _append_tool_result(payload, response, content="✅ converted")

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_v2_complete"
    assert response[2] == []
    assert response[1].endswith("请以产物校验和人工批准状态为准。")


def test_local_presentation_v2_recovers_after_oversized_write_and_missing_source() -> None:
    stub = _load_stub()
    request_id = "a9d5b849-56a3-4f58-a96e-ccdf4b1006ef"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v2",
        "spec": {"page_count": 8, "editability_contract": "editable"},
    }
    payload: dict[str, Any] = {
        "tools": [
            {"function": {"name": name}}
            for name in ("read_file", "write_file", "convert_html_to_pptx")
        ],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are producing an approved deck. "
                    f"DELIVERABLE_REQUEST={json.dumps(contract)}"
                ),
            }
        ],
    }

    for expected_path in ("outline.json", "slide_spec.json"):
        response = stub._choose_response(payload)
        assert response[0] == "deliverable_v2_plan_read"
        arguments = json.loads(response[2][0]["function"]["arguments"])
        assert arguments["path"].endswith(expected_path)
        _append_tool_result(payload, response, content="{}")

    failed_write = stub._choose_response(payload)
    assert failed_write[0] == "deliverable_v2_render_write"
    _append_tool_result(
        payload,
        failed_write,
        content="write_file content exceeds the 6000 character limit",
    )
    recovery_write = stub._choose_response(payload)
    assert recovery_write[0] == "deliverable_v2_render_write"
    recovery_args = json.loads(recovery_write[2][0]["function"]["arguments"])
    assert recovery_args["mode"] == "overwrite"
    assert len(recovery_args["content"]) <= 5000

    while recovery_write[0] == "deliverable_v2_render_write":
        _append_tool_result(payload, recovery_write)
        recovery_write = stub._choose_response(payload)

    assert recovery_write[0] == "deliverable_v2_convert"
    _append_tool_result(
        payload,
        recovery_write,
        content="conversion_source_not_found: source file not found",
    )

    retry = stub._choose_response(payload)
    assert retry[0] == "deliverable_v2_convert"
    _append_tool_result(
        payload,
        retry,
        content=(
            "convert_html_to_pptx did not produce a valid PPTX artifact: "
            "presentation rendered visual quality failed"
        ),
    )
    final_retry = stub._choose_response(payload)
    assert final_retry[0] == "deliverable_v2_convert"
    _append_tool_result(payload, final_retry, content="✅ converted")

    # A later artifact gate can fail even though conversion itself succeeded.
    # The local browser fixture accepts one explicit, bounded revalidation run
    # so the same durable request can prove recovery after a gate correction.
    payload["messages"].append(
        {"role": "user", "content": "请重新运行 V2 演示文稿转换并重新校验。"}
    )
    gate_revalidation = stub._choose_response(payload)
    assert gate_revalidation[0] == "deliverable_v2_convert"
    _append_tool_result(payload, gate_revalidation, content="✅ converted")
    complete = stub._choose_response(payload)
    assert complete[0] == "deliverable_v2_complete"


def test_local_presentation_v2_revision_ignores_prior_request_tool_history() -> None:
    stub = _load_stub()
    request_id = "a9d5b849-56a3-4f58-a96e-ccdf4b1006ef"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v2",
        "spec": {"page_count": 8, "editability_contract": "editable"},
    }
    base = f"workspace/deliverables/{request_id}"
    prior_calls = [
        stub._tool_call("read_file", {"path": f"{base}/outline.json"}),
        stub._tool_call("read_file", {"path": f"{base}/slide_spec.json"}),
        stub._tool_call(
            "write_file",
            {"path": f"{base}/presentation.html", "content": "old", "mode": "overwrite"},
        ),
        stub._tool_call(
            "convert_html_to_pptx",
            {
                "source_path": f"{base}/presentation.html",
                "target_path": f"{base}/result.pptx",
            },
        ),
    ]
    payload: dict[str, Any] = {
        "tools": [
            {"function": {"name": name}}
            for name in ("read_file", "write_file", "convert_html_to_pptx")
        ],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are producing an approved deck. "
                    f"DELIVERABLE_REQUEST={json.dumps(contract)}"
                ),
            },
            {"role": "assistant", "content": None, "tool_calls": prior_calls},
            *[
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "✅ prior stage succeeded",
                }
                for call in prior_calls
            ],
            {
                "role": "system",
                "content": (
                    "You are producing an approved deck. "
                    f"DELIVERABLE_REQUEST={json.dumps(contract)}"
                ),
            },
        ],
    }

    response = stub._choose_response(payload)

    assert response[0] == "deliverable_v2_plan_read"
    arguments = json.loads(response[2][0]["function"]["arguments"])
    assert arguments["path"] == f"{base}/outline.json"


def test_local_presentation_v2_recognizes_the_real_page_revision_prompt() -> None:
    stub = _load_stub()
    request_id = "a9d5b849-56a3-4f58-a96e-ccdf4b1006ef"
    contract = {
        "request_id": request_id,
        "work_type": "presentation",
        "workflow_id": "builtin.presentation.v2",
        "spec": {"page_count": 8, "editability_contract": "editable"},
    }
    base = f"workspace/deliverables/{request_id}"
    payload: dict[str, Any] = {
        "tools": [
            {"function": {"name": name}}
            for name in ("read_file", "write_file", "convert_html_to_pptx")
        ],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are executing a page-targeted revision of an approved deck "
                    "for a persisted Astra Deliverable Request. "
                    f"DELIVERABLE_REQUEST={json.dumps(contract)}"
                ),
            }
        ],
    }

    for expected_path in ("outline.json", "slide_spec.json", "presentation.html"):
        response = stub._choose_response(payload)
        assert response[0] == "deliverable_v2_plan_read"
        arguments = json.loads(response[2][0]["function"]["arguments"])
        assert arguments["path"] == f"{base}/{expected_path}"
        _append_tool_result(payload, response, content="{}")

    response = stub._choose_response(payload)
    assert response[0] == "deliverable_v2_render_write"
    arguments = json.loads(response[2][0]["function"]["arguments"])
    assert arguments["path"] == f"{base}/presentation.html"


def test_tool_result_failed_recognizes_runtime_failure_summaries() -> None:
    stub = _load_stub()

    assert stub._tool_result_failed("write_file content exceeds the 6000 character limit")
    assert stub._tool_result_failed("conversion_source_not_found: source file not found")
    assert not stub._tool_result_failed("✅ converted")


def test_presentation_visible_copy_is_safe_and_layout_stable() -> None:
    stub = _load_stub()

    rendered = stub._presentation_html_text(
        "5 家设计伙伴；request_id 67f47461-144b-4118-8ccf-c19321b0a0ca 已复核。"
    )

    assert "5家设计伙伴；" in rendered
    assert "67f47461…b0a0ca" in rendered
    assert "67f47461-144b-4118-8ccf-c19321b0a0ca" not in rendered
    assert "复核。" in rendered
    assert "&#8288;" not in rendered
