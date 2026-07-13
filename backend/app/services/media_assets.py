"""Safe reference-asset transport and deterministic media text rendering."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
import tempfile


MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OVERLAY_TEXT_CHARS = 300
SUPPORTED_REFERENCE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font_path() -> str:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise ValueError("No CJK-capable font is installed for deterministic media text")


def _validate_image_bytes(
    raw: bytes,
    *,
    label: str,
    require_video_dimensions: bool = False,
) -> tuple[str, int, int]:
    if not raw:
        raise ValueError(f"{label} is empty")
    if len(raw) >= MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError(f"{label} must be smaller than 20MB")

    from PIL import Image

    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
    except Exception as exc:
        raise ValueError(f"{label} is not a valid JPG, PNG, or WebP image") from exc

    if image_format not in SUPPORTED_REFERENCE_FORMATS:
        raise ValueError(f"{label} format is not supported; use JPG, PNG, or WebP")
    if width <= 0 or height <= 0:
        raise ValueError(f"{label} has invalid dimensions")
    if require_video_dimensions:
        short_edge = min(width, height)
        aspect_ratio = width / height
        if short_edge <= 300 or not 0.4 <= aspect_ratio <= 2.5:
            raise ValueError(
                f"{label} must have a short edge over 300px and an aspect ratio between 2:5 and 5:2"
            )
    return SUPPORTED_REFERENCE_FORMATS[image_format], width, height


def validate_generated_image(raw: bytes) -> tuple[int, int]:
    """Reject empty/corrupt provider output before storage and settlement."""
    _mime, width, height = _validate_image_bytes(raw, label="Generated image")
    return width, height


def image_reference_for_provider(
    workspace: Path,
    value: str | None,
    *,
    label: str,
    require_video_dimensions: bool = False,
) -> str | None:
    """Return a public URL or a validated Base64 data URL for MiniMax."""
    reference = str(value or "").strip()
    if not reference:
        return None
    if reference.startswith(("https://", "http://")):
        return reference

    if reference.startswith("data:image/"):
        try:
            header, encoded = reference.split(",", 1)
            if ";base64" not in header.lower():
                raise ValueError
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError(f"{label} contains an invalid image data URL") from exc
        mime, _width, _height = _validate_image_bytes(
            raw,
            label=label,
            require_video_dimensions=require_video_dimensions,
        )
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    root = workspace.resolve()
    path = (root / reference.lstrip("/")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside the workspace") from exc
    if not path.is_file():
        raise ValueError(f"{label} was not found in the workspace: {reference}")
    raw = path.read_bytes()
    mime, _width, _height = _validate_image_bytes(
        raw,
        label=label,
        require_video_dimensions=require_video_dimensions,
    )
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _wrapped_lines(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        current = ""
        for character in paragraph:
            candidate = current + character
            box = draw.textbbox((0, 0), candidate, font=font)
            if current and box[2] - box[0] > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current or " ")
    return lines[:6]


def apply_image_text_overlay(
    raw: bytes,
    text: str | None,
    *,
    position: str = "bottom",
    output_format: str | None = None,
) -> bytes:
    """Normalize output and render exact copy with a real CJK-capable font."""
    overlay_text = str(text or "").strip()
    if not overlay_text and not output_format:
        return raw
    if len(overlay_text) > MAX_OVERLAY_TEXT_CHARS:
        raise ValueError(f"overlay_text must be at most {MAX_OVERLAY_TEXT_CHARS} characters")

    from PIL import Image, ImageDraw, ImageFont

    with Image.open(BytesIO(raw)) as source:
        image = source.convert("RGBA")
    width, height = image.size
    if overlay_text:
        font_size = max(24, min(96, int(min(width, height) * 0.065)))
        font = ImageFont.truetype(_font_path(), font_size)
        draw = ImageDraw.Draw(image, "RGBA")
        lines = _wrapped_lines(draw, overlay_text, font, int(width * 0.82))
        spacing = max(6, font_size // 5)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        text_width = max(box[2] - box[0] for box in boxes)
        text_height = sum(box[3] - box[1] for box in boxes) + spacing * (len(lines) - 1)
        pad_x = max(18, font_size // 2)
        pad_y = max(14, font_size // 3)
        x = (width - text_width) // 2
        normalized_position = position if position in {"top", "center", "bottom"} else "bottom"
        y = {
            "top": int(height * 0.08),
            "center": (height - text_height) // 2,
            "bottom": height - text_height - int(height * 0.09),
        }[normalized_position]
        draw.rounded_rectangle(
            (x - pad_x, y - pad_y, x + text_width + pad_x, y + text_height + pad_y),
            radius=max(12, font_size // 3),
            fill=(0, 0, 0, 150),
        )
        cursor_y = y
        for line, box in zip(lines, boxes, strict=True):
            line_width = box[2] - box[0]
            draw.text(
                ((width - line_width) // 2, cursor_y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=max(1, font_size // 28),
                stroke_fill=(0, 0, 0, 220),
            )
            cursor_y += box[3] - box[1] + spacing

    output = BytesIO()
    requested = str(output_format or "PNG").strip().upper().lstrip(".")
    normalized_format = {"JPG": "JPEG", "JPEG": "JPEG", "WEBP": "WEBP"}.get(requested, "PNG")
    save_kwargs = {"optimize": True}
    if normalized_format in {"JPEG", "WEBP"}:
        save_kwargs["quality"] = 95
    image.convert("RGB").save(output, format=normalized_format, **save_kwargs)
    result = output.getvalue()
    validate_generated_image(result)
    return result


def valid_mp4(raw: bytes) -> bool:
    return len(raw) >= 12 and b"ftyp" in raw[:64]


async def apply_video_text_overlay(
    raw: bytes,
    text: str | None,
    *,
    position: str = "bottom",
) -> bytes:
    """Use ffmpeg+Noto to add deterministic text after provider generation."""
    overlay_text = str(text or "").strip()
    if not overlay_text:
        return raw
    if len(overlay_text) > MAX_OVERLAY_TEXT_CHARS:
        raise ValueError(f"overlay_text must be at most {MAX_OVERLAY_TEXT_CHARS} characters")
    if not valid_mp4(raw):
        raise ValueError("Video text overlay input is not a valid MP4")

    normalized_position = position if position in {"top", "center", "bottom"} else "bottom"
    y_expr = {
        "top": "h*0.08",
        "center": "(h-text_h)/2",
        "bottom": "h-text_h-h*0.09",
    }[normalized_position]
    with tempfile.TemporaryDirectory(prefix="astra-media-overlay-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "input.mp4"
        output_path = root / "output.mp4"
        text_path = root / "overlay.txt"
        input_path.write_bytes(raw)
        text_path.write_text(overlay_text, encoding="utf-8")
        drawtext = (
            f"drawtext=fontfile='{_font_path()}':textfile='{text_path}':"
            "fontcolor=white:fontsize=h/18:line_spacing=12:"
            "box=1:boxcolor=black@0.58:boxborderw=24:"
            f"x=(w-text_w)/2:y={y_expr}"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_path),
                "-vf",
                drawtext,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ValueError("ffmpeg is not installed for deterministic video text") from exc
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=240)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ValueError("Video text overlay timed out") from exc
        if process.returncode != 0 or not output_path.is_file():
            detail = (stderr or b"").decode("utf-8", errors="replace")[-600:]
            raise ValueError(f"Video text overlay failed: {detail}")
        result = output_path.read_bytes()
    if not valid_mp4(result):
        raise ValueError("Video text overlay output is not a valid MP4")
    return result
