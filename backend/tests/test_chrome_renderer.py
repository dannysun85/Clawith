from pathlib import Path

import pytest

from app.services.document_conversion.chrome_renderer import (
    build_hybrid_text_capture_css,
    chrome_executable,
    collect_browser_layout,
)


def test_hybrid_text_capture_css_hides_styled_descendants() -> None:
    css = build_hybrid_text_capture_css(
        ["text-1"],
        text_clip_item_ids=["gradient-text"],
    )

    assert '[data-clawith-item-id="text-1"],' in css
    assert '[data-clawith-item-id="text-1"] *' in css
    assert '[data-clawith-item-id="text-1"] *::before' in css
    assert "-webkit-text-fill-color: transparent !important" in css
    assert '[data-clawith-item-id="gradient-text"]' in css
    assert "background-image: none !important" in css


@pytest.mark.asyncio
@pytest.mark.skipif(chrome_executable() is None, reason="Chrome is not installed")
async def test_browser_layout_splits_painted_inline_labels(tmp_path: Path) -> None:
    source = tmp_path / "inline-labels.html"
    source.write_text(
        """
        <!doctype html>
        <html><body>
          <section class="slide" style="position:relative;width:1280px;height:720px;background:#071a38">
            <div data-visual>
              <div style="position:absolute;left:100px;top:100px;display:flex;gap:12px">
                <span style="padding:6px 14px;border:1px solid #22d3ee;background:rgba(34,211,238,.12)">数字员工</span>
                <span style="padding:6px 14px;border:1px solid #8b5cf6;background:rgba(139,92,246,.12)">协同闭环</span>
              </div>
              <div style="position:absolute;left:100px;top:200px;font-size:24px">
                <span style="background:linear-gradient(90deg,#22d3ee,#8b5cf6);background-clip:text;color:transparent">ReefTotem OPC</span>
                · 数字员工协同平台
              </div>
              <div style="position:absolute;left:100px;top:300px;display:flex;gap:10px;font-size:16px">
                <span style="width:10px;height:10px;border-radius:50%;background:#22d3ee"></span>
                深圳前海瑞孚图腾科技有限公司
              </div>
            </div>
          </section>
        </body></html>
        """,
        encoding="utf-8",
    )

    layout = await collect_browser_layout(
        source,
        design_w_px=1280,
        design_h_px=720,
        render_mode="hybrid_editable",
        render_scale=1,
    )
    assert layout is not None
    try:
        items = layout["slides"][0]["items"]
        assert layout["slides"][0]["preferWholeSlideVisualCapture"] is True
        texts = [item["text"] for item in items if item["kind"] == "text"]
        assert "数字员工" in texts
        assert "协同闭环" in texts
        assert "数字员工 协同闭环" not in texts
        assert "ReefTotem OPC" in texts
        assert "· 数字员工协同平台" in texts
        company = next(
            item
            for item in items
            if item["kind"] == "text"
            and item["text"] == "深圳前海瑞孚图腾科技有限公司"
        )
        assert company["x"] > 110
    finally:
        for key in (
            "screenshots",
            "backgroundScreenshots",
            "contentScreenshots",
        ):
            for value in layout.get(key) or []:
                if value:
                    Path(value).unlink(missing_ok=True)
        for value in (layout.get("shapeScreenshots") or {}).values():
            Path(value).unlink(missing_ok=True)
