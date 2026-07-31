"""User-facing work stages projected from authoritative product facts."""

from __future__ import annotations


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
    if task_status == "done" or execution_status == "completed":
        return "completed"
    return "task"


__all__ = ["TERMINAL_RUN_EVENTS", "project_execution_status", "project_user_stage"]
