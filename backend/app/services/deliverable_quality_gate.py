"""Hash-bound commercial-quality receipts for durable deliverable approval.

Structural artifact validation proves that a file is readable and matches the
declared output contract.  It does not prove visual, semantic, audio, or
commercial quality.  This module keeps those two claims separate and prevents
an incomplete or failed creative review from being treated as approval-ready.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Literal, Mapping, Sequence
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.deliverable import DeliverableArtifactRevision, DeliverableRequest
from app.services.creative_review_panel import BlindCandidatePanelResult


CREATIVE_WORK_TYPES = frozenset({"presentation", "poster", "video"})
QUALITY_GATE_EVALUATION_KEY = "quality_gate"
QualityReceiptStatus = Literal["passed", "blocked", "incomplete"]
QualityGateStatus = Literal[
    "not_required",
    "pending",
    "passed",
    "blocked",
    "incomplete",
    "invalid",
]
QualityReceiptSource = Literal["blind_review_panel", "automated_evidence"]


class DeliverableQualityGateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeliverableQualityGateReceipt(BaseModel):
    """Immutable commercial-quality conclusion bound to exact artifact bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    receipt_ref: str = Field(min_length=1, max_length=240)
    source: QualityReceiptSource
    status: QualityReceiptStatus
    artifact_hashes: dict[str, str] = Field(min_length=1)
    reviewer_count: int = Field(default=0, ge=0)
    required_evidence_kinds: tuple[str, ...] = ()
    complete_evidence_kinds: tuple[str, ...] = ()
    hard_gate_failures: tuple[str, ...] = ()
    missing_evidence_kinds: tuple[str, ...] = ()
    disagreements: tuple[str, ...] = ()
    commercially_usable: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_conclusion(self) -> "DeliverableQualityGateReceipt":
        for artifact_key, content_hash in self.artifact_hashes.items():
            if not artifact_key or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
                raise ValueError("artifact_hashes must contain lowercase SHA-256 values")
        if not set(self.complete_evidence_kinds) <= set(self.required_evidence_kinds):
            raise ValueError("complete evidence must be a subset of required evidence")
        if self.source == "automated_evidence" and self.status == "passed":
            raise ValueError("automated evidence cannot issue a commercial pass")
        if self.status == "passed":
            if (
                self.source != "blind_review_panel"
                or self.reviewer_count < 3
                or not self.commercially_usable
                or self.hard_gate_failures
                or self.missing_evidence_kinds
                or self.disagreements
                or set(self.complete_evidence_kinds)
                != set(self.required_evidence_kinds)
            ):
                raise ValueError(
                    "a pass requires a complete three-reviewer panel with no failures or disagreements"
                )
        elif self.commercially_usable:
            raise ValueError("blocked or incomplete receipts cannot be commercially usable")
        if self.status == "blocked" and not self.hard_gate_failures:
            raise ValueError("a blocked receipt requires at least one hard-gate failure")
        return self


