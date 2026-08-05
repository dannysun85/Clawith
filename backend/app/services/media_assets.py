"""Validated media assets and deterministic brand-safe post-processing."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unicodedata
import warnings


MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_EDGE_PIXELS = 8192
MAX_IMAGE_PIXELS = 40_000_000
MAX_OVERLAY_TEXT_CHARS = 300
MAX_OVERLAY_BLOCKS = 8
MAX_OVERLAY_BLOCK_TEXT_CHARS = 300
MAX_OVERLAY_BLOCK_TOTAL_CHARS = 600
SUPPORTED_REFERENCE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)
_POSTER_FONT_CANDIDATES_BY_ROLE = {
    "title": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Black.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ),
    "subtitle": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-SemiBold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ),
    "tagline": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-DemiLight.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ),
    "body": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-DemiLight.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ),
    "cta": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
    ),
}
_TEXT_POSITIONS = {"top", "center", "bottom"}
_OVERLAY_BLOCK_ROLES = {"title", "subtitle", "tagline", "body", "cta"}
_BRAND_POSITIONS = {
    "top_left",
    "top_right",
    "center",
    "bottom_left",
    "bottom_right",
}


class MediaContractError(ValueError):
    """The requested media cannot meet the deterministic delivery contract."""


@dataclass(frozen=True, slots=True)
class ImageAsset:
    raw: bytes
    mime_type: str
    width: int
    height: int
    sha256: str
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class FontSelection:
    path: str
    face_index: int
    family: str
    sha256: str


@dataclass(frozen=True, slots=True)
class OverlayReceipt:
    rendered_text_sha256: str | None = None
    brand_asset_sha256: str | None = None
    font_sha256: str | None = None
    font_family: str | None = None
    font_face_index: int | None = None
    font_roles: dict[str, dict[str, str | int]] | None = None
    line_count: int = 0
    background_sanitized: bool = False
    layout_version: str | None = None
    block_count: int = 0
    overlay_blocks_sha256: str | None = None
    source_width: int | None = None
    source_height: int | None = None
    output_width: int | None = None
    output_height: int | None = None
    output_bytes: int | None = None
    size_adjusted: bool = False
    layout_bounds_verified: bool = False
    content_left: int | None = None
    content_top: int | None = None
    content_right: int | None = None
    content_bottom: int | None = None
    safe_margin_x: int | None = None
    safe_margin_y: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VideoInfo:
    width: int
    height: int
    duration_seconds: float
    codec_name: str
    pixel_format: str
    audio_codec_name: str | None
    fast_start: bool


@dataclass(frozen=True, slots=True)
class AudioInfo:
    duration_seconds: float
    codec_name: str
    sample_rate: int
    channels: int
    container_format: str


@dataclass(frozen=True, slots=True)
class AudioMixReceipt:
    voiceover_sha256: str | None
    music_sha256: str | None
    voiceover_start_seconds: float
    voiceover_gain: float
    music_gain: float
    source_audio_retained: bool
    output_duration_seconds: float
    output_video_codec: str
    output_audio_codec: str
    output_width: int
    output_height: int
    browser_safe: bool
    fast_start: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _validate_image_bytes(
    raw: bytes,
    *,
    label: str,
    require_video_dimensions: bool = False,
) -> tuple[str, int, int]:
    if not raw:
        raise MediaContractError(f"{label} is empty")
    if len(raw) >= MAX_REFERENCE_IMAGE_BYTES:
        raise MediaContractError(f"{label} must be smaller than 20MB")

    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                image_format = str(image.format or "").upper()
                encoded_width, encoded_height = image.size
                getexif = getattr(image, "getexif", None)
                orientation = int(getexif().get(274, 1) or 1) if getexif else 1
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise MediaContractError(f"{label} exceeds the safe image dimensions") from exc
    except Exception as exc:
        raise MediaContractError(f"{label} is not a valid JPG, PNG, or WebP image") from exc

    if image_format not in SUPPORTED_REFERENCE_FORMATS:
        raise MediaContractError(f"{label} format is not supported; use JPG, PNG, or WebP")
    if encoded_width <= 0 or encoded_height <= 0:
        raise MediaContractError(f"{label} has invalid dimensions")
    if encoded_width > MAX_IMAGE_EDGE_PIXELS or encoded_height > MAX_IMAGE_EDGE_PIXELS:
        raise MediaContractError(
            f"{label} dimensions must not exceed {MAX_IMAGE_EDGE_PIXELS}px per edge"
        )
    if encoded_width * encoded_height > MAX_IMAGE_PIXELS:
        raise MediaContractError(
            f"{label} must not exceed {MAX_IMAGE_PIXELS:,} total pixels"
        )
    width, height = (
        (encoded_height, encoded_width)
        if orientation in {5, 6, 7, 8}
        else (encoded_width, encoded_height)
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                image.verify()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise MediaContractError(f"{label} exceeds the safe image dimensions") from exc
    except Exception as exc:
        raise MediaContractError(f"{label} is not a valid JPG, PNG, or WebP image") from exc
    if require_video_dimensions:
        short_edge = min(width, height)
        aspect_ratio = width / height
        if short_edge <= 300 or not 0.4 <= aspect_ratio <= 2.5:
            raise MediaContractError(
                f"{label} must have a short edge over 300px and an aspect ratio between 2:5 and 5:2"
            )
    return SUPPORTED_REFERENCE_FORMATS[image_format], width, height


def image_asset_from_bytes(
    raw: bytes,
    *,
    label: str,
    source_path: str | None = None,
    require_video_dimensions: bool = False,
) -> ImageAsset:
    mime_type, width, height = _validate_image_bytes(
        raw,
        label=label,
        require_video_dimensions=require_video_dimensions,
    )
    return ImageAsset(
        raw=raw,
        mime_type=mime_type,
        width=width,
        height=height,
        sha256=hashlib.sha256(raw).hexdigest(),
        source_path=source_path,
    )


def validate_generated_image(raw: bytes) -> tuple[int, int]:
    """Reject empty or corrupt provider output before storage and settlement."""
    asset = image_asset_from_bytes(raw, label="Generated image")
    return asset.width, asset.height


def validate_image_delivery_contract(
    width: int,
    height: int,
    *,
    expected_aspect_ratio: str | None = None,
) -> tuple[int, int]:
    """Reject a valid image whose visible shape violates the user request."""

    ratio_targets = {
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "1:1": 1.0,
        "4:3": 4 / 3,
        "3:4": 3 / 4,
        "3:2": 3 / 2,
        "2:3": 2 / 3,
    }
    normalized_ratio = str(expected_aspect_ratio or "").strip()
    target_ratio = ratio_targets.get(normalized_ratio)
    if target_ratio is None:
        return width, height
    actual_ratio = width / height
    if abs(actual_ratio - target_ratio) / target_ratio > 0.03:
        raise MediaContractError(
            "Image delivery contract invalid: "
            f"aspect ratio is {width}:{height}, expected {normalized_ratio}"
        )
    return width, height


def _decode_image_data_url(value: str, *, label: str) -> bytes:
    try:
        header, encoded = value.split(",", 1)
        if ";base64" not in header.lower():
            raise ValueError
        max_encoded_chars = ((MAX_REFERENCE_IMAGE_BYTES - 1 + 2) // 3) * 4
        if len(encoded) > max_encoded_chars:
            raise MediaContractError(f"{label} must be smaller than 20MB")
        return base64.b64decode(encoded, validate=True)
    except MediaContractError:
        raise
    except Exception as exc:
        raise MediaContractError(f"{label} contains an invalid image data URL") from exc


def _workspace_image_asset(
    workspace: Path,
    value: str,
    *,
    label: str,
    require_video_dimensions: bool = False,
) -> ImageAsset:
    root = workspace.resolve()
    reference = value.strip().replace("\\", "/").lstrip("/")

    def resolve_inside_workspace(candidate: str) -> tuple[Path, str]:
        path = (root / candidate).resolve()
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise MediaContractError(f"{label} is outside the workspace") from exc
        return path, relative_path

    path, relative_path = resolve_inside_workspace(reference)
    if not path.is_file() and reference.startswith("uploads/"):
        path, relative_path = resolve_inside_workspace(f"workspace/{reference}")
    if not path.is_file():
        raise MediaContractError(f"{label} was not found in the workspace: {value}")
    try:
        if path.stat().st_size >= MAX_REFERENCE_IMAGE_BYTES:
            raise MediaContractError(f"{label} must be smaller than 20MB")
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size >= MAX_REFERENCE_IMAGE_BYTES:
                raise MediaContractError(f"{label} must be smaller than 20MB")
            raw = handle.read(MAX_REFERENCE_IMAGE_BYTES)
            if len(raw) >= MAX_REFERENCE_IMAGE_BYTES:
                raise MediaContractError(f"{label} must be smaller than 20MB")
            if os.fstat(handle.fileno()).st_size >= MAX_REFERENCE_IMAGE_BYTES:
                raise MediaContractError(f"{label} must be smaller than 20MB")
    except MediaContractError:
        raise
    except OSError as exc:
        raise MediaContractError(f"{label} could not be read from the workspace") from exc
    return image_asset_from_bytes(
        raw,
        label=label,
        source_path=relative_path,
        require_video_dimensions=require_video_dimensions,
    )


def load_brand_asset(
    workspace: Path,
    value: str | None,
    *,
    label: str = "Brand asset",
    require_workspace_path: bool = False,
) -> ImageAsset | None:
    """Load an immutable product/logo layer without fetching an untrusted URL."""
    reference = str(value or "").strip()
    if not reference:
        return None
    if reference.startswith(("https://", "http://")):
        raise MediaContractError(
            f"{label} must be uploaded to the Agent workspace before brand-safe composition"
        )
    if reference.startswith("data:image/"):
        if require_workspace_path:
            raise MediaContractError(
                f"{label} must use a workspace path so an asynchronous video task can freeze it"
            )
        return image_asset_from_bytes(_decode_image_data_url(reference, label=label), label=label)
    return _workspace_image_asset(workspace, reference, label=label)


def image_reference_for_provider(
    workspace: Path,
    value: str | None,
    *,
    label: str,
    require_video_dimensions: bool = False,
    transport_metadata: dict[str, object] | None = None,
) -> str | None:
    """Return a validated Base64 data URL for MiniMax."""
    reference = str(value or "").strip()
    if not reference:
        return None
    if reference.startswith(("https://", "http://")):
        raise MediaContractError(
            f"{label} must be uploaded to the Agent workspace before provider submission"
        )
    if reference.startswith("data:image/"):
        asset = image_asset_from_bytes(
            _decode_image_data_url(reference, label=label),
            label=label,
            require_video_dimensions=require_video_dimensions,
        )
    else:
        asset = _workspace_image_asset(
            workspace,
            reference,
            label=label,
            require_video_dimensions=require_video_dimensions,
        )
    source_asset = asset
    if require_video_dimensions:
        asset = _compact_video_reference_asset(asset)
    if transport_metadata is not None:
        transport_metadata.clear()
        transport_metadata.update(
            {
                "source_sha256": source_asset.sha256,
                "source_mime_type": source_asset.mime_type,
                "source_width": source_asset.width,
                "source_height": source_asset.height,
                "source_bytes": len(source_asset.raw),
                "transport_sha256": asset.sha256,
                "transport_mime_type": asset.mime_type,
                "transport_width": asset.width,
                "transport_height": asset.height,
                "transport_bytes": len(asset.raw),
                "compacted": asset.sha256 != source_asset.sha256,
            }
        )
    return f"data:{asset.mime_type};base64,{base64.b64encode(asset.raw).decode('ascii')}"


def _compact_video_reference_asset(asset: ImageAsset) -> ImageAsset:
    """Bound I2V request bodies to a browser/video-sized JPEG.

    Ultra image generation can produce a 4K PNG larger than 8 MB. Embedding
    that file unchanged in a JSON video-submission request expands it again by
    roughly one third and has caused the provider gateway to reach its request
    timeout without returning a task identity. Keep small references byte-for-
    byte compatible, but normalize oversized inputs to at most 1920 px and
    2 MB before the provider request starts.
    """

    max_edge = 1920
    max_bytes = 2 * 1024 * 1024
    if max(asset.width, asset.height) <= max_edge and len(asset.raw) <= max_bytes:
        return asset

    from PIL import Image, ImageOps

    with Image.open(BytesIO(asset.raw)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    encoded, _output_size = _encode_bounded_image(
        image,
        "JPEG",
        max_bytes=max_bytes,
    )
    return image_asset_from_bytes(
        encoded,
        label="Compacted video reference",
        source_path=asset.source_path,
        require_video_dimensions=True,
    )


def normalize_overlay_text(text: str | None) -> str:
    """Return the single canonical copy used for hashing and rendering."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not value.strip():
        return ""
    if len(value) > MAX_OVERLAY_TEXT_CHARS:
        raise MediaContractError(f"overlay_text must be at most {MAX_OVERLAY_TEXT_CHARS} characters")
    for character in value:
        if character in {"\n", "\t"}:
            continue
        if unicodedata.category(character).startswith("C"):
            raise MediaContractError("overlay_text contains unsupported control characters")
    return value.replace("\t", "    ")


