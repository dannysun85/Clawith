import fitz

from app.services.agent_tools import _read_document_sync, _read_pdf_fast_sync


def _write_pdf(path, page_texts):
    document = fitz.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_read_document_can_reach_later_pdf_pages(tmp_path):
    pdf_path = tmp_path / "long.pdf"
    _write_pdf(pdf_path, ["FIRST-PAGE", "SECOND-PAGE", "THIRD-PAGE"])

    result = _read_document_sync(
        tmp_path,
        "long.pdf",
        page_start=3,
        max_pages=1,
        max_chars=2000,
    )

    assert result.ok is True
    assert "--- Page 3 ---" in result.content
    assert "THIRD-PAGE" in result.content
    assert "FIRST-PAGE" not in result.content


def test_pdf_fallback_respects_page_window(tmp_path):
    pdf_path = tmp_path / "long.pdf"
    _write_pdf(pdf_path, ["FIRST-PAGE", "SECOND-PAGE", "THIRD-PAGE"])

    result = _read_pdf_fast_sync(
        tmp_path,
        "long.pdf",
        page_start=2,
        max_pages=1,
        max_chars=2000,
    )

    assert result.ok is True
    assert "--- Page 2 ---" in result.content
    assert "SECOND-PAGE" in result.content
    assert "FIRST-PAGE" not in result.content
    assert "THIRD-PAGE" not in result.content
