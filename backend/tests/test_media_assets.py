import base64
from io import BytesIO

import pytest
from PIL import Image

from app.services.agent_tools import _read_file
from app.services.media_assets import (
    apply_image_text_overlay,
    image_reference_for_provider,
    validate_generated_image,
)


def _image_bytes(size=(512, 512), image_format="PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (32, 96, 160)).save(output, format=image_format)
    return output.getvalue()


def test_workspace_reference_is_validated_and_transported_as_data_url(tmp_path):
    source = tmp_path / "workspace" / "product.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(_image_bytes((720, 480)))

    data_url = image_reference_for_provider(
        tmp_path,
        "workspace/product.png",
        label="First-frame image",
        require_video_dimensions=True,
    )

    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == source.read_bytes()


def test_video_reference_rejects_too_small_image(tmp_path):
    source = tmp_path / "small.png"
    source.write_bytes(_image_bytes((300, 600)))

    with pytest.raises(ValueError, match="short edge over 300px"):
        image_reference_for_provider(
            tmp_path,
            "small.png",
            label="First-frame image",
            require_video_dimensions=True,
        )


def test_reference_path_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_image_bytes())

    with pytest.raises(ValueError, match="outside the workspace"):
        image_reference_for_provider(tmp_path, "../outside.png", label="Reference image")


def test_cjk_overlay_is_rendered_and_output_matches_requested_format():
    raw = _image_bytes()

    result = apply_image_text_overlay(
        raw,
        "深圳新品 Product 2026",
        position="bottom",
        output_format=".jpg",
    )

    assert result.startswith(b"\xff\xd8\xff")
    assert result != raw
    assert validate_generated_image(result) == (512, 512)


def test_image_normalization_prevents_extension_content_mismatch():
    jpeg = _image_bytes(image_format="JPEG")

    result = apply_image_text_overlay(jpeg, None, output_format=".png")

    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert validate_generated_image(result) == (512, 512)


def test_read_file_rejects_binary_instead_of_returning_mojibake(tmp_path):
    image_path = tmp_path / "workspace" / "product.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_image_bytes())

    result = _read_file(tmp_path, "workspace/product.png")

    assert "binary file" in result
    assert "first_frame_image" in result
    assert "�" not in result