def normalize_overlay_blocks(value: object) -> tuple[dict[str, str], ...]:
    """Validate role-aware exact copy for deterministic commercial poster layout."""

    if value is None or value == "" or value == [] or value == ():
        return ()
    if not isinstance(value, (list, tuple)):
        raise MediaContractError("overlay_blocks must be an array")
    if not 1 <= len(value) <= MAX_OVERLAY_BLOCKS:
        raise MediaContractError(
            f"overlay_blocks must contain between 1 and {MAX_OVERLAY_BLOCKS} items"
        )
    normalized: list[dict[str, str]] = []
    total_chars = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise MediaContractError(f"overlay_blocks[{index}] must be an object")
        role = str(item.get("role") or "").strip().casefold()
        if role not in _OVERLAY_BLOCK_ROLES:
            raise MediaContractError(
                f"overlay_blocks[{index}].role must be one of: "
                f"{', '.join(sorted(_OVERLAY_BLOCK_ROLES))}"
            )
        text = normalize_overlay_text(item.get("text"))
        if not text:
            raise MediaContractError(f"overlay_blocks[{index}].text must not be empty")
        if len(text) > MAX_OVERLAY_BLOCK_TEXT_CHARS:
            raise MediaContractError(
                f"overlay_blocks[{index}].text must be at most "
                f"{MAX_OVERLAY_BLOCK_TEXT_CHARS} characters"
            )
        total_chars += len(text)
        normalized.append({"role": role, "text": text})
    if total_chars > MAX_OVERLAY_BLOCK_TOTAL_CHARS:
        raise MediaContractError(
            f"overlay_blocks text must total at most {MAX_OVERLAY_BLOCK_TOTAL_CHARS} characters"
        )
    return tuple(normalized)


def overlay_blocks_sha256(value: object) -> str | None:
    blocks = normalize_overlay_blocks(value)
    if not blocks:
        return None
    canonical = json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_overlay_blocks(value: object) -> str | None:
    blocks = normalize_overlay_blocks(value)
    if not blocks:
        return None
    texts_by_role: dict[str, list[str]] = {}
    for block in blocks:
        texts_by_role.setdefault(block["role"], []).append(block["text"])
    for role, texts in texts_by_role.items():
        _font_for_poster_role("\n".join(texts), role)
    return overlay_blocks_sha256(blocks)


