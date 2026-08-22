"""Version-bound product navigation facts for Work runtime grounding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import hmac
import json
from pathlib import Path
import re


_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "product_information_architecture.v1.json"
)
_SUPPORTED_LOCALES = ("zh-CN", "en")
_ARROW_RE = re.compile(r"\s*(?:→|->|›|»|⟶)\s*")
_BACKTICK_ROUTE_RE = re.compile(r"`(?P<route>/[^`\s]+)`")
_MARKDOWN_PREFIX_RE = re.compile(r"^[\s#>*+\-\d.)、]+")
_LEADING_ACTIONS = (
    "请进入",
    "请打开",
    "请点击",
    "请前往",
    "进入",
    "打开",
    "点击",
    "前往",
    "please navigate to",
    "please go to",
    "please open",
    "navigate to",
    "go to",
    "open",
)


@dataclass(frozen=True, slots=True)
class ProductNavigationEvaluation:
    required: bool
    valid: bool
    passed: bool
    details: dict
    repair_reason: str | None = None


def _catalog_payload(value: Mapping[str, object]) -> dict:
    return {
        "version": value.get("version"),
        "catalog_id": value.get("catalog_id"),
        "entries": value.get("entries"),
    }


def _catalog_digest(value: Mapping[str, object]) -> str:
    canonical = json.dumps(
        _catalog_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_entries(value: Mapping[str, object]) -> list[dict] | None:
    raw_entries = value.get("entries")
    if (
        value.get("version") != 1
        or not isinstance(value.get("catalog_id"), str)
        or not str(value.get("catalog_id")).strip()
        or not isinstance(raw_entries, Sequence)
        or isinstance(raw_entries, (str, bytes, bytearray))
        or not raw_entries
    ):
        return None

    entries: list[dict] = []
    ids: set[str] = set()
    locale_breadcrumbs: dict[str, set[tuple[str, ...]]] = {
        locale: set() for locale in _SUPPORTED_LOCALES
    }
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            return None
        entry_id = raw_entry.get("id")
        route = raw_entry.get("route")
        access = raw_entry.get("access")
        breadcrumbs = raw_entry.get("breadcrumbs")
        if (
            not isinstance(entry_id, str)
            or not entry_id.strip()
            or entry_id in ids
            or not isinstance(route, str)
            or not route.startswith("/")
            or not isinstance(access, str)
            or not access.strip()
            or not isinstance(breadcrumbs, Mapping)
        ):
            return None
        normalized_breadcrumbs: dict[str, list[str]] = {}
        for locale in _SUPPORTED_LOCALES:
            raw_segments = breadcrumbs.get(locale)
            if (
                not isinstance(raw_segments, Sequence)
                or isinstance(raw_segments, (str, bytes, bytearray))
                or not raw_segments
                or any(not isinstance(segment, str) or not segment.strip() for segment in raw_segments)
            ):
                return None
            segments = [str(segment).strip() for segment in raw_segments]
            signature = tuple(_normalize_label(segment) for segment in segments)
            if signature in locale_breadcrumbs[locale]:
                return None
            locale_breadcrumbs[locale].add(signature)
            normalized_breadcrumbs[locale] = segments
        ids.add(entry_id)
        entries.append(
            {
                "id": entry_id,
                "route": route,
                "access": access,
                "breadcrumbs": normalized_breadcrumbs,
            }
        )
    return entries


@lru_cache(maxsize=1)
def _load_catalog() -> dict:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise RuntimeError("product information architecture catalog must be an object")
    entries = _validated_entries(raw)
    if entries is None:
        raise RuntimeError("product information architecture catalog is invalid")
    catalog = {
        "version": 1,
        "catalog_id": str(raw["catalog_id"]),
        "entries": entries,
    }
    catalog["catalog_sha256"] = _catalog_digest(catalog)
    return catalog


def product_information_architecture_snapshot() -> dict:
    """Return a JSON-safe immutable-by-copy catalog bound to a digest."""

    return deepcopy(_load_catalog())


def _validated_snapshot(value: object) -> tuple[list[dict], str] | None:
    if not isinstance(value, Mapping):
        return None
    entries = _validated_entries(value)
    digest = value.get("catalog_sha256")
    if (
        entries is None
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not hmac.compare_digest(digest, _catalog_digest(value))
    ):
        return None
    return entries, str(value.get("catalog_id"))


def render_product_information_architecture_prompt(value: object) -> str:
    """Render only public product navigation facts; catalog access never grants authority."""

    validated = _validated_snapshot(value)
    if validated is None:
        return ""
    entries, catalog_id = validated
    lines = [
        f"Astra 产品入口目录（{catalog_id}，仅描述入口，不授予权限）：",
    ]
    for entry in entries:
        breadcrumb = " → ".join(entry["breadcrumbs"]["zh-CN"])
        lines.append(f"- {breadcrumb} (`{entry['route']}`; access={entry['access']})")
    lines.extend(
        [
            "如果结果需要说明用户在 Astra 中点击哪里，只能逐字使用上面目录中的 breadcrumb 和 route。",
            "目录中没有的入口必须明确写为“当前版本没有可验证入口”，不得自行创造菜单、页面或已上线能力。",
            "普通业务步骤不要伪装成产品 breadcrumb；页面存在也不代表当前用户有权限或功能已启用。",
        ]
    )
    return "\n".join(lines)


def _normalize_label(value: str) -> str:
    normalized = value.strip()
    normalized = re.split(r"[:：；;。]", normalized, maxsplit=1)[0]
    normalized = re.sub(
        r"\s*[（(]\s*`?/[^`）)]+`?\s*[）)]\s*$",
        "",
        normalized,
    )
    normalized = normalized.strip("`*_[]()（）\"'‘’“”")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.casefold().strip()


def _claim_segments(line: str, roots: set[str]) -> list[str] | None:
    if _ARROW_RE.search(line) is None:
        return None
    raw_segments = _ARROW_RE.split(_MARKDOWN_PREFIX_RE.sub("", line.strip()))
    if len(raw_segments) < 2:
        return None
    segments = [_normalize_label(segment) for segment in raw_segments]
    raw_first = raw_segments[0]
    first_candidates = [segments[0]]
    if re.search(r"[:：]", raw_first):
        first_candidates.append(_normalize_label(re.split(r"[:：]", raw_first)[-1]))
    first = ""
    for candidate in first_candidates:
        normalized_candidate = candidate
        for action in _LEADING_ACTIONS:
            normalized_action = _normalize_label(action)
            if normalized_candidate.startswith(f"{normalized_action} "):
                normalized_candidate = normalized_candidate[len(normalized_action) :].strip()
            elif normalized_candidate.startswith(normalized_action) and normalized_action in {
                "请进入",
                "请打开",
                "请点击",
                "请前往",
                "进入",
                "打开",
                "点击",
                "前往",
            }:
                normalized_candidate = normalized_candidate[len(normalized_action) :].strip()
        if normalized_candidate in roots:
            first = normalized_candidate
            break
    segments[0] = first
    if first not in roots or any(not segment for segment in segments):
        return None
    return segments


def evaluate_product_navigation_claims(
    value: object,
    candidate: str,
) -> ProductNavigationEvaluation:
    """Reject explicit Astra breadcrumbs that are absent from the bound catalog."""

    if value is None:
        return ProductNavigationEvaluation(
            required=False,
            valid=True,
            passed=True,
            details={"code": "product_navigation_grounding_not_required"},
        )
    validated = _validated_snapshot(value)
    if validated is None:
        return ProductNavigationEvaluation(
            required=True,
            valid=False,
            passed=False,
            details={"code": "invalid_product_information_architecture_snapshot"},
        )
    entries, catalog_id = validated
    allowed_by_locale: dict[str, set[tuple[str, ...]]] = {
        locale: {
            tuple(_normalize_label(segment) for segment in entry["breadcrumbs"][locale])
            for entry in entries
        }
        for locale in _SUPPORTED_LOCALES
    }
    roots = {
        signature[0]
        for signatures in allowed_by_locale.values()
        for signature in signatures
    }
    claims: list[list[str]] = []
    invalid_claims: list[list[str]] = []
    invalid_routes: list[dict[str, object]] = []
    allowed_claims = set().union(*allowed_by_locale.values())
    routes_by_claim: dict[tuple[str, ...], set[str]] = {}
    for entry in entries:
        for locale in _SUPPORTED_LOCALES:
            signature = tuple(
                _normalize_label(segment) for segment in entry["breadcrumbs"][locale]
            )
            routes_by_claim.setdefault(signature, set()).add(entry["route"])

    def route_matches(template: str, claimed: str) -> bool:
        pattern = re.escape(template)
        pattern = re.sub(r"\\\{[^{}]+\\\}", r"[^/]+", pattern)
        return re.fullmatch(pattern, claimed) is not None

    for line in candidate.splitlines():
        segments = _claim_segments(line, roots)
        if segments is None:
            continue
        claims.append(segments)
        signature = tuple(segments)
        if signature not in allowed_claims:
            invalid_claims.append(segments)
            continue
        claimed_routes = [match.group("route") for match in _BACKTICK_ROUTE_RE.finditer(line)]
        allowed_routes = routes_by_claim.get(signature, set())
        for claimed_route in claimed_routes:
            if not any(route_matches(template, claimed_route) for template in allowed_routes):
                invalid_routes.append(
                    {
                        "breadcrumb": segments,
                        "claimed_route": claimed_route,
                        "allowed_routes": sorted(allowed_routes),
                    }
                )
    passed = not invalid_claims and not invalid_routes
    details = {
        "code": (
            "product_navigation_grounding_passed"
            if passed
            else "product_navigation_grounding_failed"
        ),
        "catalog_id": catalog_id,
        "claim_count": len(claims),
        "claims": claims,
        "invalid_claims": invalid_claims,
        "invalid_routes": invalid_routes,
    }
    return ProductNavigationEvaluation(
        required=True,
        valid=True,
        passed=passed,
        details=details,
        repair_reason=(
            None
            if passed
            else "The result cites an Astra breadcrumb that does not exist in the confirmed product catalog. Use an exact catalog breadcrumb and route, or state that the current version has no verified entry."
        ),
    )


__all__ = [
    "ProductNavigationEvaluation",
    "evaluate_product_navigation_claims",
    "product_information_architecture_snapshot",
    "render_product_information_architecture_prompt",
]
