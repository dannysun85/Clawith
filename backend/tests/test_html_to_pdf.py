import pytest
from unittest.mock import MagicMock, patch
import fitz

from app.services.document_conversion.html_to_pdf import (
    _CDP_MAX_MESSAGE_BYTES,
    _paged_pdf_geometry,
    _validate_pdf_page_count,
    _write_slide_screenshot_pdf,
    convert_html_to_pdf,
)


def test_pdf_page_count_validation_uses_physical_pages(tmp_path):
    pdf_path = tmp_path / "deck.pdf"
    document = fitz.open()
    document.new_page()
    document.new_page()
    document.save(pdf_path)
    document.close()

    _validate_pdf_page_count(pdf_path, 2)
    with pytest.raises(
        ValueError,
        match=r"expected 3, found 2",
    ):
        _validate_pdf_page_count(pdf_path, 3)


def test_cdp_message_limit_supports_multi_page_image_led_decks():
    assert _CDP_MAX_MESSAGE_BYTES >= 200_000_000


def test_paged_pdf_geometry_defaults_to_authored_slide_size():
    geometry = _paged_pdf_geometry(
        {},
        design_width_px=1280,
        design_height_px=720,
    )

    assert geometry == {
        "preferCSSPageSize": True,
        "paperWidth": pytest.approx(13.3333333333),
        "paperHeight": 7.5,
        "scale": 1.0,
    }


def test_slide_screenshot_pdf_preserves_page_count_and_geometry(tmp_path):
    screenshot = tmp_path / "slide.png"
    pixmap = fitz.Pixmap(fitz.csRGB, (0, 0, 2, 2), False)
    pixmap.clear_with(0x335577)
    pixmap.save(screenshot)
    target = tmp_path / "deck.pdf"

    _write_slide_screenshot_pdf(
        [screenshot, screenshot],
        target,
        design_width_px=1280,
        design_height_px=720,
    )

    with fitz.open(target) as document:
        assert document.page_count == 2
        assert document[0].rect.width == pytest.approx(960)
        assert document[0].rect.height == pytest.approx(540)


@pytest.mark.asyncio
@patch("app.services.document_conversion.html_to_pdf.chrome_executable")
@patch("subprocess.Popen")
@patch("time.time")
@patch("weasyprint.HTML")
async def test_convert_html_to_pdf_linux(
    mock_weasy_html,
    mock_time,
    mock_popen,
    mock_chrome_exec,
    tmp_path,
):
    mock_chrome_exec.return_value = "/usr/bin/google-chrome"
    mock_time.side_effect = [1000.0, 1010.0]  # Fails deadline immediately
    
    # Mock subprocess.Popen
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    
    # Mock weasyprint HTML write_pdf
    mock_weasy_instance = MagicMock()
    mock_weasy_html.return_value = mock_weasy_instance

    src = tmp_path / "src.html"
    src.write_text("<html><body>Test</body></html>", encoding="utf-8")
    tgt = tmp_path / "tgt.pdf"
    
    mock_socket = MagicMock()
    mock_socket.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 9222)
    with patch("sys.platform", "linux"), patch("socket.socket", return_value=mock_socket):
        res = await convert_html_to_pdf(src, tgt, "tgt.pdf", {})
        
    assert mock_popen.called
    args = mock_popen.call_args[0][0]
    assert "--no-sandbox" in args
    assert "--disable-setuid-sandbox" in args
    assert "WeasyPrint" in res


@pytest.mark.asyncio
@patch("app.services.document_conversion.html_to_pdf.chrome_executable")
@patch("subprocess.Popen")
@patch("time.time")
@patch("weasyprint.HTML")
async def test_convert_html_to_pdf_darwin(
    mock_weasy_html,
    mock_time,
    mock_popen,
    mock_chrome_exec,
    tmp_path,
):
    mock_chrome_exec.return_value = "/usr/bin/google-chrome"
    mock_time.side_effect = [1000.0, 1010.0]  # Fails deadline immediately
    
    # Mock subprocess.Popen
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    
    # Mock weasyprint HTML write_pdf
    mock_weasy_instance = MagicMock()
    mock_weasy_html.return_value = mock_weasy_instance

    src = tmp_path / "src.html"
    src.write_text("<html><body>Test</body></html>", encoding="utf-8")
    tgt = tmp_path / "tgt.pdf"
    
    mock_socket = MagicMock()
    mock_socket.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 9222)
    with patch("sys.platform", "darwin"), patch("socket.socket", return_value=mock_socket):
        res = await convert_html_to_pdf(src, tgt, "tgt.pdf", {})
        
    assert mock_popen.called
    args = mock_popen.call_args[0][0]
    assert "--no-sandbox" not in args
    assert "--disable-setuid-sandbox" not in args
    assert "WeasyPrint" in res
