"""Deterministic font-substitution reporting for deck artifacts (FR-P7).

The PPTX renderer writes the requested family names into the file; a viewer
substitutes whatever the host cannot provide.  This module makes that
substitution explicit: it extracts the requested families from the rendered
PPTX (or the source HTML) and reports every family the rendering host cannot
supply, so the substitution is recorded on the artifact instead of being
discovered by the customer.

Everything here is provider-free and side-effect-free; host font discovery is
cached per process and injectable in tests.
"""

from __future__ import annotations

from io import BytesIO
import html
from pathlib import Path
import re
import shutil
import subprocess
import zipfile
from xml.etree import ElementTree


# CSS generic families are aliases every renderer resolves locally; they are
# never "missing".
GENERIC_FONT_FAMILIES = frozenset(
    {
        "sans-serif",
        "serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "ui-sans-serif",
        "ui-serif",
        "ui-monospace",
        "ui-rounded",
        "emoji",
        "math",
        "fangsong",
        "-apple-system",
        "blinkmacsystemfont",
        "arial",  # metric-compatible fallbacks ship with every renderer host
        "helvetica",
        "times new roman",
    }
)

# Capture runs to the end of the declaration (``;``/``}``) or the enclosing
# attribute/tag; per-entry normalization strips surrounding quotes.
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;}<>]+)", re.IGNORECASE)
_FONT_DIR_CANDIDATES = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
)
_FONT_FILE_SUFFIXES = {".ttf", ".otf", ".ttc", ".woff2"}

_available_cache: frozenset[str] | None = None


def normalize_family(value: str) -> str:
    return " ".join(str(value or "").strip().strip("\"'").casefold().split())


def requested_font_families_from_html(html_text: str) -> tuple[str, ...]:
    """Distinct font families requested by the deck source, fallbacks included."""

    families: list[str] = []
    for match in _FONT_FAMILY_RE.finditer(html.unescape(html_text or "")):
        for entry in match.group(1).split(","):
            normalized = normalize_family(entry)
            if normalized and normalized not in families:
                families.append(normalized)
    return tuple(families)


def requested_font_families_from_pptx(data: bytes) -> tuple[str, ...]:
    """Distinct typefaces the rendered PPTX slides ask viewers to use.

    Only slide XML is read: theme defaults (e.g. the template's Calibri) are
    renderer boilerplate, not a choice the deck made, and ``+mj-lt`` style
    placeholders resolve to those theme defaults.
    """

    families: list[str] = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r"ppt/slides/slide\d+\.xml", name):
                continue
            root = ElementTree.fromstring(archive.read(name))
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                if tag not in {"latin", "ea", "cs"}:
                    continue
                typeface = normalize_family(element.attrib.get("typeface", ""))
                if typeface and not typeface.startswith("+") and typeface not in families:
                    families.append(typeface)
    return tuple(families)


def _fc_list_families() -> frozenset[str]:
    tool = shutil.which("fc-list")
    if tool is None:
        return frozenset()
    try:
        process = subprocess.run(
            [tool, ":", "family"],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return frozenset()
    families: set[str] = set()
    for line in process.stdout.splitlines():
        for entry in line.split(","):
            normalized = normalize_family(entry)
            if normalized:
                families.add(normalized)
    return frozenset(families)


def _font_file_stem_families() -> frozenset[str]:
    """Last-resort discovery: derive family hints from font file names."""

    families: set[str] = set()
    for directory in _FONT_DIR_CANDIDATES:
        root = Path(directory)
        if not root.is_dir():
            continue
        try:
            entries = list(root.rglob("*"))
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() not in _FONT_FILE_SUFFIXES:
                continue
            stem = re.sub(r"(?i)[-_ ](regular|bold|italic|light|medium|thin|black|semibold|demi|book).*$", "", entry.stem)
            normalized = normalize_family(stem.replace("-", " ").replace("_", " "))
            if normalized:
                families.add(normalized)
    return frozenset(families)


def available_font_families(*, refresh: bool = False) -> frozenset[str]:
    """Families the rendering host can actually supply (cached per process)."""

    global _available_cache
    if refresh or _available_cache is None:
        _available_cache = _fc_list_families() | _font_file_stem_families()
    return _available_cache


def font_substitution_report(
    requested: tuple[str, ...] | list[str],
    available: frozenset[str] | set[str],
) -> list[dict[str, str]]:
    """Requested → actual mapping for every family the host cannot supply."""

    available_normalized = {normalize_family(item) for item in available}
    report: list[dict[str, str]] = []
    for family in dict.fromkeys(normalize_family(item) for item in requested):
        if not family or family in GENERIC_FONT_FAMILIES or family in available_normalized:
            continue
        report.append(
            {
                "requested": family,
                "actual": "host default sans/serif fallback",
                "reason": "font_not_installed",
            }
        )
    return report


__all__ = [
    "GENERIC_FONT_FAMILIES",
    "available_font_families",
    "font_substitution_report",
    "normalize_family",
    "requested_font_families_from_html",
    "requested_font_families_from_pptx",
]
