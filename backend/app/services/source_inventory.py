"""Source inventory and fact-assertion reconciliation for v2 decks (FR-P2).

Every uploaded file, declared URL, and the brief itself is registered with a
stable ``source_id`` and a SHA-256 binding *before* any outline or render work
exists.  The reconciliation in this module is a pure, provider-free function:
it resolves ``slide_spec.source_refs`` against the inventory and requires every
quantified or ranking fact assertion in slide copy to be traceable to a
registered source — unsourced assertions must carry an explicit assumption
label or the deck fails the semantic gate.  This is the execution layer of the
"no fabricated facts" hard gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.deliverable import DeliverableRequest
from app.services.storage import agent_storage_key, get_storage_backend
from app.services.text_extractor import extract_text


SOURCE_INVENTORY_SCHEMA_VERSION = "source-inventory-v1"
SEMANTIC_QA_SCHEMA_VERSION = "semantic-qa-v1"

IMAGE_VISUAL_KINDS = frozenset({"generated_image", "supplied_image"})
EDITABLE_VISUAL_KINDS = frozenset(
    {
        "editable_chart",
        "editable_diagram",
        "editable_table",
        "editable_typography",
    }
)

_MAX_FACTS_PER_ENTRY = 40
_MAX_FACT_CHARS = 240


class SourceInventoryEntry(BaseModel):
    """One hash-bound evidence source registered before any paid work."""

    source_id: str = Field(pattern=r"^src-\d{2}$")
    kind: Literal["upload", "url", "brief", "assumption"]
    path: str = Field(default="", max_length=1000)
    url: str = Field(default="", max_length=1000)
    sha256: str = Field(default="", max_length=64)
    extracted_facts: tuple[str, ...] = ()
    registered_by: Literal["server", "agent", "user"] = "server"

    model_config = ConfigDict(extra="forbid", frozen=True)


class FactAssertion(BaseModel):
    """One deterministic fact-assertion hit inside slide copy."""

    text: str = Field(min_length=1, max_length=500)
    kind: Literal["quantified", "ranking"]
    assumption: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticFinding(BaseModel):
    """One machine-actionable semantic gate violation."""

    code: Literal[
        "unresolved_source_ref",
        "unsourced_fact_assertion",
        "image_slide_fact_assertion",
        "data_slide_not_editable",
    ]
    slide_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(default="", max_length=200)

    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticReconciliation(BaseModel):
    """Hash-bound semantic QA verdict for one slide_spec revision."""

    schema_version: str = SEMANTIC_QA_SCHEMA_VERSION
    inventory_sha256: str = Field(min_length=64, max_length=64)
    assertion_count: int = Field(ge=0)
    assumption_count: int = Field(ge=0)
    findings: tuple[SemanticFinding, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def passed(self) -> bool:
        return not self.findings


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inventory_sha256(entries: Sequence[SourceInventoryEntry]) -> str:
    return _canonical_sha256([entry.model_dump(mode="json") for entry in entries])


def _normalized_ref(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def _workspace_input_path(value: object) -> str:
    path = _normalized_ref(value).lstrip("/")
    if not path or ".." in path.split("/"):
        return ""
    if path.startswith("uploads/"):
        path = f"workspace/{path}"
    if not path.startswith("workspace/"):
        return ""
    return path


def _fact_lines(text: str) -> tuple[str, ...]:
    facts: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(str(raw_line).split())
        if not line:
            continue
        facts.append(line[:_MAX_FACT_CHARS])
        if len(facts) >= _MAX_FACTS_PER_ENTRY:
            break
    return tuple(facts)


def _brief_facts(request: DeliverableRequest) -> tuple[str, ...]:
    facts: list[str] = []
    goal = " ".join(str(request.goal or "").split())
    if goal:
        facts.append(goal[:_MAX_FACT_CHARS])
    spec = request.spec if isinstance(request.spec, Mapping) else {}
    key_points = spec.get("key_points")
    lines: Sequence[object]
    if isinstance(key_points, str):
        lines = key_points.splitlines()
    elif isinstance(key_points, Sequence) and not isinstance(key_points, (str, bytes)):
        lines = key_points
    else:
        lines = ()
    for raw in lines:
        line = " ".join(str(raw or "").split())
        if line:
            facts.append(line[:_MAX_FACT_CHARS])
        if len(facts) >= _MAX_FACTS_PER_ENTRY:
            break
    return tuple(facts)


async def compile_source_inventory(
    request: DeliverableRequest,
    *,
    storage=None,
) -> tuple[SourceInventoryEntry, ...]:
    """Register every declared evidence source with its hash binding.

    Uploads are read from workspace storage and hashed; extracted text becomes
    the citable fact pool.  An unreadable upload stays registered (audit trail)
    but carries no facts, so it can never silently source an assertion.
    """

    entries: list[SourceInventoryEntry] = []
    storage_backend = storage or get_storage_backend()

    for item in request.inputs or ():
        value = item.get("path") if isinstance(item, Mapping) else getattr(item, "path", None)
        path = _workspace_input_path(value)
        if not path:
            continue
        source_id = f"src-{len(entries) + 1:02d}"
        digest = ""
        facts: tuple[str, ...] = ()
        try:
            data = await storage_backend.read_bytes(
                agent_storage_key(request.agent_id, path)
            )
        except Exception:
            data = None
        if data is not None:
            digest = hashlib.sha256(data).hexdigest()
            filename = path.rsplit("/", 1)[-1]
            try:
                extracted = extract_text(data, filename)
            except Exception:
                extracted = None
            if extracted:
                facts = _fact_lines(extracted)
            elif not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                # A non-image upload that yields no text still proves its own
                # bytes; it simply cannot source a textual fact assertion.
                facts = ()
        entries.append(
            SourceInventoryEntry(
                source_id=source_id,
                kind="upload",
                path=path,
                sha256=digest,
                extracted_facts=facts,
                registered_by="server",
            )
        )

    spec = request.spec if isinstance(request.spec, Mapping) else {}
    raw_urls = spec.get("source_urls")
    if isinstance(raw_urls, Sequence) and not isinstance(raw_urls, (str, bytes)):
        for item in raw_urls:
            url = ""
            declared_facts: tuple[str, ...] = ()
            if isinstance(item, str):
                url = item.strip()
            elif isinstance(item, Mapping):
                url = str(item.get("url") or "").strip()
                raw_facts = item.get("facts")
                if isinstance(raw_facts, Sequence) and not isinstance(
                    raw_facts, (str, bytes)
                ):
                    declared_facts = tuple(
                        " ".join(str(fact).split())[:_MAX_FACT_CHARS]
                        for fact in raw_facts
                        if str(fact or "").strip()
                    )[:_MAX_FACTS_PER_ENTRY]
            if not url or not re.match(r"^https?://", url):
                continue
            entries.append(
                SourceInventoryEntry(
                    source_id=f"src-{len(entries) + 1:02d}",
                    kind="url",
                    url=url,
                    extracted_facts=declared_facts,
                    registered_by="user",
                )
            )

    # The brief itself is always a registered source: user-stated requirements
    # are legitimate evidence, and binding them keeps brief-cited numbers
    # traceable to the exact request revision.
    entries.append(
        SourceInventoryEntry(
            source_id=f"src-{len(entries) + 1:02d}",
            kind="brief",
            sha256=_canonical_sha256(
                {"goal": str(request.goal or ""), "key_points": _brief_facts(request)}
            ),
            extracted_facts=_brief_facts(request),
            registered_by="user",
        )
    )
    return tuple(entries)


def resolve_source_ref(
    ref: object,
    entries: Sequence[SourceInventoryEntry],
) -> SourceInventoryEntry | None:
    """Resolve one slide_spec source_ref to an inventory entry, or ``None``."""

    normalized = _normalized_ref(ref)
    if not normalized:
        return None
    folded = normalized.casefold()
    for entry in entries:
        if entry.source_id.casefold() == folded:
            return entry
        if entry.path and _normalized_ref(entry.path).casefold() == folded:
            return entry
        if entry.url and entry.url == str(ref).strip():
            return entry
        if entry.sha256 and entry.sha256 == normalized:
            return entry
    return None


_QUANTIFIED_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:[%％°]|"
    r"倍|万|亿|千|百|小时|分钟|秒|天|周|个月|年|月|日|元|块|美元|人民币|"
    r"公里|千米|米|厘米|毫米|千克|克|毫升|升|度|层|款|台|件|次|人|家|个|"
    r"km|kg|cm|mm|ml|GB|MB|TB|Hz|kWh|W)"
    r"|\b\d{2,}(?:\.\d+)?\b",
    re.IGNORECASE,
)
_RANKING_PATTERN = re.compile(
    r"第\s*[一1]\b|排名第一|销量第一|行业第一|领先|TOP\s*\d*|No\.?\s*1|"
    r"最(?:高|大|快|强|好|低|小|慢)|"
    r"\b(?:best|leading|largest|fastest|top-rated|number one)\b",
    re.IGNORECASE,
)
_ASSUMPTION_MARKERS = (
    "假设",
    "假定",
    "待验证",
    "待确认",
    "预估",
    "assumption",
    "hypothesis",
    "to be validated",
    "to be confirmed",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")


def detect_fact_assertions(text: object) -> tuple[FactAssertion, ...]:
    """Deterministically flag quantified/ranking claims in slide copy.

    Explicitly labelled assumptions are reported but exempt from the sourcing
    requirement — the label itself satisfies the "no fabrication presented as
    fact" contract.
    """

    normalized = " ".join(str(text or "").split())
    if not normalized:
        return ()
    assertions: list[FactAssertion] = []
    for sentence in _SENTENCE_SPLIT.split(normalized):
        sentence = sentence.strip()
        if not sentence:
            continue
        kind: Literal["quantified", "ranking"] | None = None
        if _QUANTIFIED_PATTERN.search(sentence):
            kind = "quantified"
        elif _RANKING_PATTERN.search(sentence):
            kind = "ranking"
        if kind is None:
            continue
        lowered = sentence.casefold()
        assertions.append(
            FactAssertion(
                text=sentence[:500],
                kind=kind,
                assumption=any(marker in lowered for marker in _ASSUMPTION_MARKERS),
            )
        )
    return tuple(assertions)


def _numeric_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+(?:\.\d+)?", text))


def _numeric_token_in_haystack(token: str, haystack: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(token)}(?!\d)", haystack) is not None


def _slide_copy_text(slide: Mapping[str, Any]) -> str:
    parts: list[str] = [str(slide.get("headline") or "")]
    body_points = slide.get("body_points")
    if isinstance(body_points, Sequence) and not isinstance(body_points, (str, bytes)):
        parts.extend(str(point or "") for point in body_points)
    return " ".join(part for part in (item.strip() for item in parts) if part)


def reconcile_slide_semantics(
    slides: Sequence[Mapping[str, Any]],
    entries: Sequence[SourceInventoryEntry],
) -> SemanticReconciliation:
    """Reconcile slide copy against the registered inventory, fail closed.

    Every slide's ``source_refs`` must resolve to inventory entries; every
    un-labelled fact assertion must be traceable to a resolved source whose
    registered facts carry the asserted numbers.  Image slides carry no fact
    assertions at all (FR-P5), and ``data_slide`` pages must use an editable
    visual kind (FR-P5).  This is the deterministic 100% interception core;
    it never calls a model.
    """

    findings: list[SemanticFinding] = []
    assertion_count = 0
    assumption_count = 0
    for index, slide in enumerate(slides, start=1):
        slide_id = str(slide.get("slide_id") or f"slide-{index:02d}")
        raw_refs = slide.get("source_refs")
        ref_values: list[str] = []
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, (str, bytes)):
            for item in raw_refs:
                if isinstance(item, Mapping):
                    ref_values.append(str(item.get("ref") or ""))
                else:
                    ref_values.append(str(item or ""))
        resolved = [
            entry
            for entry in (resolve_source_ref(ref, entries) for ref in ref_values)
            if entry is not None
        ]
        resolved_fact_pools = [
            " ".join(entry.extracted_facts) for entry in resolved if entry.extracted_facts
        ]
        unresolved_refs = [
            ref
            for ref, entry in zip(
                ref_values,
                (resolve_source_ref(ref, entries) for ref in ref_values),
                strict=False,
            )
            if ref.strip() and entry is None
        ]
        for ref in unresolved_refs:
            findings.append(
                SemanticFinding(
                    code="unresolved_source_ref",
                    slide_id=slide_id,
                    message=(
                        f"slide {slide_id} source_ref '{ref[:80]}' does not resolve "
                        "to any registered source inventory entry"
                    ),
                    excerpt=ref[:200],
                )
            )

        visual_kind = str(slide.get("visual_kind") or "").strip()
        if slide.get("data_slide") is True and visual_kind not in EDITABLE_VISUAL_KINDS:
            findings.append(
                SemanticFinding(
                    code="data_slide_not_editable",
                    slide_id=slide_id,
                    message=(
                        f"slide {slide_id} declares data_slide=true but uses "
                        f"visual_kind '{visual_kind or '<empty>'}'; data pages must "
                        "use editable_chart/editable_diagram/editable_table/editable_typography"
                    ),
                )
            )

        assertions = detect_fact_assertions(_slide_copy_text(slide))
        assertion_count += len(assertions)
        assumption_count += sum(1 for assertion in assertions if assertion.assumption)
        pending = [assertion for assertion in assertions if not assertion.assumption]
        if visual_kind in IMAGE_VISUAL_KINDS and pending:
            findings.append(
                SemanticFinding(
                    code="image_slide_fact_assertion",
                    slide_id=slide_id,
                    message=(
                        f"slide {slide_id} is an image slide ({visual_kind}) and must "
                        "not state fact assertions; move the claim to an editable "
                        "sourced slide or label it as an assumption"
                    ),
                    excerpt=pending[0].text[:200],
                )
            )
            continue
        for assertion in pending:
            tokens = _numeric_tokens(assertion.text)
            sourced = False
            if resolved_fact_pools:
                if tokens:
                    sourced = any(
                        all(
                            _numeric_token_in_haystack(token, pool) for token in tokens
                        )
                        for pool in resolved_fact_pools
                    )
                else:
                    # Ranking/superlative claims cannot be number-matched; the
                    # deterministic core requires a hash-bound fact-bearing
                    # source and leaves semantic adequacy to human review.
                    sourced = True
            if not sourced:
                findings.append(
                    SemanticFinding(
                        code="unsourced_fact_assertion",
                        slide_id=slide_id,
                        message=(
                            f"slide {slide_id} states a {assertion.kind} fact without "
                            "a registered source carrying it; cite a source inventory "
                            "entry or label the statement as an assumption (假设)"
                        ),
                        excerpt=assertion.text[:200],
                    )
                )
    return SemanticReconciliation(
        inventory_sha256=inventory_sha256(entries),
        assertion_count=assertion_count,
        assumption_count=assumption_count,
        findings=tuple(findings),
    )


def semantic_report_checks(
    reconciliation: SemanticReconciliation,
) -> tuple[dict[str, Any], ...]:
    """Shape the reconciliation as generic QA check rows for unit receipts."""

    def check(name: str, code: str) -> dict[str, Any]:
        matched = [
            finding for finding in reconciliation.findings if finding.code == code
        ]
        return {
            "name": name,
            "status": "failed" if matched else "passed",
            "evidence": tuple(f"{item.slide_id}: {item.message}" for item in matched[:5]),
        }

    return (
        check("source_refs_resolved", "unresolved_source_ref"),
        check("fact_assertions_sourced", "unsourced_fact_assertion"),
        check("image_slides_fact_free", "image_slide_fact_assertion"),
        check("data_slides_editable", "data_slide_not_editable"),
    )


__all__ = [
    "EDITABLE_VISUAL_KINDS",
    "IMAGE_VISUAL_KINDS",
    "SEMANTIC_QA_SCHEMA_VERSION",
    "SOURCE_INVENTORY_SCHEMA_VERSION",
    "FactAssertion",
    "SemanticFinding",
    "SemanticReconciliation",
    "SourceInventoryEntry",
    "compile_source_inventory",
    "detect_fact_assertions",
    "inventory_sha256",
    "reconcile_slide_semantics",
    "resolve_source_ref",
    "semantic_report_checks",
]
