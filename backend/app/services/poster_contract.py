"""Server-owned exact-copy contract for persisted poster deliverables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re

from app.services.media_assets import (
    MediaContractError,
    normalize_overlay_blocks,
    overlay_blocks_sha256,
    validate_overlay_blocks,
)


_DEFAULT_POSTER_COPY_ROLES = ("title", "subtitle", "tagline")
_CTA_COPY_RE = re.compile(
    r"(?:立即|马上|现在|即刻|点击|了解|体验|购买|预约|注册|咨询|"
    r"learn\s+more|shop\s+now|buy\s+now|try\s+now|get\s+started|sign\s+up|contact\s+us)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PosterExecutionPolicy:
    execution_strategy: str
    allow_degraded_fallback: bool


def poster_execution_policy(spec: object) -> PosterExecutionPolicy:
    """Compile the persisted business policy for a formal poster request."""

    fallback_policy = (
        str(spec.get("fallback_policy") or "primary_only").strip()
        if isinstance(spec, Mapping)
        else "primary_only"
    )
    return PosterExecutionPolicy(
        execution_strategy="commercial_quality",
        allow_degraded_fallback=fallback_policy == "allow_degraded",
    )


def poster_exact_copy_blocks(spec: object) -> tuple[dict[str, str], ...]:
    """Compile persisted poster copy into deterministic compositor blocks.

    ``exact_copy_blocks`` is the future structured contract.  The existing
    textarea remains compatible: each non-empty line is preserved verbatim
    after the compositor's standard surrounding-whitespace normalization, with
    a deterministic hierarchy instead of an LLM-inferred role.
    """

    if not isinstance(spec, Mapping):
        return ()

    explicit = spec.get("exact_copy_blocks")
    if explicit is not None:
        blocks = normalize_overlay_blocks(explicit)
        validate_overlay_blocks(blocks)
        return blocks

    raw_copy = spec.get("exact_copy")
    if raw_copy is None:
        return ()
    if isinstance(raw_copy, Sequence) and not isinstance(raw_copy, (str, bytes)):
        lines = [str(item).strip() for item in raw_copy]
    else:
        lines = [line.strip() for line in str(raw_copy).splitlines()]
    visible_lines = [line for line in lines if line]
    blocks = normalize_overlay_blocks(
        [
            {
                "role": (
                    "cta"
                    if index >= 2
                    and _CTA_COPY_RE.search(line)
                    else _DEFAULT_POSTER_COPY_ROLES[index]
                    if index < len(_DEFAULT_POSTER_COPY_ROLES)
                    else "body"
                ),
                "text": line,
            }
            for index, line in enumerate(visible_lines)
        ]
    )
    validate_overlay_blocks(blocks)
    return blocks


def poster_exact_copy_contract(spec: object) -> tuple[tuple[dict[str, str], ...], str | None]:
    """Return the canonical blocks and their compositor-compatible digest."""

    blocks = poster_exact_copy_blocks(spec)
    digest = overlay_blocks_sha256(blocks)
    if blocks and not digest:
        raise MediaContractError("Poster exact-copy digest could not be created")
    return blocks, digest


__all__ = [
    "PosterExecutionPolicy",
    "poster_exact_copy_blocks",
    "poster_exact_copy_contract",
    "poster_execution_policy",
]
