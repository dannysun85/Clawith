import asyncio
import base64
import hashlib
from io import BytesIO
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageStat

from app.services.agent_tools import _read_file
from app.services import media_assets
from app.services.media_assets import (
    AudioMixReceipt,
    MediaContractError,
    OverlayReceipt,
    apply_image_brand_overlays,
    apply_image_text_overlay,
    apply_video_brand_overlays,
    compose_video_audio_tracks,
    image_asset_from_bytes,
    image_reference_for_provider,
    preflight_poster_layout,
    trim_generated_audio,
    validate_generated_audio,
    validate_generated_image,
    validate_generated_video,
    validate_overlay_blocks,
    validate_overlay_text,
    validate_uploaded_video,
    validate_video_delivery_contract,
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


def _real_audio(
    tmp_path: Path,
    audio_format: str,
    *,
    duration_seconds: float = 0.25,
) -> bytes:
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
            f"sine=frequency=440:duration={duration_seconds}",
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


def test_ultra_first_frame_is_compacted_before_video_submission(tmp_path):
    source = tmp_path / "workspace" / "ultra-first-frame.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(_image_bytes((2304, 4096)))

    transport_metadata: dict[str, object] = {}
    data_url = image_reference_for_provider(
        tmp_path,
        "workspace/ultra-first-frame.png",
        label="First-frame image",
        require_video_dimensions=True,
        transport_metadata=transport_metadata,
    )

    assert data_url.startswith("data:image/jpeg;base64,")
    compacted = base64.b64decode(data_url.split(",", 1)[1])
    assert len(compacted) < 2 * 1024 * 1024
    with Image.open(BytesIO(compacted)) as image:
        assert image.size == (1080, 1920)
    assert transport_metadata["source_width"] == 2304
    assert transport_metadata["source_height"] == 4096
    assert transport_metadata["transport_width"] == 1080
    assert transport_metadata["transport_height"] == 1920
    assert transport_metadata["transport_bytes"] == len(compacted)
    assert transport_metadata["transport_sha256"] == hashlib.sha256(
        compacted
    ).hexdigest()
    assert transport_metadata["compacted"] is True


def test_workspace_reference_accepts_chat_upload_shorthand(tmp_path):
    source = tmp_path / "workspace" / "uploads" / "product.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(_image_bytes((720, 480)))

    data_url = image_reference_for_provider(
        tmp_path,
        "uploads/product.png",
        label="Reference image",
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


def test_simplified_chinese_prefers_sc_face_from_cjk_collection(tmp_path, monkeypatch):
    collection = tmp_path / "NotoSansCJK.ttc"
    collection.write_bytes(b"font-collection")
    supported = frozenset(ord(character) for character in "中文 English 123")
    monkeypatch.setattr(media_assets, "_FONT_CANDIDATES", (str(collection),))
    monkeypatch.setattr(
        media_assets,
        "_font_faces",
        lambda _path: (
            (0, supported, "Noto Sans CJK JP"),
            (1, supported, "Noto Sans CJK KR"),
            (2, supported, "Noto Sans CJK SC"),
        ),
    )
    monkeypatch.setattr(media_assets, "_file_sha256", lambda _path: "0" * 64)

    selection = media_assets._font_for_text("中文 English 123")

    assert selection.family == "Noto Sans CJK SC"
    assert selection.face_index == 2


def test_simplified_chinese_prefers_weighted_sc_family_from_cjk_collection(tmp_path, monkeypatch):
    collection = tmp_path / "NotoSansCJK-Black.ttc"
    collection.write_bytes(b"font-collection")
    supported = frozenset(ord(character) for character in "量化交易平台")
    monkeypatch.setattr(media_assets, "_FONT_CANDIDATES", (str(collection),))
    monkeypatch.setattr(
        media_assets,
        "_font_faces",
        lambda _path: (
            (0, supported, "Noto Sans CJK JP Black"),
            (2, supported, "Noto Sans CJK SC Black"),
        ),
    )
    monkeypatch.setattr(media_assets, "_file_sha256", lambda _path: "0" * 64)

    selection = media_assets._font_for_text("量化交易平台")

    assert selection.family == "Noto Sans CJK SC Black"
    assert selection.face_index == 2


def test_chinese_poster_separator_does_not_select_japanese_face(tmp_path, monkeypatch):
    collection = tmp_path / "NotoSansCJK.ttc"
    collection.write_bytes(b"font-collection")
    supported = frozenset(ord(character) for character in "中文排版・清晰可读")
    monkeypatch.setattr(media_assets, "_FONT_CANDIDATES", (str(collection),))
    monkeypatch.setattr(
        media_assets,
        "_font_faces",
        lambda _path: (
            (0, supported, "Noto Sans CJK JP"),
            (2, supported, "Noto Sans CJK SC"),
        ),
    )
    monkeypatch.setattr(media_assets, "_file_sha256", lambda _path: "0" * 64)

    selection = media_assets._font_for_text("中文排版・清晰可读")

    assert selection.family == "Noto Sans CJK SC"
    assert selection.face_index == 2


def test_japanese_copy_prefers_jp_face_from_cjk_collection(tmp_path, monkeypatch):
    collection = tmp_path / "NotoSansCJK.ttc"
    collection.write_bytes(b"font-collection")
    supported = frozenset(ord(character) for character in "日本語テスト")
    monkeypatch.setattr(media_assets, "_FONT_CANDIDATES", (str(collection),))
    monkeypatch.setattr(
        media_assets,
        "_font_faces",
        lambda _path: (
            (0, supported, "Noto Sans CJK JP"),
            (2, supported, "Noto Sans CJK SC"),
        ),
    )
    monkeypatch.setattr(media_assets, "_file_sha256", lambda _path: "0" * 64)

    selection = media_assets._font_for_text("日本語テスト")

    assert selection.family == "Noto Sans CJK JP"
    assert selection.face_index == 0


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


def test_role_aware_poster_blocks_render_without_legacy_black_panel():
    blocks = [
        {"role": "title", "text": "量化交易平台"},
        {"role": "subtitle", "text": "智能策略・实时信号・数据驱动决策"},
        {"role": "tagline", "text": "从复杂市场中，捕捉更清晰的交易方向"},
        {"role": "cta", "text": "立即体验"},
    ]

    blocks_digest = validate_overlay_blocks(blocks)
    assert blocks_digest is not None
    result, receipt = apply_image_brand_overlays(
        _image_bytes((720, 1280)),
        None,
        overlay_blocks=blocks,
        output_format=".png",
    )

    assert validate_generated_image(result) == (720, 1280)
    assert receipt.layout_version == "poster-v3"
    assert receipt.block_count == 4
    assert receipt.overlay_blocks_sha256 == blocks_digest
    assert receipt.font_family is not None
    assert receipt.font_roles is not None
    assert set(receipt.font_roles) == {"title", "subtitle", "tagline", "cta"}
    for role_receipt in receipt.font_roles.values():
        assert set(role_receipt) == {"family", "face_index", "sha256"}
        assert len(str(role_receipt["sha256"])) == 64
    assert receipt.font_family == receipt.font_roles["title"]["family"]
    assert receipt.font_sha256 == receipt.font_roles["title"]["sha256"]
    assert receipt.font_face_index == receipt.font_roles["title"]["face_index"]
    assert receipt.output_bytes == len(result)
    assert receipt.layout_bounds_verified is True
    assert receipt.safe_margin_x is not None
    assert receipt.safe_margin_y is not None
    assert receipt.content_left is not None
    assert receipt.content_right is not None
    assert receipt.content_left >= receipt.safe_margin_x
    assert receipt.content_right <= receipt.source_width - receipt.safe_margin_x
    assert receipt.content_top >= receipt.safe_margin_y
    assert receipt.content_bottom <= receipt.source_height - receipt.safe_margin_y
    with Image.open(BytesIO(result)) as image:
        # The legacy renderer put an opaque black panel behind all copy. The
        # structured poster renderer keeps the supplied background visible.
        assert image.convert("RGB").getpixel((360, 640)) != (0, 0, 0)


def test_commercial_poster_title_prefers_black_sans_over_serif() -> None:
    candidates = media_assets._POSTER_FONT_CANDIDATES_BY_ROLE["title"]

    assert "NotoSansCJK-Black" in candidates[0]
    assert next(path for path in candidates if "NotoSerif" in path) != candidates[0]


def test_commercial_poster_secondary_copy_adapts_to_background_luminance() -> None:
    bright = Image.new("RGB", (320, 200), (236, 220, 250))
    dark = Image.new("RGB", (320, 200), (28, 23, 72))
    copy_region = (40, 60, 280, 110)

    assert media_assets._poster_region_is_bright(bright, copy_region) is True
    assert media_assets._poster_region_is_bright(dark, copy_region) is False


def test_poster_preflight_rejects_copy_that_cannot_fit_the_safe_area():
    blocks = [
        {
            "role": "body",
            "text": "一\n二\n三\n四\n五\n六\n七\n八\n九\n十",
        }
        for _ in range(6)
    ]

    with pytest.raises(MediaContractError, match="poster safe area"):
        preflight_poster_layout(blocks, aspect_ratio="16:9")


def test_commercial_poster_title_avoids_single_character_widow():
    blocks = [
        {"role": "title", "text": "把 AI 公司真正运行起来"},
        {"role": "subtitle", "text": "数字员工・任务协作・WorkProduct 审核"},
        {"role": "tagline", "text": "从任务到成果，企业运营真正闭环"},
        {"role": "body", "text": "ReefTotem｜深圳前海瑞孚图腾科技有限公司"},
        {"role": "cta", "text": "立即体验"},
    ]

    receipt = preflight_poster_layout(blocks, aspect_ratio="9:16")

    assert receipt.block_count == 5
    assert receipt.line_count == 5


def test_poster_copy_after_cta_is_rendered_as_a_footer():
    blocks = [
        {"role": "title", "text": "把 AI 公司真正运行起来"},
        {"role": "subtitle", "text": "数字员工 · 任务协作 · 成果审核"},
        {"role": "tagline", "text": "从需求到商业成果，完整闭环"},
        {"role": "cta", "text": "立即体验 ReefTotem OPC"},
        {"role": "body", "text": "深圳前海瑞孚图腾科技有限公司"},
    ]

    receipt = preflight_poster_layout(blocks, aspect_ratio="9:16")

    assert receipt.block_count == 5
    assert receipt.line_count == 5
    assert receipt.content_bottom is not None
    assert receipt.content_bottom >= int(1920 * 0.88)


def test_glass_cta_uses_rose_to_violet_gradient_instead_of_flat_fill():
    canvas = Image.new("RGBA", (320, 120), (0, 0, 0, 0))

    media_assets._composite_glass_cta(canvas, (20, 20, 300, 100), 40)

    left_red, _left_green, left_blue, left_alpha = canvas.getpixel((70, 60))
    right_red, _right_green, right_blue, right_alpha = canvas.getpixel((250, 60))
    assert left_red > right_red
    assert right_blue > left_blue
    assert left_alpha > 200
    assert right_alpha > 200


def test_bounded_encoder_downscales_only_when_encoded_result_exceeds_limit():
    image = Image.effect_noise((720, 1280), 100).convert("RGBA")

    result, output_size = media_assets._encode_bounded_image(
        image,
        "PNG",
        max_bytes=220_000,
    )

    assert len(result) < 220_000
    assert output_size[0] < 720
    assert output_size[1] < 1280
    assert output_size[0] / output_size[1] == pytest.approx(720 / 1280, rel=0.01)


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
async def test_unbranded_video_is_normalized_to_browser_safe_mp4(tmp_path):
    source = _real_mp4(tmp_path)
    source_info = await validate_generated_video(
        source,
        require_browser_safe=False,
    )

    result, receipt = await apply_video_brand_overlays(source, None)
    result_info = await validate_generated_video(result)

    assert source_info.fast_start is False
    assert result_info.codec_name == "h264"
    assert result_info.pixel_format == "yuv420p"
    assert result_info.audio_codec_name in {None, "aac"}
    assert result_info.fast_start is True
    assert receipt == OverlayReceipt()


def test_video_delivery_contract_accepts_matching_duration_ratio_and_audio():
    accepted = media_assets.VideoInfo(
        width=768,
        height=1366,
        duration_seconds=10.125,
        codec_name="h264",
        pixel_format="yuv420p",
        audio_codec_name="aac",
        fast_start=True,
    )

    assert (
        validate_video_delivery_contract(
            accepted,
            expected_duration_seconds=10,
            expected_aspect_ratio="9:16",
            require_audio=True,
        )
        is accepted
    )


def test_image_delivery_contract_accepts_matching_ratio_and_rejects_mismatch():
    assert media_assets.validate_image_delivery_contract(
        1440,
        2560,
        expected_aspect_ratio="9:16",
    ) == (1440, 2560)

    with pytest.raises(
        MediaContractError,
        match="aspect ratio is 2560:1440, expected 9:16",
    ):
        media_assets.validate_image_delivery_contract(
            2560,
            1440,
            expected_aspect_ratio="9:16",
        )


def test_video_delivery_contract_rejects_customer_visible_mismatch():
    rejected = media_assets.VideoInfo(
        width=1366,
        height=768,
        duration_seconds=5.875,
        codec_name="h264",
        pixel_format="yuv420p",
        audio_codec_name=None,
        fast_start=True,
    )

    with pytest.raises(
        MediaContractError,
        match=(
            "duration is 5.875s.*aspect ratio is 1366:768.*"
            "audio stream is required but missing"
        ),
    ):
        validate_video_delivery_contract(
            rejected,
            expected_duration_seconds=10,
            expected_aspect_ratio="9:16",
            require_audio=True,
        )


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
async def test_video_title_uses_display_font_without_legacy_black_panel(tmp_path):
    source = _real_mp4(tmp_path)

    result, receipt = await apply_video_brand_overlays(
        source,
        "量化交易平台",
        text_position="center",
    )

    assert receipt.layout_version == "single-text-v2"
    assert receipt.block_count == 1
    assert receipt.layout_bounds_verified is True
    assert receipt.font_family is not None
    assert "Serif" not in receipt.font_family
    assert "Song" not in receipt.font_family

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
        center_band = rendered.crop((180, 130, 460, 230))
        dark_panel_pixels = sum(
            1
            for red, green, blue in center_band.get_flattened_data()
            if red < 24 and green < 58 and blue < 72
        )

    assert dark_panel_pixels / (center_band.width * center_band.height) < 0.25


@pytest.mark.asyncio
async def test_silent_video_can_be_finished_with_voiceover_and_music(tmp_path):
    source = _real_mp4(tmp_path)
    voice_dir = tmp_path / "voice"
    music_dir = tmp_path / "music"
    voice_dir.mkdir()
    music_dir.mkdir()
    voiceover = _real_audio(voice_dir, "mp3")
    music = _real_audio(music_dir, "wav")

    result, receipt = await compose_video_audio_tracks(
        source,
        voiceover_raw=voiceover,
        voiceover_format="mp3",
        music_raw=music,
        music_format="wav",
        voiceover_start_seconds=0.1,
        voiceover_gain=1.0,
        music_gain=0.12,
    )
    info = await validate_generated_video(result)

    assert isinstance(receipt, AudioMixReceipt)
    assert receipt.voiceover_sha256
    assert receipt.music_sha256
    assert receipt.source_audio_retained is False
    assert receipt.output_video_codec == "h264"
    assert receipt.output_audio_codec == "aac"
    assert receipt.output_width == 640
    assert receipt.output_height == 360
    assert receipt.browser_safe is True
    assert receipt.fast_start is True
    assert info.audio_codec_name == "aac"
    assert info.width == 640
    assert info.height == 360
    assert abs(info.duration_seconds - 1.2) <= 0.1
    assert info.fast_start is True


@pytest.mark.asyncio
async def test_video_audio_composition_requires_an_actual_audio_track(tmp_path):
    with pytest.raises(MediaContractError, match="At least one"):
        await compose_video_audio_tracks(_real_mp4(tmp_path))


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
@pytest.mark.parametrize("audio_format", ["mp3", "wav"])
async def test_full_music_track_is_trimmed_to_customer_duration(tmp_path, audio_format):
    source = _real_audio(
        tmp_path,
        audio_format,
        duration_seconds=2.0,
    )

    result, info = await trim_generated_audio(
        source,
        audio_format=audio_format,
        duration_seconds=0.8,
        label="Commercial music clip",
    )

    assert result != source
    assert abs(info.duration_seconds - 0.8) <= 0.15
    assert (
        await validate_generated_audio(result, audio_format=audio_format)
    ).duration_seconds == info.duration_seconds


@pytest.mark.asyncio
async def test_music_duration_contract_rejects_short_provider_output(tmp_path):
    source = _real_audio(tmp_path, "mp3", duration_seconds=0.5)

    with pytest.raises(MediaContractError, match="shorter than the requested"):
        await trim_generated_audio(
            source,
            audio_format="mp3",
            duration_seconds=1.5,
            label="Commercial music clip",
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
    with Image.open(BytesIO(result)) as image:
        assert image.mode == "RGB"


def test_commercial_image_normalizes_provider_dimensions_before_composition():
    source = _image_bytes((900, 1600))

    result, receipt = apply_image_brand_overlays(
        source,
        None,
        overlay_blocks=(
            {"role": "title", "text": "量化交易平台"},
            {"role": "tagline", "text": "从复杂市场中，捕捉更清晰的交易方向"},
        ),
        output_dimensions="540x960",
        output_format=".png",
    )

    assert validate_generated_image(result) == (540, 960)
    assert receipt.source_width == 900
    assert receipt.source_height == 1600
    assert receipt.output_width == 540
    assert receipt.output_height == 960
    assert receipt.size_adjusted is True


def test_commercial_image_rejects_dimension_normalization_that_changes_ratio():
    with pytest.raises(
        MediaContractError,
        match="without changing its aspect ratio",
    ):
        apply_image_brand_overlays(
            _image_bytes((900, 1600)),
            None,
            output_dimensions="960x540",
            output_format=".png",
        )


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
