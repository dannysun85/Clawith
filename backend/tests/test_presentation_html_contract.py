from pathlib import Path
import json

import pytest

from app.services.document_conversion import html_to_pdf, pptx_renderer
from app.services.document_conversion.presentation_contract import (
    PresentationVisualQualityError,
    validate_browser_slide_text_bounds,
    validate_browser_slide_visual_quality,
    validate_presentation_html_contract,
    validate_presentation_visible_text,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_browser_layout_rejects_text_clipped_by_slide_canvas():
    layout = {
        "slides": [
            {
                "width": 1280,
                "height": 720,
                "items": [
                    {
                        "kind": "text",
                        "text": "品牌视觉关键词",
                        "x": 72,
                        "y": 704,
                        "w": 200,
                        "h": 30,
                    }
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="slide 1 text exceeds canvas"):
        validate_browser_slide_text_bounds(layout)

    layout["slides"][0]["items"][0]["y"] = 680
    validate_browser_slide_text_bounds(layout)


def test_browser_visual_quality_returns_structured_known_defect_receipt():
    layout = {
        "slides": [
            {
                "width": 1280,
                "height": 720,
                "items": [
                    {
                        "kind": "text",
                        "tag": "h1",
                        "text": "面向品牌方市场负责人的商业方案",
                        "x": 80,
                        "y": 60,
                        "w": 520,
                        "h": 100,
                        "clientWidth": 520,
                        "clientHeight": 100,
                        "scrollWidth": 520,
                        "scrollHeight": 100,
                        "lines": [
                            {"text": "面向品牌方市场负责人的商业方"},
                            {"text": "案"},
                        ],
                    },
                    {
                        "kind": "text",
                        "tag": "p",
                        "text": "重叠说明文字",
                        "x": 90,
                        "y": 100,
                        "w": 300,
                        "h": 60,
                        "clientWidth": 300,
                        "clientHeight": 60,
                        "scrollWidth": 300,
                        "scrollHeight": 90,
                        "lines": [{"text": "重叠说明文字"}],
                    },
                ],
            }
        ]
    }

    with pytest.raises(PresentationVisualQualityError) as error:
        validate_browser_slide_visual_quality(layout)

    receipt = error.value.receipt
    assert receipt["gate"] == "presentation_render_quality"
    assert receipt["status"] == "failed"
    assert receipt["scope_guard"] == {
        "may_expand_user_edit_scope": False,
        "action": "request_user_approval",
    }
    assert "request user approval" in str(error.value)
    assert {
        failure["code"] for failure in receipt["failures"]
    } >= {
        "title_orphan_line",
        "text_container_overflow",
        "text_overlap",
    }


def test_browser_visual_quality_accepts_measured_non_overlapping_layout():
    layout = {
        "slides": [
            {
                "width": 1280,
                "height": 720,
                "items": [
                    {
                        "kind": "text",
                        "tag": "h1",
                        "text": "商业演示方案",
                        "x": 80,
                        "y": 60,
                        "w": 520,
                        "h": 80,
                        "clientWidth": 520,
                        "clientHeight": 80,
                        "scrollWidth": 520,
                        "scrollHeight": 80,
                        "lines": [{"text": "商业演示方案"}],
                    },
                    {
                        "kind": "text",
                        "tag": "p",
                        "text": "交付标准与下一步计划",
                        "x": 80,
                        "y": 180,
                        "w": 520,
                        "h": 60,
                        "clientWidth": 520,
                        "clientHeight": 60,
                        "scrollWidth": 520,
                        "scrollHeight": 60,
                        "lines": [{"text": "交付标准与下一步计划"}],
                    },
                ],
            }
        ],
        "screenshots": ["/tmp/slide-1.png"],
    }

    receipt = validate_browser_slide_visual_quality(
        layout,
        screenshot_key="screenshots",
    )

    assert receipt["status"] == "passed"
    assert receipt["slide_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "converter_name"),
    [
        (html_to_pdf, "convert_html_to_pdf"),
        (pptx_renderer, "render_html_to_pptx"),
    ],
)
async def test_renderers_propagate_structured_visual_quality_failure(
    monkeypatch,
    tmp_path: Path,
    module,
    converter_name: str,
) -> None:
    source = _write(tmp_path / "presentation.html", "<html></html>")
    target = tmp_path / (
        "deck.pdf" if converter_name == "convert_html_to_pdf" else "deck.pptx"
    )
    expected = PresentationVisualQualityError(
        [
            {
                "code": "text_container_overflow",
                "slide": 8,
                "message": "slide 8 text container overflows",
            }
        ]
    )

    def fail_contract(*_args, **_kwargs):
        raise expected

    monkeypatch.setattr(module, "validate_presentation_html_contract", fail_contract)
    converter = getattr(module, converter_name)

    with pytest.raises(PresentationVisualQualityError) as error:
        if converter_name == "convert_html_to_pdf":
            await converter(source, target, "deck.pdf", {})
        else:
            await converter(source, target, "deck.pptx", None, {})

    assert error.value.code == "presentation_visual_quality_failed"
    assert error.value.receipt["failures"][0]["code"] == "text_container_overflow"


def test_presentation_html_contract_accepts_complete_source_and_local_image(
    tmp_path: Path,
) -> None:
    (tmp_path / "hero.jpg").write_bytes(b"image")
    source = _write(
        tmp_path / "slides.html",
        """
        <html><head><style>.slide{width:1280px;height:720px}</style></head>
        <body>
          <section class="slide" data-slide="1"><img src="hero.jpg"></section>
          <section class="slide" data-slide="2"></section>
        </body></html>
        """,
    )

    validate_presentation_html_contract(source, expected_page_count=2)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "<html><body><style>.slide{width:1280px",
            "unclosed <html> block",
        ),
        (
            '<html><body><section class="slide" data-slide="1"></section></body></html>',
            "expected 2 .slide[data-slide] nodes, found 1",
        ),
        (
            """
            <html><body>
              <section class="slide" data-slide="1"></section>
              <section class="slide" data-slide="1"></section>
            </body></html>
            """,
            "data-slide values must be unique",
        ),
        (
            """
            <html><body>
              <section class="slide" data-slide="1"><img src="missing.jpg"></section>
              <section class="slide" data-slide="2"></section>
            </body></html>
            """,
            "missing local image files: missing.jpg",
        ),
    ],
)
def test_presentation_html_contract_rejects_invalid_source(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    source = _write(tmp_path / "slides.html", body)

    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):
        validate_presentation_html_contract(source, expected_page_count=2)


def test_presentation_html_contract_rejects_missing_image_without_page_count(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "slides.html",
        '<html><body><img src="missing.jpg"></body></html>',
    )

    with pytest.raises(ValueError, match="missing local image files: missing.jpg"):
        validate_presentation_html_contract(source, expected_page_count=None)


@pytest.mark.parametrize(
    ("visible_copy", "message"),
    [
        ("[Your Brand Logo]", "unresolved visible placeholder"),
        ("品牌名称：待补充", "unresolved visible placeholder"),
        ("茶饮爱好者 ★", "unsupported visible rating glyphs"),
    ],
)
def test_presentation_html_contract_rejects_release_blocking_visible_copy(
    tmp_path: Path,
    visible_copy: str,
    message: str,
) -> None:
    source = _write(
        tmp_path / "slides.html",
        f"""
        <html><head><style>.slide{{width:1280px;height:720px}}</style></head>
        <body>
          <section class="slide" data-slide="1"><p>{visible_copy}</p></section>
        </body></html>
        """,
    )

    with pytest.raises(ValueError, match=message):
        validate_presentation_html_contract(source, expected_page_count=1)


@pytest.mark.parametrize("visible_copy", ["[Your Brand Logo]", "办公白领 ★"])
def test_presentation_visible_text_policy_rejects_release_blockers(
    visible_copy: str,
) -> None:
    with pytest.raises(ValueError, match="visible content policy invalid"):
        validate_presentation_visible_text(visible_copy)


def test_presentation_planning_contract_accepts_matching_outline_and_slide_spec(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "slides.html",
        """
        <html><head><style>.slide{width:1280px;height:720px}</style></head>
        <body>
          <section class="slide" data-slide="1" data-layout="hero_metric">
            <h1 data-slide-title>为什么是<span>现在</span></h1>
            <div data-visual><strong>关键数字</strong><span>决策窗口</span></div>
          </section>
          <section class="slide" data-slide="2" data-layout="three_step">
            <h2 data-slide-title>下一步行动</h2>
            <div data-visual><span>准备</span><span>试点</span><span>复盘</span></div>
          </section>
        </body></html>
        """,
    )
    outline = tmp_path / "outline.json"
    outline.write_text(
        json.dumps(
            {
                "deck_title": "商业方案",
                "audience": "客户决策人",
                "core_message": "现在进入试点",
                "slides": [
                    {
                        "slide_id": "1",
                        "purpose": "建立紧迫性",
                        "headline": "为什么是现在",
                        "evidence": "客户提供的数据",
                        "visual_intent": "关键数字",
                    },
                    {
                        "slide_id": "2",
                        "purpose": "推动决策",
                        "headline": "下一步行动",
                        "evidence": [],
                        "visual_intent": "三步路线图",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    slide_spec = tmp_path / "slide_spec.json"
    slide_spec.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "slide_id": "1",
                        "headline": "为什么是现在",
                        "layout": "hero_metric",
                        "body_points": ["市场窗口"],
                        "visual_asset": "关键数字",
                        "source_refs": [
                            {"label": "方案依据", "ref": "客户提供的数据"}
                        ],
                    },
                    {
                        "slide_id": "2",
                        "headline": "下一步行动",
                        "layout": "three_step",
                        "body_points": ["试点"],
                        "visual_asset": "三步路线图",
                        "source_refs": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    validate_presentation_html_contract(
        source,
        expected_page_count=2,
        outline_file=outline,
        slide_spec_file=slide_spec,
    )


def test_presentation_planning_contract_rejects_headline_drift(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "slides.html",
        """
        <html><body>
          <section class="slide" data-slide="1" data-layout="closing">
            <h1 data-slide-title>HTML 标题</h1>
            <div data-visual>行动图</div>
          </section>
        </body></html>
        """,
    )
    outline = tmp_path / "outline.json"
    outline.write_text(
        json.dumps(
            {
                "deck_title": "商业方案",
                "audience": "客户",
                "core_message": "行动",
                "slides": [
                    {
                        "slide_id": "1",
                        "purpose": "推动决策",
                        "headline": "规格标题",
                        "evidence": [],
                        "visual_intent": "行动图",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    slide_spec = tmp_path / "slide_spec.json"
    slide_spec.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "slide_id": "1",
                        "headline": "规格标题",
                        "layout": "closing",
                        "body_points": [],
                        "visual_asset": "行动图",
                        "source_refs": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="slide 1 HTML title differs from slide_spec headline",
    ):
        validate_presentation_html_contract(
            source,
            expected_page_count=1,
            outline_file=outline,
            slide_spec_file=slide_spec,
        )


def test_presentation_planning_contract_rejects_unimplemented_visual_intent(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "slides.html",
        """
        <html><body>
          <section class="slide" data-slide="1" data-layout="cover">
            <h1 data-slide-title>商业方案</h1>
          </section>
        </body></html>
        """,
    )
    outline = tmp_path / "outline.json"
    outline.write_text(
        json.dumps(
            {
                "deck_title": "商业方案",
                "audience": "客户",
                "core_message": "开始试点",
                "slides": [
                    {
                        "slide_id": "1",
                        "purpose": "建立共识",
                        "headline": "商业方案",
                        "evidence": [],
                        "visual_intent": "品牌化封面构图",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    slide_spec = tmp_path / "slide_spec.json"
    slide_spec.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "slide_id": "1",
                        "headline": "商业方案",
                        "layout": "cover",
                        "body_points": [],
                        "visual_asset": "品牌化封面构图",
                        "source_refs": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"slide 1 must implement its visual intent with \[data-visual\]",
    ):
        validate_presentation_html_contract(
            source,
            expected_page_count=1,
            outline_file=outline,
            slide_spec_file=slide_spec,
        )


def test_presentation_planning_contract_rejects_descriptive_visual_placeholder(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "slides.html",
        """
        <html><body>
          <section class="slide" data-slide="1" data-layout="process">
            <h1 data-slide-title>交付流程</h1>
            <div data-visual>这里放一张流程图</div>
          </section>
        </body></html>
        """,
    )
    outline = tmp_path / "outline.json"
    outline.write_text(
        json.dumps(
            {
                "deck_title": "商业方案",
                "audience": "客户",
                "core_message": "稳健交付",
                "slides": [
                    {
                        "slide_id": "1",
                        "purpose": "说明流程",
                        "headline": "交付流程",
                        "evidence": ["用户确认的工作说明"],
                        "visual_intent": "交付流程图",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    slide_spec = tmp_path / "slide_spec.json"
    slide_spec.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "slide_id": "1",
                        "headline": "交付流程",
                        "layout": "process",
                        "body_points": ["准备", "执行", "复核"],
                        "visual_asset": "交付流程图",
                        "source_refs": ["用户确认的工作说明"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"\[data-visual\] must contain a real media asset or a multi-element editable composition",
    ):
        validate_presentation_html_contract(
            source,
            expected_page_count=1,
            outline_file=outline,
            slide_spec_file=slide_spec,
        )


def _write_adaptive_visual_plan_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "hero-a.png").write_bytes(b"image-a")
    (assets / "hero-b.png").write_bytes(b"image-b")
    (assets / "hero-c.png").write_bytes(b"image-c")

    source = _write(
        tmp_path / "slides.html",
        """
        <html><head><style>.slide{width:1280px;height:720px}</style></head><body>
          <section class="slide" data-slide="1" data-layout="cover">
            <h1 data-slide-title>封面</h1>
            <div data-visual><img src="assets/hero-a.png"></div>
          </section>
          <section class="slide" data-slide="2" data-layout="split_scene">
            <h1 data-slide-title>使用场景</h1>
            <div data-visual><img src="assets/hero-b.png"></div>
          </section>
          <section class="slide" data-slide="3" data-layout="process">
            <h1 data-slide-title>人物故事</h1>
            <div data-visual><img src="assets/hero-c.png"></div>
          </section>
          <section class="slide" data-slide="4" data-layout="decision_matrix">
            <h1 data-slide-title>核心判断</h1>
            <div data-visual><strong>机会</strong><span>风险</span></div>
          </section>
          <section class="slide" data-slide="5" data-layout="cover">
            <h1 data-slide-title>验证矩阵</h1>
            <div data-visual><span>维度 A</span><span>维度 B</span></div>
          </section>
          <section class="slide" data-slide="6" data-layout="split_scene">
            <h1 data-slide-title>执行路径</h1>
            <div data-visual><span>准备</span><span>执行</span></div>
          </section>
          <section class="slide" data-slide="7" data-layout="process">
            <h1 data-slide-title>试点计划</h1>
            <div data-visual><span>范围</span><span>验收</span></div>
          </section>
          <section class="slide" data-slide="8" data-layout="decision_matrix">
            <h1 data-slide-title>下一步</h1>
            <div data-visual><span>试点</span><span>复盘</span></div>
          </section>
        </body></html>
        """,
    )
    headlines = [
        "封面",
        "使用场景",
        "人物故事",
        "核心判断",
        "验证矩阵",
        "执行路径",
        "试点计划",
        "下一步",
    ]
    layouts = [
        "cover",
        "split_scene",
        "process",
        "decision_matrix",
        "cover",
        "split_scene",
        "process",
        "decision_matrix",
    ]
    outline = _write(
        tmp_path / "outline.json",
        json.dumps(
            {
                "deck_title": "动态商业方案",
                "audience": "业务决策者",
                "core_message": "以可验证路径推进",
                "slides": [
                    {
                        "slide_id": str(index),
                        "purpose": f"完成第 {index} 页目标",
                        "headline": headline,
                        "evidence": [],
                        "visual_intent": f"第 {index} 页视觉",
                    }
                    for index, headline in enumerate(headlines, start=1)
                ],
            },
            ensure_ascii=False,
        ),
    )
    visual_kinds = [
        "generated_image",
        "supplied_image",
        "generated_image",
        "editable_typography",
        "editable_table",
        "editable_diagram",
        "editable_chart",
        "editable_diagram",
    ]
    asset_refs = [
        "assets/hero-a.png",
        "assets/hero-b.png",
        "assets/hero-c.png",
        "",
        "",
        "",
        "",
        "",
    ]
    slide_spec = _write(
        tmp_path / "slide_spec.json",
        json.dumps(
            {
                "visual_plan_version": "adaptive-v1",
                "visual_policy": {
                    "minimum_distinct_layouts": 4,
                    "minimum_distinct_images": 3,
                    "maximum_uses_per_image": 3,
                    "minimum_editable_compositions": 2,
                },
                "slides": [
                    {
                        "slide_id": str(index),
                        "headline": headline,
                        "slide_type": f"purpose_{index}",
                        "layout": layouts[index - 1],
                        "body_points": [],
                        "visual_kind": visual_kinds[index - 1],
                        "visual_asset": f"第 {index} 页视觉",
                        "asset_ref": asset_refs[index - 1],
                        "source_refs": [],
                    }
                    for index, headline in enumerate(headlines, start=1)
                ],
            },
            ensure_ascii=False,
        ),
    )
    return source, outline, slide_spec


def test_adaptive_visual_plan_accepts_varied_layouts_assets_and_editable_compositions(
    tmp_path: Path,
) -> None:
    source, outline, slide_spec = _write_adaptive_visual_plan_fixture(tmp_path)

    validate_presentation_html_contract(
        source,
        expected_page_count=8,
        outline_file=outline,
        slide_spec_file=slide_spec,
    )


def test_adaptive_visual_plan_rejects_image_reuse_that_collapses_asset_variety(
    tmp_path: Path,
) -> None:
    source, outline, slide_spec = _write_adaptive_visual_plan_fixture(tmp_path)
    spec = json.loads(slide_spec.read_text(encoding="utf-8"))
    spec["slides"][1]["asset_ref"] = "assets/hero-a.png"
    slide_spec.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="slide_spec uses 2 distinct image assets; visual_policy requires 3",
    ):
        validate_presentation_html_contract(
            source,
            expected_page_count=8,
            outline_file=outline,
            slide_spec_file=slide_spec,
        )


def test_adaptive_visual_plan_rejects_consecutive_duplicate_layouts(
    tmp_path: Path,
) -> None:
    source, outline, slide_spec = _write_adaptive_visual_plan_fixture(tmp_path)
    spec = json.loads(slide_spec.read_text(encoding="utf-8"))
    spec["slides"][1]["layout"] = "cover"
    slide_spec.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"slide_spec\.slides\[2\]\.layout must not repeat the previous slide layout",
    ):
        validate_presentation_html_contract(
            source,
            expected_page_count=8,
            outline_file=outline,
            slide_spec_file=slide_spec,
        )
