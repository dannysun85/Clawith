import pytest
from fastapi import HTTPException

from app.api import upload


def test_chat_image_extensions_match_minimax_m3_contract():
    assert upload.IMAGE_EXTENSIONS == {".png", ".jpg", ".jpeg", ".webp"}
    assert ".bmp" not in upload.MIME_MAP
    assert ".gif" not in upload.MIME_MAP


@pytest.mark.parametrize("extension", [".bmp", ".gif"])
def test_unsupported_image_upload_is_rejected_before_storage_or_provider_use(extension):
    with pytest.raises(HTTPException) as exc:
        upload._validate_multimodal_upload_extension(extension)

    assert exc.value.status_code == 400
    assert "JPEG, PNG, or WEBP" in exc.value.detail
