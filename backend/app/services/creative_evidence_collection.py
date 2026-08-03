"""Local, provider-free evidence collection for creative review panels.

OCR is supporting evidence, not an automatic commercial approval.  Missing
language coverage remains partial so an unrecognized watermark cannot be
mistaken for proof that no watermark exists.
"""

from __future__ import annotations

import csv
from difflib import SequenceMatcher
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Sequence
import unicodedata

from PIL import Image, ImageOps, ImageStat

from app.services.creative_review_panel import CreativeEvidenceReceipt


MAX_OCR_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_VIDEO_OCR_FRAMES = 15
_EXPECTED_SUFFIXES = {
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "video": {".mp4"},
}
_LANGUAGE_TO_TESSERACT = {
    "en": "eng",
    "en-US": "eng",
    "zh": "chi_sim",
    "zh-CN": "chi_sim",
}


class _OcrExecutionError(RuntimeError):
    """A bounded OCR/FFmpeg failure that must not become an API 500."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("OCR artifact must be a regular file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_OCR_ARTIFACT_BYTES:
        raise ValueError("OCR artifact is outside the allowed size range")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _available_tesseract_languages(tesseract_path: str) -> tuple[str, ...]:
    process = subprocess.run(
        [tesseract_path, "--list-langs"],
        check=True,
        capture_output=True,
        errors="replace",
        text=True,
        timeout=15,
    )
    languages = tuple(
        line.strip()
        for line in process.stdout.splitlines()
        if line.strip() and not line.startswith("List of available languages")
    )
    return tuple(sorted(languages))


def _required_tesseract_languages(
    expected_languages: Sequence[str],
) -> tuple[str, ...]:
    required = {
        _LANGUAGE_TO_TESSERACT.get(language, language)
        for language in expected_languages
    }
    return tuple(sorted(required))


def parse_tesseract_tsv(
    payload: str,
    *,
    minimum_confidence: float = 10,
) -> tuple[str, ...]:
    """Return bounded OCR tokens with useful confidence from Tesseract TSV."""

    tokens: list[str] = []
    reader = csv.DictReader(
        io.StringIO(payload),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    )
    for row in reader:
        text = str(row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf") or -1)
        except (TypeError, ValueError):
            confidence = -1
        if text and confidence >= minimum_confidence:
            tokens.append(text[:160])
    return tuple(tokens[:500])


def _run_tesseract(
    path: Path,
    *,
    tesseract_path: str,
    languages: Sequence[str],
    minimum_confidence: float,
) -> tuple[str, ...]:
    command = [tesseract_path, str(path), "stdout"]
    if languages:
        command.extend(["-l", "+".join(languages)])
    command.extend(["--psm", "11", "tsv"])
    try:
        process = subprocess.run(
            command,
            check=True,
            capture_output=True,
            errors="replace",
            text=True,
            timeout=90,
        )
    except subprocess.CalledProcessError as exc:
        raise _OcrExecutionError(f"tesseract_exit_{exc.returncode}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _OcrExecutionError(
            f"tesseract_{type(exc).__name__.lower()}"
        ) from exc
    return parse_tesseract_tsv(
        process.stdout,
        minimum_confidence=minimum_confidence,
    )


def _save_enhanced_ocr_variant(image: Image.Image, path: Path) -> None:
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
    if ImageStat.Stat(grayscale).mean[0] < 110:
        grayscale = ImageOps.invert(grayscale)
    scale = max(2, min(4, 2400 // max(grayscale.width, grayscale.height, 1)))
    grayscale.resize(
        (grayscale.width * scale, grayscale.height * scale),
        Image.Resampling.LANCZOS,
    ).save(path)


def _prepare_ocr_variants(path: Path, *, output_dir: Path) -> tuple[Path, ...]:
    """Create bounded full-frame and corner variants for faint overlay text."""

    with Image.open(path) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if width < 32 or height < 32:
        return (path,)

    variants: list[Path] = [path]
    full_path = output_dir / "full-enhanced.png"
    _save_enhanced_ocr_variant(image, full_path)
    variants.append(full_path)

    crop_width = max(32, int(width * 0.5))
    crop_height = max(32, int(height * 0.4))
    boxes = {
        "top-left": (0, 0, crop_width, crop_height),
        "top-right": (width - crop_width, 0, width, crop_height),
        "bottom-left": (0, height - crop_height, crop_width, height),
        "bottom-right": (
            width - crop_width,
            height - crop_height,
            width,
            height,
        ),
    }
    for name, box in boxes.items():
        variant_path = output_dir / f"{name}-enhanced.png"
        _save_enhanced_ocr_variant(image.crop(box), variant_path)
        variants.append(variant_path)
    return tuple(variants)


def _collect_path_ocr_tokens(
    path: Path,
    *,
    tesseract_path: str,
    languages: Sequence[str],
    minimum_confidence: float,
) -> tuple[tuple[str, ...], int]:
    with tempfile.TemporaryDirectory(prefix="creative-ocr-variants-") as temp_dir:
        variant_paths = _prepare_ocr_variants(
            path,
            output_dir=Path(temp_dir),
        )
        tokens = tuple(
            token
            for variant_path in variant_paths
            for token in _run_tesseract(
                variant_path,
                tesseract_path=tesseract_path,
                languages=languages,
                minimum_confidence=minimum_confidence,
            )
        )
    return tokens, len(variant_paths)


def _canonical_ocr_text(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _possible_prohibited_match(canonical_text: str, term: str) -> bool:
    canonical_term = _canonical_ocr_text(term)
    if len(canonical_term) < 4 or len(canonical_text) < len(canonical_term):
        return False
    window_size = len(canonical_term)
    return any(
        SequenceMatcher(
            None,
            canonical_term,
            canonical_text[index : index + window_size],
        ).ratio()
        >= 0.72
        for index in range(len(canonical_text) - window_size + 1)
    )


def _receipt_findings(
    tokens: Sequence[str],
    *,
    prohibited_terms: Sequence[str],
    scanned_frame_count: int,
    ocr_variant_count: int,
) -> tuple[str, ...]:
    normalized_text = " ".join(tokens)
    canonical_text = _canonical_ocr_text(normalized_text)
    matched_terms = tuple(
        term
        for term in prohibited_terms
        if _canonical_ocr_text(term) in canonical_text
    )
    possible_terms = tuple(
        term
        for term in prohibited_terms
        if term not in matched_terms
        and _possible_prohibited_match(canonical_text, term)
    )
    text_digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    return (
        f"scanned_frame_count={scanned_frame_count}",
        f"ocr_variant_count={ocr_variant_count}",
        f"detected_token_count={len(tokens)}",
        f"detected_text_sha256={text_digest}",
        *tuple(f"prohibited_term_detected={term}" for term in matched_terms),
        *tuple(
            f"prohibited_term_possible_match={term}" for term in possible_terms
        ),
        (
            "ocr_absence_is_not_visual_proof; independent human visual review "
            "remains required"
        ),
    )


def _extract_video_frames(
    path: Path,
    *,
    ffmpeg_path: str,
    output_dir: Path,
) -> tuple[Path, ...]:
    output_pattern = output_dir / "frame-%03d.png"
    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vf",
                "fps=1,scale='min(1280,iw)':-2",
                "-frames:v",
                str(MAX_VIDEO_OCR_FRAMES),
                str(output_pattern),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as exc:
        raise _OcrExecutionError(f"ffmpeg_exit_{exc.returncode}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _OcrExecutionError(
            f"ffmpeg_{type(exc).__name__.lower()}"
        ) from exc
    return tuple(sorted(output_dir.glob("frame-*.png")))


def collect_ocr_evidence(
    *,
    artifact_type: str,
    path: Path,
    expected_languages: Sequence[str],
    prohibited_terms: Sequence[str] = (),
    minimum_confidence: float = 10,
    tesseract_binary: str = "tesseract",
    ffmpeg_binary: str = "ffmpeg",
) -> CreativeEvidenceReceipt:
    """Collect image or sampled-video OCR evidence without approving the asset."""

    if artifact_type not in _EXPECTED_SUFFIXES:
        raise ValueError("artifact_type must be image or video")
    if path.suffix.lower() not in _EXPECTED_SUFFIXES[artifact_type]:
        raise ValueError("Artifact suffix does not match artifact_type")
    artifact_hash = _sha256_file(path)
    tesseract_path = shutil.which(tesseract_binary)
    if tesseract_path is None:
        return CreativeEvidenceReceipt(
            receipt_ref=f"ocr-{artifact_hash[:20]}",
            kind="ocr" if artifact_type == "image" else "frame_ocr",
            status="unavailable",
            artifact_hashes={
                "image" if artifact_type == "image" else "mp4": artifact_hash
            },
            source="tesseract_unavailable",
            findings=("Tesseract executable was not found",),
        )
    available_languages = _available_tesseract_languages(tesseract_path)
    required_languages = _required_tesseract_languages(expected_languages)
    usable_languages = tuple(
        language
        for language in required_languages
        if language in available_languages
    )
    coverage_complete = set(required_languages) <= set(available_languages)

    if artifact_type == "image":
        try:
            tokens, ocr_variant_count = _collect_path_ocr_tokens(
                path,
                tesseract_path=tesseract_path,
                languages=usable_languages,
                minimum_confidence=minimum_confidence,
            )
        except _OcrExecutionError as exc:
            return CreativeEvidenceReceipt(
                receipt_ref=f"ocr-{artifact_hash[:20]}",
                kind="ocr",
                status="unavailable",
                artifact_hashes={"image": artifact_hash},
                source="tesseract:tsv",
                language_coverage=usable_languages,
                findings=(
                    f"ocr_execution_failed={exc.code}",
                    "ocr_absence_is_not_visual_proof; independent human visual review remains required",
                ),
            )
        scanned_frame_count = 1
    else:
        ffmpeg_path = shutil.which(ffmpeg_binary)
        if ffmpeg_path is None:
            return CreativeEvidenceReceipt(
                receipt_ref=f"frame-ocr-{artifact_hash[:20]}",
                kind="frame_ocr",
                status="unavailable",
                artifact_hashes={"mp4": artifact_hash},
                source=f"{Path(tesseract_path).name}+ffmpeg_unavailable",
                language_coverage=usable_languages,
                findings=("FFmpeg executable was not found",),
            )
        try:
            with tempfile.TemporaryDirectory(prefix="creative-frame-ocr-") as temp_dir:
                frame_paths = _extract_video_frames(
                    path,
                    ffmpeg_path=ffmpeg_path,
                    output_dir=Path(temp_dir),
                )
                frame_results = tuple(
                    _collect_path_ocr_tokens(
                        frame_path,
                        tesseract_path=tesseract_path,
                        languages=usable_languages,
                        minimum_confidence=minimum_confidence,
                    )
                    for frame_path in frame_paths
                )
                tokens = tuple(
                    token
                    for frame_tokens, _variant_count in frame_results
                    for token in frame_tokens
                )
                ocr_variant_count = sum(
                    variant_count for _frame_tokens, variant_count in frame_results
                )
                scanned_frame_count = len(frame_paths)
        except _OcrExecutionError as exc:
            return CreativeEvidenceReceipt(
                receipt_ref=f"frame-ocr-{artifact_hash[:20]}",
                kind="frame_ocr",
                status="unavailable",
                artifact_hashes={"mp4": artifact_hash},
                source="tesseract:tsv+ffmpeg",
                language_coverage=usable_languages,
                findings=(
                    f"ocr_execution_failed={exc.code}",
                    "ocr_absence_is_not_visual_proof; independent human visual review remains required",
                ),
            )

    status = "complete" if coverage_complete and required_languages else "partial"
    return CreativeEvidenceReceipt(
        receipt_ref=(
            f"ocr-{artifact_hash[:20]}"
            if artifact_type == "image"
            else f"frame-ocr-{artifact_hash[:20]}"
        ),
        kind="ocr" if artifact_type == "image" else "frame_ocr",
        status=status,
        artifact_hashes={
            "image" if artifact_type == "image" else "mp4": artifact_hash
        },
        source=f"{Path(tesseract_path).name}:tsv",
        language_coverage=usable_languages,
        findings=(
            f"required_languages={','.join(required_languages) or 'none'}",
            f"available_languages={','.join(available_languages) or 'none'}",
            *_receipt_findings(
                tokens,
                prohibited_terms=prohibited_terms,
                scanned_frame_count=scanned_frame_count,
                ocr_variant_count=ocr_variant_count,
            ),
        ),
    )


__all__ = [
    "_prepare_ocr_variants",
    "collect_ocr_evidence",
    "parse_tesseract_tsv",
]
