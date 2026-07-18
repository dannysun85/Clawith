import asyncio
import base64
from io import BytesIO
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageStat

from app.services.agent_tools import _read_file
from app.services import media_assets
from app.services.media_assets import (
    MediaContractError,
    apply_image_brand_overlays,
    apply_image_text_overlay,
    apply_video_brand_overlays,
    image_asset_from_bytes,
    image_reference_for_provider,
    validate_generated_audio,
    validate_generated_video,
    validate_generated_image,
    validate_overlay_text,
    validate_uploaded_video,
)


def _image_bytes(size=(512, 512), image_format="PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (32, 96, 160)).save(output, format=image_format)
    return output.getvalue()


def _brand_asset_bytes(size=(160, 120)) -> bytes:
    output = BytesIO()
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, size[0] - 8, size[1] - 8), radius=18, fill=(238, 32, 48, 255))
    image.save(output, format="PNG")
    return output.getvalue()


def _striped_image_bytes(size=(640, 360)) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", size, "black")
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 4):
        draw.rectangle((x, 0, x + 1, size[1]), fill="white")
    image.save(output, format="PNG")
    return output.getvalue()


def _real_mp4(tmp_path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the real media contract test")
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#205080:s=640x360:d=1.2:r=24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _real_audio(tmp_path: Path, audio_format: str) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the real audio contract test")
    path = tmp_path / f"source.{audio_format}"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


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


def test_reference_image_rejects_oversized_edge_before_pixel_conversion():
    raw = _image_bytes((media_assets.MAX_IMAGE_EDGE_PIXELS + 1, 1))

    with pytest.raises(MediaContractError, match="must not exceed 8192px per edge"):
        image_asset_from_bytes(raw, label="Reference image")


def test_reference_image_rejects_oversized_pixel_count_before_decode(monkeypatch):
    class HeaderOnlyImage:
        format = "PNG"
        size = (7000, 6000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def verify(self):
            raise AssertionError("verify must not run after an oversized header")

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: HeaderOnlyImage())

    with pytest.raises(MediaContractError, match="40,000,000 total pixels"):
        image_asset_from_bytes(b"header-only", label="Reference image")


def test_reference_path_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_image_bytes())

    with pytest.raises(ValueError, match="outside the workspace"):
        image_reference_for_provider(tmp_path, "../outside.png", label="Reference image")


def test_remote_reference_must_be_uploaded_before_provider_submission(tmp_path):
    with pytest.raises(MediaContractError, match="uploaded to the Agent workspace"):
        image_reference_for_provider(
            tmp_path,
            "https://example.invalid/product.png",
            label="Reference image",
        )


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


def test_wrapping_never_drops_exact_customer_copy():
    text = "深圳前海瑞孚图腾科技有限公司 OPC Product 2026"
    selection = media_assets._font_for_text(text)
    font = ImageFont.truetype(selection.path, 28, index=selection.face_index)
    draw = ImageDraw.Draw(Image.new("RGB", (640, 360)))

    lines = media_assets._wrapped_lines(draw, text, font, 180)

    assert "".join(lines) == text


def test_exact_copy_fails_instead_of_silent_truncation_when_canvas_is_too_small():
    with pytest.raises(MediaContractError, match="without truncation"):
        apply_image_brand_overlays(
            _image_bytes((128, 128)),
            "深" * 300,
        )


def test_missing_font_glyph_is_rejected_before_provider_call(tmp_path, monkeypatch):
    fake_font = tmp_path / "ascii-only.ttf"
    fake_font.write_bytes(b"not-a-real-font")
    monkeypatch.setattr(media_assets, "_FONT_CANDIDATES", (str(fake_font),))
    monkeypatch.setattr(
        media_assets,
        "_font_faces",
        lambda _path: ((0, frozenset(ord(character) for character in "ASCII 123"), "ASCII"),),
    )
    monkeypatch.setattr(media_assets, "_file_sha256", lambda _path: "0" * 64)

    with pytest.raises(MediaContractError, match="covers every requested character"):
        validate_overlay_text("中文")


def test_product_asset_is_composited_unchanged_and_receipted():
    brand_raw = _brand_asset_bytes()
    brand_asset = image_asset_from_bytes(brand_raw, label="Product")

    result, receipt = apply_image_brand_overlays(
        _image_bytes((640, 360)),
        "新品发布 OPC 2026",
        text_position="bottom",
        brand_asset=brand_asset,
        brand_position="center",
        brand_scale=0.25,
        sanitize_generated_background=True,
    )

    assert receipt.brand_asset_sha256 == brand_asset.sha256
    assert receipt.rendered_text_sha256 is not None
    assert receipt.font_sha256 is not None
    assert receipt.line_count >= 1
    assert receipt.background_sanitized is True
    with Image.open(BytesIO(result)) as image:
        red, green, blue = image.convert("RGB").getpixel((320, 180))
    assert red > 220 and green < 70 and blue < 80


def test_brand_safe_image_suppresses_provider_background_pseudo_text():
    brand_asset = image_asset_from_bytes(_brand_asset_bytes(), label="Product")

    original, _ = apply_image_brand_overlays(
        _striped_image_bytes(),
        None,
        brand_asset=brand_asset,
        sanitize_generated_background=False,
    )
    sanitized, receipt = apply_image_brand_overlays(
        _striped_image_bytes(),
        None,
        brand_asset=brand_asset,
        sanitize_generated_background=True,
    )

    with Image.open(BytesIO(original)) as original_image:
        original_variance = ImageStat.Stat(
            original_image.convert("L").crop((0, 0, 160, 120))
        ).var[0]
    with Image.open(BytesIO(sanitized)) as sanitized_image:
        sanitized_variance = ImageStat.Stat(
            sanitized_image.convert("L").crop((0, 0, 160, 120))
        ).var[0]

    assert sanitized_variance < original_variance * 0.1
    assert receipt.background_sanitized is True