class DeliverableApprovalReadiness(BaseModel):
    """Reader-facing approval state; blockers are stable machine codes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approvable: bool
    quality_gate_required: bool
    quality_status: QualityGateStatus
    blockers: tuple[str, ...] = ()
    receipt_ref: str | None = None


def _canonical_receipt_bytes(receipt: DeliverableQualityGateReceipt) -> bytes:
    payload = receipt.model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def quality_receipt_sha256(receipt: DeliverableQualityGateReceipt) -> str:
    return hashlib.sha256(_canonical_receipt_bytes(receipt)).hexdigest()


def _uuid_allowlist(raw_value: str, *, setting_name: str) -> frozenset[str]:
    values = frozenset(
        value.strip().lower()
        for value in raw_value.split(",")
        if value.strip()
    )
    for value in values:
        try:
            normalized = str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError(
                f"{setting_name} contains invalid UUID {value!r}"
            ) from exc
        if normalized != value:
            raise ValueError(
                f"{setting_name} contains non-canonical UUID {value!r}"
            )
    return values


def creative_quality_gate_required_for_request(
    request: DeliverableRequest,
    *,
    enabled: bool,
    tenant_ids: str,
    agent_ids: str,
) -> bool:
    """Require review only for an explicitly allowlisted tenant or Agent."""

    if not enabled or request.work_type not in CREATIVE_WORK_TYPES:
        return False
    tenant_allowlist = _uuid_allowlist(
        tenant_ids,
        setting_name="DELIVERABLE_CREATIVE_QUALITY_GATE_TENANT_IDS",
    )
    agent_allowlist = _uuid_allowlist(
        agent_ids,
        setting_name="DELIVERABLE_CREATIVE_QUALITY_GATE_AGENT_IDS",
    )
    return (
        str(request.tenant_id) in tenant_allowlist
        or str(request.agent_id) in agent_allowlist
    )


def quality_gate_evaluation_payload(
    receipt: DeliverableQualityGateReceipt,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "receipt": receipt.model_dump(mode="json"),
        "receipt_sha256": quality_receipt_sha256(receipt),
    }


def selected_deliverable_artifacts(
    request: DeliverableRequest,
    artifacts: Sequence[DeliverableArtifactRevision],
) -> tuple[DeliverableArtifactRevision, ...]:
    required_types = tuple(
        dict.fromkeys(str(item).strip().lower() for item in request.output_contract)
    )
    latest: dict[str, DeliverableArtifactRevision] = {}
    for artifact in artifacts:
        if artifact.status not in {"candidate", "approved"}:
            continue
        current = latest.get(artifact.artifact_key)
        if current is None or artifact.revision_number > current.revision_number:
            latest[artifact.artifact_key] = artifact
    return tuple(latest[key] for key in required_types if key in latest)


def _artifact_hashes(
    artifacts: Sequence[DeliverableArtifactRevision],
) -> dict[str, str]:
    return {artifact.artifact_key: artifact.content_hash for artifact in artifacts}


def _parse_quality_gate_payload(
    payload: object,
) -> DeliverableQualityGateReceipt:
    if not isinstance(payload, Mapping):
        raise ValueError("quality-gate payload is not an object")
    receipt = DeliverableQualityGateReceipt.model_validate(payload.get("receipt"))
    digest = payload.get("receipt_sha256")
    if digest != quality_receipt_sha256(receipt):
        raise ValueError("quality-gate receipt digest does not match")
    return receipt


def deliverable_approval_readiness(
    request: DeliverableRequest,
    artifacts: Sequence[DeliverableArtifactRevision],
    *,
    require_creative_quality_gate: bool,
) -> DeliverableApprovalReadiness:
    """Calculate approval readiness without mutating request or artifact state."""

    is_creative = request.work_type in CREATIVE_WORK_TYPES
    gate_required = bool(is_creative and require_creative_quality_gate)
    selected = selected_deliverable_artifacts(request, artifacts)
    required_types = tuple(
        dict.fromkeys(str(item).strip().lower() for item in request.output_contract)
    )
    if len(selected) != len(required_types):
        return DeliverableApprovalReadiness(
            approvable=False,
            quality_gate_required=gate_required,
            quality_status="pending" if is_creative else "not_required",
            blockers=("deliverable_artifact_missing",),
        )

    payloads: list[object] = []
    for artifact in selected:
        evaluation = artifact.evaluation
        payloads.append(
            evaluation.get(QUALITY_GATE_EVALUATION_KEY)
            if isinstance(evaluation, Mapping)
            else None
        )
    present_payloads = [payload for payload in payloads if payload is not None]
    if not present_payloads:
        blockers: tuple[str, ...] = ()
        quality_status: QualityGateStatus = "not_required"
        if gate_required:
            blockers = ("deliverable_creative_quality_review_required",)
            quality_status = "pending"
        if request.current_stage != "output_review":
            blockers = (*blockers, "deliverable_not_in_output_review")
        return DeliverableApprovalReadiness(
            approvable=not blockers,
            quality_gate_required=gate_required,
            quality_status=quality_status,
            blockers=blockers,
        )
    if len(present_payloads) != len(selected):
        return DeliverableApprovalReadiness(
            approvable=False,
            quality_gate_required=gate_required,
            quality_status="invalid",
            blockers=("deliverable_creative_quality_receipt_invalid",),
        )

    try:
        receipts = tuple(_parse_quality_gate_payload(payload) for payload in present_payloads)
    except (TypeError, ValueError):
        return DeliverableApprovalReadiness(
            approvable=False,
            quality_gate_required=gate_required,
            quality_status="invalid",
            blockers=("deliverable_creative_quality_receipt_invalid",),
        )
    first = receipts[0]
    if any(receipt != first for receipt in receipts[1:]):
        return DeliverableApprovalReadiness(
            approvable=False,
            quality_gate_required=gate_required,
            quality_status="invalid",
            blockers=("deliverable_creative_quality_receipt_mismatch",),
        )
    if first.artifact_hashes != _artifact_hashes(selected):
        return DeliverableApprovalReadiness(
            approvable=False,
            quality_gate_required=gate_required,
            quality_status="invalid",
            blockers=("deliverable_creative_quality_receipt_hash_mismatch",),
            receipt_ref=first.receipt_ref,
        )

    status_to_blocker = {
        "blocked": "deliverable_creative_quality_blocked",
        "incomplete": "deliverable_creative_quality_incomplete",
    }
    blocker = status_to_blocker.get(first.status)
    blockers = (blocker,) if blocker else ()
    if request.current_stage != "output_review":
        blockers = (*blockers, "deliverable_not_in_output_review")
    return DeliverableApprovalReadiness(
        approvable=not blockers,
        quality_gate_required=gate_required,
        quality_status=first.status,
        blockers=blockers,
        receipt_ref=first.receipt_ref,
    )


def enforce_deliverable_quality_gate(
    request: DeliverableRequest,
    artifacts: Sequence[DeliverableArtifactRevision],
    *,
    require_creative_quality_gate: bool,
) -> None:
    readiness = deliverable_approval_readiness(
        request,
        artifacts,
        require_creative_quality_gate=require_creative_quality_gate,
    )
    quality_blockers = tuple(
        blocker
        for blocker in readiness.blockers
        if blocker.startswith("deliverable_creative_quality_")
    )
    if quality_blockers:
        raise DeliverableQualityGateError(
            quality_blockers[0],
            "Creative deliverable has not passed its hash-bound commercial quality review",
        )


def attach_deliverable_quality_gate_receipt(
    artifacts: Sequence[DeliverableArtifactRevision],
    receipt: DeliverableQualityGateReceipt,
) -> None:
    """Attach one identical, hash-bound receipt to every artifact in the set."""

    if receipt.artifact_hashes != _artifact_hashes(artifacts):
        raise DeliverableQualityGateError(
            "deliverable_creative_quality_receipt_hash_mismatch",
            "Quality receipt hashes do not match the selected artifact set",
        )
    payload = quality_gate_evaluation_payload(receipt)
    for artifact in artifacts:
        evaluation = dict(artifact.evaluation or {})
        evaluation[QUALITY_GATE_EVALUATION_KEY] = payload
        artifact.evaluation = evaluation


def quality_receipt_from_panel_result(
    result: BlindCandidatePanelResult,
    *,
    artifact_hashes: Mapping[str, str],
    receipt_ref: str,
    created_at: datetime | None = None,
) -> DeliverableQualityGateReceipt:
    """Convert one formal blind-panel result into an approval receipt."""

    failed_gates = tuple(result.evaluation.hard_gate_failures)
    status: QualityReceiptStatus
    if result.panel_status == "blocked":
        status = "blocked"
    elif result.panel_status == "scored" and result.commercially_usable:
        status = "passed"
    else:
        status = "incomplete"
    return DeliverableQualityGateReceipt(
        receipt_ref=receipt_ref,
        source="blind_review_panel",
        status=status,
        artifact_hashes=dict(artifact_hashes),
        reviewer_count=result.reviewer_count,
        required_evidence_kinds=result.required_evidence_kinds,
        complete_evidence_kinds=result.complete_evidence_kinds,
        hard_gate_failures=failed_gates,
        missing_evidence_kinds=result.missing_evidence_kinds,
        disagreements=result.disagreements,
        commercially_usable=result.commercially_usable,
        created_at=created_at or datetime.now(UTC),
    )


def blocked_quality_receipt_from_automated_evidence(
    *,
    receipt_ref: str,
    artifact_hashes: Mapping[str, str],
    evidence_kind: str,
    hard_gate_failures: Sequence[str],
    created_at: datetime | None = None,
) -> DeliverableQualityGateReceipt:
    """Record exact automated failure evidence; never issue a positive pass."""

    return DeliverableQualityGateReceipt(
        receipt_ref=receipt_ref,
        source="automated_evidence",
        status="blocked",
        artifact_hashes=dict(artifact_hashes),
        reviewer_count=0,
        required_evidence_kinds=(evidence_kind,),
        complete_evidence_kinds=(evidence_kind,),
        hard_gate_failures=tuple(hard_gate_failures),
        commercially_usable=False,
        created_at=created_at or datetime.now(UTC),
    )


__all__ = [
    "DeliverableApprovalReadiness",
    "DeliverableQualityGateError",
    "DeliverableQualityGateReceipt",
    "attach_deliverable_quality_gate_receipt",
    "blocked_quality_receipt_from_automated_evidence",
    "creative_quality_gate_required_for_request",
    "deliverable_approval_readiness",
    "enforce_deliverable_quality_gate",
    "quality_gate_evaluation_payload",
    "quality_receipt_from_panel_result",
    "quality_receipt_sha256",
    "selected_deliverable_artifacts",
]