@lru_cache(maxsize=32)
def _font_faces(path: str) -> tuple[tuple[int, frozenset[int], str], ...]:
    from fontTools.ttLib import TTCollection, TTFont

    font_path = Path(path)
    if font_path.suffix.lower() in {".ttc", ".otc"}:
        collection = TTCollection(path, lazy=True)
        try:
            faces = []
            for index, font in enumerate(collection.fonts):
                family = font["name"].getDebugName(1) if "name" in font else None
                faces.append((index, frozenset((font.getBestCmap() or {}).keys()), family or font_path.name))
            return tuple(faces)
        finally:
            collection.close()

    font = TTFont(path, lazy=True)
    try:
        family = font["name"].getDebugName(1) if "name" in font else None
        return ((0, frozenset((font.getBestCmap() or {}).keys()), family or font_path.name),)
    finally:
        font.close()


@lru_cache(maxsize=16)
def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _font_for_text(
    text: str,
    *,
    candidates: tuple[str, ...] | None = None,
) -> FontSelection:
    required = {
        ord(character)
        for character in text
        if not character.isspace() and not unicodedata.category(character).startswith("C")
    }
    missing_by_font: list[str] = []
    for candidate in candidates or _FONT_CANDIDATES:
        if not Path(candidate).is_file():
            continue
        faces = _font_faces(candidate)
        preferred_region = "SC"
        if any(
            "\u3041" <= character <= "\u3096"
            or "\u309d" <= character <= "\u309f"
            or "\u30a1" <= character <= "\u30fa"
            or "\u30fd" <= character <= "\u30ff"
            or "\uff66" <= character <= "\uff9d"
            for character in text
        ):
            preferred_region = "JP"
        elif any(
            "\u1100" <= character <= "\u11ff"
            or "\u3130" <= character <= "\u318f"
            or "\uac00" <= character <= "\ud7af"
            for character in text
        ):
            preferred_region = "KR"
        ordered_faces = sorted(
            faces,
            key=lambda face: (
                0
                if preferred_region
                in face[2].upper().replace("-", " ").replace("_", " ").split()
                else 1,
                face[0],
            ),
        )
        for face_index, codepoints, family in ordered_faces:
            missing = required - codepoints
            if not missing:
                return FontSelection(
                    path=candidate,
                    face_index=face_index,
                    family=family,
                    sha256=_file_sha256(candidate),
                )
            missing_by_font.append(f"{family}:{len(missing)}")
    detail = ", ".join(missing_by_font[:4]) or "no CJK font installed"
    raise MediaContractError(f"No installed brand-safe font covers every requested character ({detail})")


def _font_for_poster_role(text: str, role: str) -> FontSelection:
    """Choose a deterministic role-specific face while retaining full-glyph fallback."""

    preferred = _POSTER_FONT_CANDIDATES_BY_ROLE[role]
    candidates = preferred + tuple(
        candidate for candidate in _FONT_CANDIDATES if candidate not in preferred
    )
    return _font_for_text(text, candidates=candidates)


def _font_path() -> str:
    """Compatibility helper for callers that only need a CJK-capable path."""
    return _font_for_text("中文 English 123").path