@pytest.mark.asyncio
async def test_real_video_decodes_and_contains_exact_copy_and_protected_product(tmp_path):
    source = _real_mp4(tmp_path)
    brand_asset = image_asset_from_bytes(_brand_asset_bytes(), label="Product")

    result, receipt = await apply_video_brand_overlays(
        source,
        "深圳新品 Product 2026",
        text_position="bottom",
        brand_asset=brand_asset,
        brand_position="center",
        brand_scale=0.25,
        sanitize_generated_background=True,
    )
    info = await validate_generated_video(result)

    assert info.width == 640
    assert info.height == 360
    assert info.duration_seconds >= 1.0
    assert receipt.brand_asset_sha256 == brand_asset.sha256
    assert receipt.rendered_text_sha256 is not None
    assert receipt.background_sanitized is True

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    frame = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        input=result,
        stdout=subprocess.PIPE,
        check=True,
    )
    with Image.open(BytesIO(frame.stdout)) as image:
        rendered = image.convert("RGB")
        red, green, blue = rendered.getpixel((320, 180))
    assert red > 180 and green < 100 and blue < 110
    assert any(
        all(channel > 210 for channel in rendered.getpixel((x, y)))
        for x in range(0, 640, 4)
        for y in range(250, 360, 4)
    )


@pytest.mark.asyncio
async def test_mp4_header_without_decodable_video_is_rejected():
    with pytest.raises(MediaContractError, match="Video validation failed"):
        await validate_generated_video(b"\x00\x00\x00\x18ftypmp42not-a-real-video")


@pytest.mark.asyncio
async def test_uploaded_video_container_must_match_filename(tmp_path):
    raw = _real_mp4(tmp_path)

    info = await validate_uploaded_video(raw, extension=".mp4")
    assert info.codec_name == "h264"
    with pytest.raises(MediaContractError, match="do not match the .avi container"):
        await validate_uploaded_video(raw, extension=".avi")


@pytest.mark.asyncio
async def test_nonempty_fake_audio_is_rejected_instead_of_delivered():
    with pytest.raises(MediaContractError, match="Audio validation failed"):
        await validate_generated_audio(
            b"fake-mp3",
            audio_format="mp3",
            label="MiniMax speech output",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual_format", "requested_format"),
    [("wav", "mp3"), ("mp3", "wav")],
)
async def test_audio_container_must_match_requested_format(
    tmp_path,
    actual_format,
    requested_format,
):
    with pytest.raises(MediaContractError, match="does not match requested"):
        await validate_generated_audio(
            _real_audio(tmp_path, actual_format),
            audio_format=requested_format,
        )


@pytest.mark.asyncio
async def test_cancelled_media_process_is_killed_and_reaped(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.killed = False
            self.reaped = False
            self.done = asyncio.Event()

        async def communicate(self):
            await self.done.wait()
            self.reaped = True
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.done.set()

    process = FakeProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    task = asyncio.create_task(
        media_assets._run_process("ffprobe", timeout=30, label="test probe")
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.reaped is True


def test_workspace_output_path_rejects_media_extension_mismatch(tmp_path):
    from app.services.agent_tools import _resolve_workspace_output_path

    with pytest.raises(ValueError, match="must end with .mp4"):
        _resolve_workspace_output_path(
            tmp_path,
            "workspace/videos/ad.webm",
            "workspace/videos",
            "video",
            "mp4",
            "ad",
        )


def test_image_normalization_prevents_extension_content_mismatch():
    jpeg = _image_bytes(image_format="JPEG")

    result = apply_image_text_overlay(jpeg, None, output_format=".png")

    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert validate_generated_image(result) == (512, 512)


@pytest.mark.parametrize("output_format", [".png"])
def test_transparent_output_formats_preserve_alpha(output_format):
    output = BytesIO()
    source = Image.new("RGBA", (128, 96), (20, 40, 60, 0))
    source.putpixel((64, 48), (240, 30, 50, 255))
    source.save(output, format="PNG")

    result, _receipt = apply_image_brand_overlays(
        output.getvalue(),
        None,
        output_format=output_format,
    )

    with Image.open(BytesIO(result)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((64, 48))[3] == 255


def test_webp_output_is_rejected_when_release_runtime_has_no_stable_encoder():
    with pytest.raises(MediaContractError, match="WebP output encoding is unavailable"):
        apply_image_brand_overlays(
            _image_bytes(),
            None,
            output_format=".webp",
        )


def test_exif_oriented_mobile_image_is_transposed_before_delivery():
    output = BytesIO()
    source = Image.new("RGB", (80, 160), (30, 90, 150))
    exif = Image.Exif()
    exif[274] = 6
    source.save(output, format="JPEG", exif=exif)

    raw = output.getvalue()
    assert validate_generated_image(raw) == (160, 80)

    result, _receipt = apply_image_brand_overlays(
        raw,
        None,
        output_format=".png",
    )
    with Image.open(BytesIO(result)) as image:
        assert image.size == (160, 80)


def test_read_file_rejects_binary_instead_of_returning_mojibake(tmp_path):
    image_path = tmp_path / "workspace" / "product.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_image_bytes())

    result = _read_file(tmp_path, "workspace/product.png")

    assert "binary file" in result
    assert "first_frame_image" in result
    assert "�" not in result
