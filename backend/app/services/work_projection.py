"""User-facing work stages projected from authoritative product facts."""

from __future__ import annotations

from collections.abc import Mapping


TERMINAL_RUN_EVENTS = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_cancelled": "cancelled",
}


def project_execution_status(
    *,
    task_status: str | None,
    terminal_run_event: str | None,
) -> str:
    if terminal_run_event in TERMINAL_RUN_EVENTS:
        return TERMINAL_RUN_EVENTS[terminal_run_event]
    if task_status == "doing":
        return "running"
    if task_status == "pending":
        return "queued"
    if task_status == "failed":
        return "failed"
    if task_status == "done":
        return "completed"
    return "not_started"


def project_user_stage(
    *,
    task_status: str | None,
    execution_status: str,
    deliverable_status: str | None,
    artifact_status: str | None,
    review_status: str | None,
    task_result_review_status: str = "not_required",
) -> str:
    """Return a stage, never promoting Task.done to formal Delivery."""
    if deliverable_status == "failed" or execution_status == "failed":
        return "blocked"
    if deliverable_status == "cancelled" or execution_status == "cancelled":
        return "cancelled"
    if deliverable_status == "succeeded" and artifact_status == "approved":
        return "delivery"
    if review_status in {"open", "incomplete", "blocked"}:
        return "review"
    if deliverable_status == "waiting_approval":
        return "approval"
    if artifact_status in {"candidate", "approved", "rejected", "superseded"}:
        return "artifact"
    if deliverable_status in {"ready", "running"}:
        return "execution"
    if execution_status in {"queued", "running"}:
        return "execution"
    if task_result_review_status == "request_changes":
        return "blocked"
    if task_result_review_status == "pending":
        return "review"
    if task_status == "done" or execution_status == "completed":
        return "completed"
    return "task"


def work_requires_owner_review(work_statement: object) -> bool:
    if not isinstance(work_statement, Mapping):
        return False
    contract = work_statement.get("acceptance_contract")
    return (
        isinstance(contract, Mapping)
        and contract.get("version") == 1
        and contract.get("owner_review_required") is True
    )


def project_task_result_review_status(
    *,
    task_status: str | None,
    work_statement: object,
    receipt_action: str | None,
) -> str:
    if not work_requires_owner_review(work_statement) or task_status != "done":
        return "not_required"
    if receipt_action in {"approve", "request_changes"}:
        return "approved" if receipt_action == "approve" else "request_changes"
    return "pending"


__all__ = [
    "TERMINAL_RUN_EVENTS",
    "project_execution_status",
    "project_task_result_review_status",
    "project_user_stage",
    "work_requires_owner_review",
]