def validate_overlay_text(text: str | None) -> str | None:
    """Fail before a paid provider call when exact copy cannot be rendered."""
    normalized = normalize_overlay_text(text)
    if not normalized:
        return None
    _font_for_text(normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _wrapped_lines(draw, text: str, font, max_width: int) -> list[str]:
    """Wrap without ever dropping customer copy."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            box = draw.textbbox((0, 0), candidate, font=font)
            if current and box[2] - box[0] > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current)
    return lines


def _text_layout(draw, text: str, selection: FontSelection, width: int, height: int):
    from PIL import ImageFont

    max_width = max(int(width * 0.78), 1)
    max_height = max(int(height * 0.46), 1)
    largest_size = max(24, min(96, int(min(width, height) * 0.075)))
    smallest_size = max(14, int(min(width, height) * 0.022))

    for font_size in range(largest_size, smallest_size - 1, -2):
        font = ImageFont.truetype(selection.path, font_size, index=selection.face_index)
        lines = _wrapped_lines(draw, text, font, max_width)
        spacing = max(5, font_size // 5)
        sample_box = draw.textbbox((0, 0), "国Ag", font=font)
        default_height = max(sample_box[3] - sample_box[1], 1)
        boxes = [draw.textbbox((0, 0), line or " ", font=font) for line in lines]
        widths = [box[2] - box[0] if line else 0 for line, box in zip(lines, boxes, strict=True)]
        heights = [max(box[3] - box[1], default_height) for box in boxes]
        text_width = max(widths, default=0)
        text_height = sum(heights) + spacing * max(len(lines) - 1, 0)
        if text_width <= max_width and text_height <= max_height:
            return font, lines, boxes, widths, heights, spacing, text_width, text_height
    raise MediaContractError(
        "overlay_text cannot fit the safe text area without truncation; shorten the copy or use a larger canvas"
    )


def _render_brand_asset_layer(canvas, asset: ImageAsset, *, position: str, scale: float) -> None:
    from PIL import Image, ImageOps

    normalized_position = position if position in _BRAND_POSITIONS else "center"
    if not 0.1 <= scale <= 0.8:
        raise MediaContractError("brand_scale must be between 0.1 and 0.8")
    with Image.open(BytesIO(asset.raw)) as source:
        product = ImageOps.exif_transpose(source).convert("RGBA")
    width, height = canvas.size
    target_width = max(1, int(width * scale))
    target_height = max(1, int(height * 0.72))
    product.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
    margin_x = max(12, int(width * 0.05))
    margin_y = max(12, int(height * 0.06))
    x = {
        "top_left": margin_x,
        "top_right": width - product.width - margin_x,
        "center": (width - product.width) // 2,
        "bottom_left": margin_x,
        "bottom_right": width - product.width - margin_x,
    }[normalized_position]
    y = {
        "top_left": margin_y,
        "top_right": margin_y,
        "center": (height - product.height) // 2,
        "bottom_left": height - product.height - margin_y,
        "bottom_right": height - product.height - margin_y,
    }[normalized_position]
    canvas.alpha_composite(product, (max(x, 0), max(y, 0)))


def _render_text_layer(canvas, text: str, *, position: str) -> OverlayReceipt:
    from PIL import ImageDraw

    selection = _font_for_text(text)
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = canvas.size
    font, lines, boxes, widths, heights, spacing, text_width, text_height = _text_layout(
        draw,
        text,
        selection,
        width,
        height,
    )
    font_size = int(getattr(font, "size", 24))
    pad_x = max(18, font_size // 2)
    pad_y = max(14, font_size // 3)
    x = (width - text_width) // 2
    normalized_position = position if position in _TEXT_POSITIONS else "bottom"
    y = {
        "top": int(height * 0.08),
        "center": (height - text_height) // 2,
        "bottom": height - text_height - int(height * 0.09),
    }[normalized_position]
    rect = (
        max(0, x - pad_x),
        max(0, y - pad_y),
        min(width, x + text_width + pad_x),
        min(height, y + text_height + pad_y),
    )
    draw.rounded_rectangle(
        rect,
        radius=max(12, font_size // 3),
        fill=(0, 0, 0, 165),
    )
    cursor_y = y
    for line, box, line_width, line_height in zip(lines, boxes, widths, heights, strict=True):
        if line:
            draw.text(
                ((width - line_width) // 2 - box[0], cursor_y - box[1]),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=max(1, font_size // 28),
                stroke_fill=(0, 0, 0, 225),
            )
        cursor_y += line_height + spacing
    return OverlayReceipt(
        rendered_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        font_sha256=selection.sha256,
        font_family=selection.family,
        font_face_index=selection.face_index,
        line_count=len(lines),
    )


def _poster_block_layout(
    draw,
    text: str,
    selection: FontSelection,
    role: str,
    width: int,
    height: int,
):
    from PIL import ImageFont

    short_edge = min(width, height)
    role_scale = {
        "title": 0.068,
        "subtitle": 0.034,
        "tagline": 0.027,
        "body": 0.024,
        "cta": 0.032,
    }[role]
    max_width_ratio = 0.82 if role != "cta" else 0.42
    max_width = max(1, int(width * max_width_ratio))
    largest_size = max(18, min(280, int(short_edge * role_scale)))
    smallest_size = max(13, int(short_edge * 0.021))
    for font_size in range(largest_size, smallest_size - 1, -1):
        font = ImageFont.truetype(selection.path, font_size, index=selection.face_index)
        lines = _wrapped_lines(draw, text, font, max_width)
        boxes = [draw.textbbox((0, 0), line or " ", font=font) for line in lines]
        widths = [box[2] - box[0] if line else 0 for line, box in zip(lines, boxes, strict=True)]
        sample = draw.textbbox((0, 0), "国Ag", font=font)
        default_height = max(sample[3] - sample[1], 1)
        heights = [max(box[3] - box[1], default_height) for box in boxes]
        spacing = max(3, font_size // 5)
        if (
            max(widths, default=0) <= max_width
            and _poster_lines_are_commercially_balanced(
                text,
                role=role,
                lines=lines,
                widths=widths,
            )
        ):
            return font, lines, boxes, widths, heights, spacing
    raise MediaContractError(
        f"overlay_blocks {role} copy cannot fit without truncation"
    )


def _poster_lines_are_commercially_balanced(
    text: str,
    *,
    role: str,
    lines: list[str],
    widths: list[int],
) -> bool:
    """Reject accidental title widows while preserving explicit line breaks."""

    visible = [
        (line.strip(), width)
        for line, width in zip(lines, widths, strict=True)
        if line.strip()
    ]
    if not visible:
        return False
    if "\n" in text:
        return True
    maximum_lines = {
        "title": 2,
        "subtitle": 2,
        "tagline": 3,
        "body": 3,
        "cta": 2,
    }[role]
    if len(visible) > maximum_lines:
        return False
    if len(visible) == 1:
        return True

    shortest_text, shortest_width = min(
        visible,
        key=lambda item: (item[1], len(item[0])),
    )
    longest_width = max(width for _line, width in visible)
    display_characters = sum(
        1 for character in shortest_text if not character.isspace()
    )
    return (
        display_characters >= 2
        and shortest_width >= max(1, int(longest_width * 0.34))
    )


def _composite_glass_cta(canvas, rect: tuple[int, int, int, int], radius: int) -> None:
    """Composite a restrained rose-to-violet glass button with depth and edge light."""

    from PIL import Image, ImageDraw, ImageFilter

    left, top, right, bottom = rect
    button_width = max(1, right - left)
    button_height = max(1, bottom - top)

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
    shadow_offset = max(2, button_height // 12)
    shadow_draw.rounded_rectangle(
        (left, top + shadow_offset, right, bottom + shadow_offset),
        radius=radius,
        fill=(50, 31, 108, 78),
    )
    canvas.alpha_composite(
        shadow.filter(ImageFilter.GaussianBlur(max(4, button_height // 6)))
    )

    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    glow_draw.rounded_rectangle(
        rect,
        radius=radius,
        fill=(217, 104, 255, 92),
    )
    canvas.alpha_composite(
        glow.filter(ImageFilter.GaussianBlur(max(5, button_height // 4)))
    )

    mask = Image.new("L", (button_width, button_height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, button_width - 1, button_height - 1),
        radius=radius,
        fill=218,
    )
    gradient = Image.new("RGBA", (button_width, button_height), (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(gradient, "RGBA")
    start = (232, 111, 200)
    end = (115, 124, 246)
    denominator = max(button_width - 1, 1)
    for column in range(button_width):
        mix = column / denominator
        color = tuple(
            round(start[channel] * (1 - mix) + end[channel] * mix)
            for channel in range(3)
        )
        gradient_draw.line(
            (column, 0, column, button_height - 1),
            fill=(*color, 226),
        )
    gradient.putalpha(mask)
    canvas.alpha_composite(gradient, (left, top))

    edge = ImageDraw.Draw(canvas, "RGBA")
    inset = max(2, button_height // 24)
    edge.rounded_rectangle(
        (left + inset, top + inset, right - inset, bottom - inset),
        radius=max(1, radius - inset),
        outline=(255, 239, 255, 188),
        width=max(1, button_height // 38),
    )
    edge.arc(
        (left + inset * 2, top + inset * 2, right - inset * 2, bottom - inset * 2),
        190,
        344,
        fill=(255, 255, 255, 102),
        width=max(1, button_height // 50),
    )


def _render_poster_blocks(
    canvas,
    blocks: tuple[dict[str, str], ...],
    *,
    dry_run: bool = False,
) -> OverlayReceipt:
    """Render and receipt one role-aware hierarchy inside a verified safe area."""

    from PIL import Image, ImageDraw, ImageFilter

    all_text = "\n".join(block["text"] for block in blocks)
    texts_by_role: dict[str, list[str]] = {}
    for block in blocks:
        texts_by_role.setdefault(block["role"], []).append(block["text"])
    selections = {
        role: _font_for_poster_role("\n".join(texts), role)
        for role, texts in texts_by_role.items()
    }
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas, "RGBA")
    safe_margin_x = max(18, int(width * 0.06))
    safe_margin_y = max(18, int(height * 0.06))
    content_rects: list[tuple[int, int, int, int]] = []

    def record_content_bounds(rect: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = rect
        if (
            left < safe_margin_x
            or top < safe_margin_y
            or right > width - safe_margin_x
            or bottom > height - safe_margin_y
            or left >= right
            or top >= bottom
        ):
            raise MediaContractError(
                "overlay_blocks layout cannot fit inside the poster safe area"
            )
        content_rects.append(rect)

    first_cta_index = next(
        (index for index, block in enumerate(blocks) if block["role"] == "cta"),
        len(blocks),
    )
    # Copy following a CTA is conventionally legal/brand attribution. Keep it
    # out of the primary hierarchy and render it as a restrained footer rather
    # than turning the company name into another headline-sized body row.
    footer_blocks = [
        block
        for index, block in enumerate(blocks)
        if index > first_cta_index and block["role"] == "body"
    ]
    footer_ids = {id(block) for block in footer_blocks}
    non_cta = [
        block
        for block in blocks
        if block["role"] != "cta" and id(block) not in footer_ids
    ]
    ctas = [block for block in blocks if block["role"] == "cta"]
    layouts = [
        (
            block,
            _poster_block_layout(
                draw,
                block["text"],
                selections[block["role"]],
                block["role"],
                width,
                height,
            ),
        )
        for block in non_cta
    ]
    block_gap = max(10, int(height * 0.014))
    layout_heights = [
        sum(layout[4]) + layout[5] * max(len(layout[1]) - 1, 0)
        for _block, layout in layouts
    ]
    group_height = sum(layout_heights) + block_gap * max(len(layouts) - 1, 0)
    cursor_y = max(int(height * 0.31), (height - group_height) // 2)
    if layouts and (
        cursor_y < safe_margin_y
        or cursor_y + group_height > height - safe_margin_y
    ):
        raise MediaContractError(
            "overlay_blocks layout cannot fit inside the poster safe area"
        )
    total_lines = 0

    for (block, layout), block_height in zip(layouts, layout_heights, strict=True):
        font, lines, boxes, widths, heights, spacing = layout
        font_size = int(font.size)
        fill = {
            "title": (255, 255, 255, 255),
            "subtitle": (252, 250, 255, 245),
            "tagline": (242, 235, 255, 245),
            "body": (242, 238, 255, 240),
        }[block["role"]]
        line_y = cursor_y
        for line, box, line_width, line_height in zip(
            lines,
            boxes,
            widths,
            heights,
            strict=True,
        ):
            if line:
                x = (width - line_width) // 2 - box[0]
                record_content_bounds(
                    (
                        x + box[0],
                        line_y,
                        x + box[0] + line_width,
                        line_y + line_height,
                    )
                )
                if not dry_run and block["role"] == "title":
                    outer_glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                    outer_glow_draw = ImageDraw.Draw(outer_glow, "RGBA")
                    outer_glow_draw.text(
                        (x, line_y - box[1]),
                        line,
                        font=font,
                        fill=(219, 179, 255, 108),
                        stroke_width=max(1, font_size // 32),
                        stroke_fill=(166, 120, 255, 86),
                    )
                    canvas.alpha_composite(
                        outer_glow.filter(
                            ImageFilter.GaussianBlur(max(4, font_size // 10))
                        )
                    )
                elif not dry_run:
                    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
                    shadow_draw.text(
                        (x, line_y - box[1] + max(1, font_size // 24)),
                        line,
                        font=font,
                        fill=(58, 42, 112, 104),
                    )
                    canvas.alpha_composite(
                        shadow.filter(
                            ImageFilter.GaussianBlur(max(2, font_size // 18))
                        )
                    )
                if not dry_run:
                    draw = ImageDraw.Draw(canvas, "RGBA")
                    draw.text(
                        (x, line_y - box[1]),
                        line,
                        font=font,
                        fill=fill,
                        stroke_width=(
                            max(1, font_size // 40)
                            if block["role"] == "title"
                            else max(1, font_size // 70)
                        ),
                        stroke_fill=(
                            (246, 226, 255, 178)
                            if block["role"] == "title"
                            else (83, 59, 135, 88)
                        ),
                    )
            line_y += line_height + spacing
            total_lines += 1
        cursor_y += block_height + block_gap

    cta_y = max(cursor_y + block_gap, int(height * 0.62))
    for block in ctas:
        font, lines, boxes, widths, heights, spacing = _poster_block_layout(
            draw,
            block["text"],
            selections["cta"],
            "cta",
            width,
            height,
        )
        text_width = max(widths, default=0)
        text_height = sum(heights) + spacing * max(len(lines) - 1, 0)
        pad_x = max(24, int(font.size * 0.9))
        pad_y = max(12, int(font.size * 0.42))
        button_width = text_width + pad_x * 2
        button_height = text_height + pad_y * 2
        x = width - button_width - max(safe_margin_x, int(width * 0.08))
        y = cta_y
        rect = (x, y, x + button_width, y + button_height)
        record_content_bounds(rect)
        if not dry_run:
            _composite_glass_cta(canvas, rect, button_height // 2)
        draw = ImageDraw.Draw(canvas, "RGBA")
        line_y = y + pad_y
        for line, box, line_width, line_height in zip(
            lines,
            boxes,
            widths,
            heights,
            strict=True,
        ):
            if not dry_run:
                draw.text(
                    (x + (button_width - line_width) // 2 - box[0], line_y - box[1]),
                    line,
                    font=font,
                    fill=(255, 255, 255, 255),
                    stroke_width=max(1, int(font.size) // 45),
                    stroke_fill=(91, 57, 145, 125),
                )
            line_y += line_height + spacing
            total_lines += 1
        cta_y = y + button_height + block_gap

    footer_y = max(cta_y + block_gap, int(height * 0.88))
    for block in footer_blocks:
        font, lines, boxes, widths, heights, spacing = _poster_block_layout(
            draw,
            block["text"],
            selections["body"],
            "body",
            width,
            height,
        )
        footer_height = sum(heights) + spacing * max(len(lines) - 1, 0)
        if footer_y + footer_height > height - safe_margin_y:
            raise MediaContractError(
                "overlay_blocks footer cannot fit inside the poster safe area"
            )
        line_y = footer_y
        for line, box, line_width, line_height in zip(
            lines,
            boxes,
            widths,
            heights,
            strict=True,
        ):
            if line:
                x = (width - line_width) // 2 - box[0]
                record_content_bounds(
                    (
                        x + box[0],
                        line_y,
                        x + box[0] + line_width,
                        line_y + line_height,
                    )
                )
                if not dry_run:
                    draw.text(
                        (x, line_y - box[1]),
                        line,
                        font=font,
                        fill=(232, 229, 247, 205),
                        stroke_width=max(1, int(font.size) // 60),
                        stroke_fill=(50, 39, 91, 105),
                    )
            line_y += line_height + spacing
            total_lines += 1
        footer_y += footer_height + block_gap

    if not content_rects:
        raise MediaContractError("overlay_blocks produced no visible poster content")
    font_roles = {
        role: {
            "family": selection.family,
            "face_index": selection.face_index,
            "sha256": selection.sha256,
        }
        for role, selection in sorted(selections.items())
    }
    primary_role = "title" if "title" in selections else sorted(selections)[0]
    primary_selection = selections[primary_role]
    return OverlayReceipt(
        rendered_text_sha256=hashlib.sha256(all_text.encode("utf-8")).hexdigest(),
        font_sha256=primary_selection.sha256,
        font_family=primary_selection.family,
        font_face_index=primary_selection.face_index,
        font_roles=font_roles,
        line_count=total_lines,
        layout_version="poster-v3",
        block_count=len(blocks),
        overlay_blocks_sha256=overlay_blocks_sha256(blocks),
        layout_bounds_verified=True,
        content_left=min(rect[0] for rect in content_rects),
        content_top=min(rect[1] for rect in content_rects),
        content_right=max(rect[2] for rect in content_rects),
        content_bottom=max(rect[3] for rect in content_rects),
        safe_margin_x=safe_margin_x,
        safe_margin_y=safe_margin_y,
    )


def preflight_poster_layout(
    blocks: object,
    *,
    aspect_ratio: str,
) -> OverlayReceipt:
    """Run the exact production font/wrap/bounds plan without provider spend."""

    from PIL import Image

    normalized_blocks = normalize_overlay_blocks(blocks)
    if not normalized_blocks:
        return OverlayReceipt()
    dimensions = {
        "1:1": (1080, 1080),
        "3:4": (1080, 1440),
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
    }.get(str(aspect_ratio or "").strip())
    if dimensions is None:
        raise MediaContractError(
            "Poster aspect ratio has no deterministic layout canvas"
        )
    canvas = Image.new("RGBA", dimensions, (0, 0, 0, 0))
    return _render_poster_blocks(canvas, normalized_blocks, dry_run=True)


def _compose_overlay_canvas(
    size: tuple[int, int],
    text: str | None,
    *,
    overlay_blocks: tuple[dict[str, str], ...] = (),
    text_position: str,
    brand_asset: ImageAsset | None,
    brand_position: str,
    brand_scale: float,
):
    from PIL import Image

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    receipt = OverlayReceipt()
    if brand_asset:
        _render_brand_asset_layer(
            canvas,
            brand_asset,
            position=brand_position,
            scale=brand_scale,
        )
        receipt = OverlayReceipt(brand_asset_sha256=brand_asset.sha256)
    normalized_text = normalize_overlay_text(text)
    if normalized_text and overlay_blocks:
        raise MediaContractError("Use overlay_text or overlay_blocks, not both")
    if overlay_blocks:
        text_receipt = _render_poster_blocks(canvas, overlay_blocks)
        receipt = replace(
            text_receipt,
            brand_asset_sha256=receipt.brand_asset_sha256,
        )
    if normalized_text:
        text_receipt = _render_text_layer(canvas, normalized_text, position=text_position)
        receipt = OverlayReceipt(
            rendered_text_sha256=text_receipt.rendered_text_sha256,
            brand_asset_sha256=receipt.brand_asset_sha256,
            font_sha256=text_receipt.font_sha256,
            font_family=text_receipt.font_family,
            font_face_index=text_receipt.font_face_index,
            font_roles=text_receipt.font_roles,
            line_count=text_receipt.line_count,
        )
    return canvas, receipt


def _encode_bounded_image(
    image,
    normalized_format: str,
    *,
    max_bytes: int = MAX_REFERENCE_IMAGE_BYTES,
) -> tuple[bytes, tuple[int, int]]:
    """Encode below the delivery limit, downscaling only when compression is insufficient."""

    from PIL import Image

    alpha_extrema = image.getchannel("A").getextrema()
    if normalized_format == "PNG" and alpha_extrema[0] < 255:
        # Retain meaningful transparency without forcing every opaque PNG
        # through a larger four-channel encoding.
        working = image.convert("RGBA")
    else:
        flattened = Image.new("RGB", image.size, (255, 255, 255))
        if "A" in image.getbands():
            flattened.paste(image, mask=image.getchannel("A"))
        else:
            flattened.paste(image)
        working = flattened
    for _attempt in range(12):
        quality_values = (95, 90, 85, 80) if normalized_format == "JPEG" else (None,)
        last_size = 0
        for quality in quality_values:
            output = BytesIO()
            save_kwargs: dict[str, object] = {"optimize": True}
            if quality is not None:
                save_kwargs["quality"] = quality
            working.save(output, format=normalized_format, **save_kwargs)
            result = output.getvalue()
            last_size = len(result)
            if last_size < max_bytes:
                return result, working.size
        ratio = max_bytes / max(last_size, 1)
        scale = min(0.95, max(0.72, ratio**0.5 * 0.97))
        next_size = (
            max(1, int(working.width * scale)),
            max(1, int(working.height * scale)),
        )
        if next_size == working.size:
            break
        working = working.resize(next_size, Image.Resampling.LANCZOS)
    raise MediaContractError(
        f"Generated image could not be encoded below {max_bytes} bytes"
    )


def _brand_safe_background_blur(size: tuple[int, int]) -> float:
    """Scale a text-suppressing blur without erasing the background motion."""
    return round(max(8.0, min(24.0, min(size) * 0.02)), 2)


def apply_image_brand_overlays(
    raw: bytes,
    text: str | None,
    *,
    overlay_blocks: object = None,
    text_position: str = "bottom",
    brand_asset: ImageAsset | None = None,
    brand_position: str = "center",
    brand_scale: float = 0.42,
    output_format: str | None = None,
    sanitize_generated_background: bool = False,
) -> tuple[bytes, OverlayReceipt]:
    """Render exact copy and an immutable product/logo layer on one image."""
    normalized_text = normalize_overlay_text(text)
    normalized_blocks = normalize_overlay_blocks(overlay_blocks)
    if normalized_text and normalized_blocks:
        raise MediaContractError("Use overlay_text or overlay_blocks, not both")
    if not normalized_text and not normalized_blocks and not brand_asset and not output_format:
        return raw, OverlayReceipt()

    from PIL import Image, ImageFilter, ImageOps

    validate_generated_image(raw)
    with Image.open(BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
    if sanitize_generated_background:
        image = image.filter(
            ImageFilter.GaussianBlur(radius=_brand_safe_background_blur(image.size))
        )
    overlay, receipt = _compose_overlay_canvas(
        image.size,
        normalized_text,
        overlay_blocks=normalized_blocks,
        text_position=text_position,
        brand_asset=brand_asset,
        brand_position=brand_position,
        brand_scale=brand_scale,
    )
    image.alpha_composite(overlay)
    if sanitize_generated_background:
        receipt = replace(receipt, background_sanitized=True)

    requested = str(output_format or "PNG").strip().upper().lstrip(".")
    if requested == "WEBP":
        # The supported production contract must not depend on an optional
        # Pillow/libwebp encoder that is absent from some release runtimes.
        raise MediaContractError("WebP output encoding is unavailable; use PNG or JPEG")
    normalized_format = {"JPG": "JPEG", "JPEG": "JPEG"}.get(requested, "PNG")
    source_width, source_height = image.size
    result, output_size = _encode_bounded_image(image, normalized_format)
    receipt = replace(
        receipt,
        source_width=source_width,
        source_height=source_height,
        output_width=output_size[0],
        output_height=output_size[1],
        output_bytes=len(result),
        size_adjusted=output_size != (source_width, source_height),
    )
    validate_generated_image(result)
    return result, receipt


def apply_image_text_overlay(
    raw: bytes,
    text: str | None,
    *,
    position: str = "bottom",
    output_format: str | None = None,
) -> bytes:
    """Backward-compatible exact-copy wrapper."""
    result, _receipt = apply_image_brand_overlays(
        raw,
        text,
        text_position=position,
        output_format=output_format,
    )
    return result


def valid_mp4(raw: bytes) -> bool:
    return len(raw) >= 12 and b"ftyp" in raw[:64]


async def _run_process(*args: str, timeout: int, label: str) -> tuple[bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaContractError(f"{args[0]} is not installed for {label}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise MediaContractError(f"{label} timed out") from exc
    except asyncio.CancelledError:
        # A cancelled request must not leave ffmpeg/ffprobe running in the
        # background. Reap the child before propagating cancellation.
        if process.returncode is None:
            process.kill()
        cleanup = asyncio.create_task(process.communicate())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A second cancellation must not orphan the child process.
            await cleanup
        raise
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise MediaContractError(f"{label} failed: {detail}")
    return stdout, stderr


def _browser_safe_video(info: VideoInfo) -> bool:
    return (
        info.codec_name == "h264"
        and info.pixel_format in {"yuv420p", "yuvj420p"}
        and info.audio_codec_name in {None, "aac", "mp3"}
        and info.fast_start
    )


async def validate_generated_video(
    raw: bytes,
    *,
    label: str = "Generated video",
    require_browser_safe: bool = True,
) -> VideoInfo:
    """Probe a real MP4 and, by default, enforce the browser delivery codec."""
    if not valid_mp4(raw):
        raise MediaContractError(f"{label} is not a valid MP4 payload")
    with tempfile.TemporaryDirectory(prefix="astra-media-probe-") as temp_dir:
        input_path = Path(temp_dir) / "input.mp4"
        input_path.write_bytes(raw)
        stdout, _stderr = await _run_process(
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(input_path),
            timeout=45,
            label="Video validation",
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        stream = next(item for item in payload.get("streams", []) if item.get("codec_type") == "video")
        audio_stream = next(
            (
                item
                for item in payload.get("streams", [])
                if item.get("codec_type") == "audio"
            ),
            None,
        )
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0)
        codec_name = str(stream.get("codec_name") or "")
        pixel_format = str(stream.get("pix_fmt") or "")
        audio_codec_name = (
            str(audio_stream.get("codec_name") or "") or None
            if audio_stream
            else None
        )
    except Exception as exc:
        raise MediaContractError(f"{label} has no readable video stream") from exc
    if width <= 0 or height <= 0 or duration <= 0 or not codec_name:
        raise MediaContractError(f"{label} has invalid video dimensions, duration, or codec")
    info = VideoInfo(
        width=width,
        height=height,
        duration_seconds=duration,
        codec_name=codec_name,
        pixel_format=pixel_format,
        audio_codec_name=audio_codec_name,
        fast_start=(
            raw.find(b"moov") >= 0
            and (raw.find(b"mdat") < 0 or raw.find(b"moov") < raw.find(b"mdat"))
        ),
    )
    if require_browser_safe and not _browser_safe_video(info):
        raise MediaContractError(
            f"{label} is not browser-safe H.264/yuv420p with AAC-compatible audio and faststart"
        )
    return info


def validate_video_delivery_contract(
    info: VideoInfo,
    *,
    expected_duration_seconds: int | float | None = None,
    expected_aspect_ratio: str | None = None,
    require_audio: bool = False,
) -> VideoInfo:
    """Enforce user-visible duration, orientation, and audio requirements."""

    failures: list[str] = []
    if expected_duration_seconds is not None:
        expected_duration = float(expected_duration_seconds)
        duration_tolerance = max(0.5, expected_duration * 0.05)
        if abs(info.duration_seconds - expected_duration) > duration_tolerance:
            failures.append(
                f"duration is {info.duration_seconds:.3f}s, expected "
                f"{expected_duration:.3f}s ± {duration_tolerance:.3f}s"
            )

    ratio_targets = {
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "1:1": 1.0,
        "4:3": 4 / 3,
        "3:4": 3 / 4,
        "21:9": 21 / 9,
    }
    normalized_ratio = str(expected_aspect_ratio or "").strip()
    target_ratio = ratio_targets.get(normalized_ratio)
    if target_ratio is not None:
        actual_ratio = info.width / info.height
        if abs(actual_ratio - target_ratio) / target_ratio > 0.03:
            failures.append(
                f"aspect ratio is {info.width}:{info.height}, expected "
                f"{normalized_ratio}"
            )

    if require_audio and info.audio_codec_name is None:
        failures.append("audio stream is required but missing")

    if failures:
        raise MediaContractError(
            "Video delivery contract invalid: " + "; ".join(failures)
        )
    return info


async def validate_uploaded_video(
    raw: bytes,
    *,
    extension: str,
    label: str = "Uploaded video",
) -> VideoInfo:
    """Probe a chat video and require its bytes to match the claimed container."""
    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    expected_containers = {
        "mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        "mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        "avi": {"avi"},
        "mkv": {"matroska", "webm"},
    }
    if normalized_extension not in expected_containers:
        raise MediaContractError(f"{label} format is unsupported")
    if not raw:
        raise MediaContractError(f"{label} is empty")

    with tempfile.TemporaryDirectory(prefix="astra-media-upload-") as temp_dir:
        input_path = Path(temp_dir) / f"input.{normalized_extension}"
        input_path.write_bytes(raw)
        stdout, _stderr = await _run_process(
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(input_path),
            timeout=45,
            label="Uploaded video validation",
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        stream = next(item for item in payload.get("streams", []) if item.get("codec_type") == "video")
        audio_stream = next(
            (item for item in payload.get("streams", []) if item.get("codec_type") == "audio"),
            None,
        )
        format_names = {
            value.strip().lower()
            for value in str((payload.get("format") or {}).get("format_name") or "").split(",")
            if value.strip()
        }
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0)
        codec_name = str(stream.get("codec_name") or "")
        pixel_format = str(stream.get("pix_fmt") or "")
        audio_codec_name = (
            str(audio_stream.get("codec_name") or "") or None
            if audio_stream
            else None
        )
    except Exception as exc:
        raise MediaContractError(f"{label} has no readable video stream") from exc
    if not (format_names & expected_containers[normalized_extension]):
        raise MediaContractError(
            f"{label} bytes do not match the .{normalized_extension} container"
        )
    if width <= 0 or height <= 0 or duration <= 0 or not codec_name:
        raise MediaContractError(f"{label} has invalid dimensions, duration, or codec")
    return VideoInfo(
        width=width,
        height=height,
        duration_seconds=duration,
        codec_name=codec_name,
        pixel_format=pixel_format,
        audio_codec_name=audio_codec_name,
        fast_start=(
            normalized_extension == "mp4"
            and raw.find(b"moov") >= 0
            and (raw.find(b"mdat") < 0 or raw.find(b"moov") < raw.find(b"mdat"))
        ),
    )


async def _transcode_browser_safe_video(raw: bytes, *, label: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="astra-media-browser-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "input.mp4"
        output_path = root / "output.mp4"
        input_path.write_bytes(raw)
        await _run_process(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
            timeout=300,
            label=label,
        )
        if not output_path.is_file():
            raise MediaContractError(f"{label} did not create an output file")
        result = output_path.read_bytes()
    await validate_generated_video(result, label=label)
    return result


async def validate_generated_audio(
    raw: bytes,
    *,
    audio_format: str,
    sample_rate: int | None = None,
    label: str = "Generated audio",
) -> AudioInfo:
    """Reject non-audio provider bytes before storage and successful delivery."""
    normalized_format = str(audio_format or "").strip().lower().lstrip(".")
    if normalized_format not in {"mp3", "wav", "flac", "pcm"}:
        raise MediaContractError(f"{label} format is unsupported")
    if not raw:
        raise MediaContractError(f"{label} is empty")

    if normalized_format == "pcm":
        rate = int(sample_rate or 0)
        if rate <= 0:
            raise MediaContractError(f"{label} PCM sample rate is invalid")
        # MiniMax PCM output is signed 16-bit mono.  Require complete samples
        # and at least 50 ms so arbitrary non-empty bytes cannot be delivered.
        minimum_bytes = max((rate * 2) // 20, 2)
        if len(raw) < minimum_bytes or len(raw) % 2:
            raise MediaContractError(f"{label} is not valid 16-bit PCM audio")
        return AudioInfo(
            duration_seconds=len(raw) / (rate * 2),
            codec_name="pcm_s16le",
            sample_rate=rate,
            channels=1,
            container_format="pcm",
        )

    with tempfile.TemporaryDirectory(prefix="astra-media-audio-probe-") as temp_dir:
        input_path = Path(temp_dir) / f"input.{normalized_format}"
        input_path.write_bytes(raw)
        stdout, _stderr = await _run_process(
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(input_path),
            timeout=45,
            label="Audio validation",
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        stream = next(item for item in payload.get("streams", []) if item.get("codec_type") == "audio")
        duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0)
        codec_name = str(stream.get("codec_name") or "")
        detected_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
        container_format = str((payload.get("format") or {}).get("format_name") or "")
    except Exception as exc:
        raise MediaContractError(f"{label} has no readable audio stream") from exc
    if duration <= 0 or not codec_name or detected_rate <= 0 or channels <= 0:
        raise MediaContractError(
            f"{label} has invalid duration, codec, sample rate, or channel count"
        )
    container_names = {
        value.strip().lower()
        for value in container_format.split(",")
        if value.strip()
    }
    format_matches = {
        "mp3": codec_name.startswith("mp3") and "mp3" in container_names,
        "wav": codec_name.startswith("pcm_") and "wav" in container_names,
        "flac": codec_name == "flac" and "flac" in container_names,
    }
    if not format_matches[normalized_format]:
        raise MediaContractError(
            f"{label} does not match requested {normalized_format} container and codec"
        )
    return AudioInfo(
        duration_seconds=duration,
        codec_name=codec_name,
        sample_rate=detected_rate,
        channels=channels,
        container_format=container_format,
    )


async def trim_generated_audio(
    raw: bytes,
    *,
    audio_format: str,
    duration_seconds: float,
    label: str = "Generated audio",
) -> tuple[bytes, AudioInfo]:
    """Trim provider audio to the customer-visible duration contract.

    Music providers may return a full song even when the customer needs a
    short commercial clip.  The provider response remains the durable recovery
    source; this deterministic post-processing step produces the exact
    customer-facing artifact before Credits settlement.
    """

    normalized_format = str(audio_format or "").strip().lower().lstrip(".")
    if normalized_format not in {"mp3", "wav"}:
        raise MediaContractError(f"{label} trim format is unsupported")
    requested_duration = float(duration_seconds)
    if not 0 < requested_duration <= 180:
        raise MediaContractError(f"{label} duration must be between 0 and 180 seconds")

    source_info = await validate_generated_audio(
        raw,
        audio_format=normalized_format,
        label=label,
    )
    tolerance = 0.15
    if source_info.duration_seconds + tolerance < requested_duration:
        raise MediaContractError(
            f"{label} is {source_info.duration_seconds:.3f}s, shorter than the "
            f"requested {requested_duration:.3f}s"
        )
    if source_info.duration_seconds <= requested_duration + tolerance:
        return raw, source_info

    with tempfile.TemporaryDirectory(prefix="astra-media-audio-trim-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / f"input.{normalized_format}"
        output_path = root / f"output.{normalized_format}"
        input_path.write_bytes(raw)
        codec_args = (
            ("-c:a", "libmp3lame", "-b:a", "256k")
            if normalized_format == "mp3"
            else ("-c:a", "pcm_s16le")
        )
        await _run_process(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-t",
            f"{requested_duration:.3f}",
            "-vn",
            *codec_args,
            str(output_path),
            timeout=120,
            label=f"{label} duration trim",
        )
        if not output_path.is_file():
            raise MediaContractError(f"{label} duration trim did not create an output file")
        result = output_path.read_bytes()

    output_info = await validate_generated_audio(
        result,
        audio_format=normalized_format,
        label=f"{label} trimmed output",
    )
    if abs(output_info.duration_seconds - requested_duration) > tolerance:
        raise MediaContractError(
            f"{label} trimmed duration is {output_info.duration_seconds:.3f}s, "
            f"expected {requested_duration:.3f}s"
        )
    return result, output_info


async def compose_video_audio_tracks(
    video_raw: bytes,
    *,
    voiceover_raw: bytes | None = None,
    voiceover_format: str | None = None,
    music_raw: bytes | None = None,
    music_format: str | None = None,
    voiceover_start_seconds: float = 0.0,
    voiceover_gain: float = 1.0,
    music_gain: float = 0.16,
    keep_source_audio: bool = False,
) -> tuple[bytes, AudioMixReceipt]:
    """Create one browser-safe MP4 from validated video and audio tracks.

    This is deterministic post-production, not another generative provider
    request.  A quiet music bed and an independently generated voice track let
    a silent image-to-video provider participate in a complete ad workflow
    without pretending that the provider generated synchronized audio.
    """

    if voiceover_raw is None and music_raw is None:
        raise MediaContractError("At least one voiceover or music track is required")
    video_info = await validate_generated_video(
        video_raw,
        label="Audio mix video input",
        require_browser_safe=False,
    )
    start_seconds = float(voiceover_start_seconds)
    if start_seconds < 0 or start_seconds >= video_info.duration_seconds:
        raise MediaContractError(
            "voiceover_start_seconds must fall within the video duration"
        )
    normalized_voice_gain = float(voiceover_gain)
    normalized_music_gain = float(music_gain)
    if not 0 < normalized_voice_gain <= 4:
        raise MediaContractError("voiceover_gain must be greater than 0 and at most 4")
    if not 0 < normalized_music_gain <= 1:
        raise MediaContractError("music_gain must be greater than 0 and at most 1")

    normalized_voice_format = str(voiceover_format or "").strip().lower().lstrip(".")
    normalized_music_format = str(music_format or "").strip().lower().lstrip(".")
    if voiceover_raw is not None:
        await validate_generated_audio(
            voiceover_raw,
            audio_format=normalized_voice_format,
            label="Voiceover input",
        )
    if music_raw is not None:
        await validate_generated_audio(
            music_raw,
            audio_format=normalized_music_format,
            label="Music input",
        )

    with tempfile.TemporaryDirectory(prefix="astra-video-audio-mix-") as temp_dir:
        root = Path(temp_dir)
        video_path = root / "video.mp4"
        output_path = root / "output.mp4"
        video_path.write_bytes(video_raw)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
        ]
        audio_inputs: list[tuple[str, int]] = []
        input_index = 1
        if voiceover_raw is not None:
            voice_path = root / f"voiceover.{normalized_voice_format}"
            voice_path.write_bytes(voiceover_raw)
            command.extend(["-i", str(voice_path)])
            audio_inputs.append(("voiceover", input_index))
            input_index += 1
        if music_raw is not None:
            music_path = root / f"music.{normalized_music_format}"
            music_path.write_bytes(music_raw)
            command.extend(["-stream_loop", "-1", "-i", str(music_path)])
            audio_inputs.append(("music", input_index))

        duration = video_info.duration_seconds
        filters: list[str] = []
        mix_labels: list[str] = []
        if keep_source_audio and video_info.audio_codec_name:
            filters.append(
                f"[0:a:0]atrim=duration={duration:.6f},volume=0.55[source_audio]"
            )
            mix_labels.append("[source_audio]")
        for kind, index in audio_inputs:
            if kind == "voiceover":
                delay_ms = round(start_seconds * 1000)
                filters.append(
                    f"[{index}:a:0]adelay={delay_ms}:all=1,apad,"
                    f"atrim=duration={duration:.6f},volume={normalized_voice_gain:.4f}"
                    "[voiceover]"
                )
                mix_labels.append("[voiceover]")
            else:
                filters.append(
                    f"[{index}:a:0]atrim=duration={duration:.6f},"
                    f"asetpts=N/SR/TB,volume={normalized_music_gain:.4f}[music]"
                )
                mix_labels.append("[music]")
        if len(mix_labels) == 1:
            filters.append(f"{mix_labels[0]}anull[aout]")
        else:
            filters.append(
                "".join(mix_labels)
                + f"amix=inputs={len(mix_labels)}:duration=longest:"
                "dropout_transition=2:normalize=0,alimiter=limit=0.95[aout]"
            )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-t",
                f"{duration:.6f}",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        await _run_process(
            *command,
            timeout=300,
            label="Video audio composition",
        )
        if not output_path.is_file():
            raise MediaContractError("Video audio composition did not create an output")
        result = output_path.read_bytes()

    output_info = await validate_generated_video(
        result,
        label="Video audio composition output",
    )
    validate_video_delivery_contract(
        output_info,
        expected_duration_seconds=video_info.duration_seconds,
        require_audio=True,
    )
    return result, AudioMixReceipt(
        voiceover_sha256=(
            hashlib.sha256(voiceover_raw).hexdigest()
            if voiceover_raw is not None
            else None
        ),
        music_sha256=(
            hashlib.sha256(music_raw).hexdigest()
            if music_raw is not None
            else None
        ),
        voiceover_start_seconds=start_seconds,
        voiceover_gain=normalized_voice_gain,
        music_gain=normalized_music_gain,
        source_audio_retained=bool(
            keep_source_audio and video_info.audio_codec_name
        ),
        output_duration_seconds=output_info.duration_seconds,
        output_video_codec=output_info.codec_name,
        output_audio_codec=str(output_info.audio_codec_name or ""),
        output_width=output_info.width,
        output_height=output_info.height,
        browser_safe=(
            output_info.codec_name == "h264"
            and output_info.audio_codec_name == "aac"
            and output_info.pixel_format == "yuv420p"
        ),
        fast_start=output_info.fast_start,
    )


async def apply_video_brand_overlays(
    raw: bytes,
    text: str | None,
    *,
    text_position: str = "bottom",
    brand_asset: ImageAsset | None = None,
    brand_position: str = "center",
    brand_scale: float = 0.42,
    sanitize_generated_background: bool = False,
) -> tuple[bytes, OverlayReceipt]:
    """Composite the same deterministic Pillow layer over every video frame."""
    info = await validate_generated_video(
        raw,
        label="Video overlay input",
        require_browser_safe=False,
    )
    normalized_text = normalize_overlay_text(text)
    if not normalized_text and not brand_asset:
        if _browser_safe_video(info):
            return raw, OverlayReceipt()
        return (
            await _transcode_browser_safe_video(
                raw,
                label="Video browser compatibility transcode",
            ),
            OverlayReceipt(),
        )

    overlay, receipt = _compose_overlay_canvas(
        (info.width, info.height),
        normalized_text,
        text_position=text_position,
        brand_asset=brand_asset,
        brand_position=brand_position,
        brand_scale=brand_scale,
    )
    with tempfile.TemporaryDirectory(prefix="astra-media-overlay-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "input.mp4"
        overlay_path = root / "overlay.png"
        output_path = root / "output.mp4"
        input_path.write_bytes(raw)
        overlay.save(overlay_path, format="PNG", optimize=True)
        if sanitize_generated_background:
            blur = _brand_safe_background_blur((info.width, info.height))
            filter_complex = (
                f"[0:v]gblur=sigma={blur}:steps=2[background];"
                "[background][1:v]overlay=0:0:format=auto:shortest=1[v]"
            )
        else:
            filter_complex = "[0:v][1:v]overlay=0:0:format=auto:shortest=1[v]"
        await _run_process(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-loop",
            "1",
            "-framerate",
            "1",
            "-i",
            str(overlay_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output_path),
            timeout=300,
            label="Video brand-safe overlay",
        )
        if not output_path.is_file():
            raise MediaContractError("Video brand-safe overlay did not create an output file")
        result = output_path.read_bytes()
    await validate_generated_video(result, label="Video overlay output")
    if sanitize_generated_background:
        receipt = replace(receipt, background_sanitized=True)
    return result, receipt


async def apply_video_text_overlay(
    raw: bytes,
    text: str | None,
    *,
    position: str = "bottom",
) -> bytes:
    """Backward-compatible exact-copy wrapper."""
    result, _receipt = await apply_video_brand_overlays(
        raw,
        text,
        text_position=position,
    )
    return result
