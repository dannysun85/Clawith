"""Read-only inventory for legacy or unsafe media Credits bindings.

The command never settles, refunds, consumes, or rewrites Credits.  It reports
rows that migration 102 can relink safely and rows that require an operator to
inspect exact provider and workspace evidence before a release.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import uuid

from sqlalchemy import func, select

from app.database import async_session
from app.models.media_generation import MediaGenerationTask
from app.models.subscription import CreditBalance, CreditReservation
from app.services.media_generation import UNRESOLVED_MEDIA_STATUSES
from app.services.media_generation import TERMINAL_MEDIA_STATUSES


OPEN_RESERVATION_STATUSES = (
    "reserved",
    "provider_inflight",
    "settlement_ready",
)
LEGACY_MEDIA_ACTIONS = ("image", "audio", "music", "video")


@dataclass(frozen=True, slots=True)
class BindingFinding:
    category: str
    task_id: str | None
    reservation_id: str | None
    tenant_id: str | None
    agent_id: str | None
    task_status: str | None
    reservation_status: str | None


@dataclass(frozen=True, slots=True)
class ReservedBalanceFinding:
    category: str
    tenant_id: str
    expected_reserved: int
    actual_reserved: int | None


def _same_uuid(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (TypeError, ValueError):
        return False


def classify_binding(task, reservation) -> str:
    """Classify one task/reservation pair without changing either row."""

    if reservation is None:
        if getattr(task, "reservation_id", None) is not None:
            return "missing_reservation"
        metadata = getattr(task, "request_metadata", None)
        raw_cost = metadata.get("credit_cost") if isinstance(metadata, dict) else None
        try:
            credit_cost = int(raw_cost)
        except (TypeError, ValueError):
            return "indeterminate_credit_reservation"
        if credit_cost == 0:
            return "zero_cost_no_reservation"
        return (
            "missing_reservation"
            if credit_cost > 0
            else "indeterminate_credit_reservation"
        )
    exact_owner = (
        _same_uuid(task.tenant_id, reservation.tenant_id)
        and _same_uuid(task.agent_id, reservation.agent_id)
        and _same_uuid(task.user_id, reservation.user_id)
    )
    if not exact_owner:
        return "owner_mismatch"
    if reservation.ref_type == "media_task" and _same_uuid(
        reservation.ref_id,
        task.id,
    ):
        binding_category = "canonical"
    elif (
        reservation.ref_type in {None, "minimax_task"}
        and reservation.ref_id is None
    ):
        binding_category = "relinkable_legacy"
    else:
        return "reference_mismatch"

    task_status = str(getattr(task, "status", "") or "")
    reservation_status = str(getattr(reservation, "status", "") or "")
    if task_status in UNRESOLVED_MEDIA_STATUSES:
        if reservation_status not in OPEN_RESERVATION_STATUSES:
            return "unresolved_task_closed_reservation"
    elif task_status in TERMINAL_MEDIA_STATUSES:
        allowed_terminal_reservation_statuses = (
            {"released", "expired"}
            if task_status == "failed"
            else {"finalized"}
        )
        if reservation_status not in allowed_terminal_reservation_statuses:
            return "terminal_task_reservation_state_mismatch"
    else:
        return "unknown_task_status"
    return binding_category


def _finding(category: str, task=None, reservation=None) -> BindingFinding:
    return BindingFinding(
        category=category,
        task_id=str(task.id) if task is not None else None,
        reservation_id=(
            str(reservation.id)
            if reservation is not None
            else str(task.reservation_id)
            if task is not None and task.reservation_id
            else None
        ),
        tenant_id=str(
            task.tenant_id if task is not None else reservation.tenant_id
        )
        if (task is not None and task.tenant_id)
        or (task is None and reservation is not None and reservation.tenant_id)
        else None,
        agent_id=str(task.agent_id if task is not None else reservation.agent_id)
        if (task is not None and task.agent_id)
        or (task is None and reservation is not None and reservation.agent_id)
        else None,
        task_status=str(task.status) if task is not None else None,
        reservation_status=(
            str(reservation.status) if reservation is not None else None
        ),
    )


def classify_reserved_balances(
    balances,
    reservation_totals: dict[uuid.UUID, int],
) -> list[ReservedBalanceFinding]:
    """Compare the materialized hold counter with every open reservation."""

    balance_by_tenant = {balance.tenant_id: balance for balance in balances}
    findings: list[ReservedBalanceFinding] = []
    for tenant_id in sorted(
        set(balance_by_tenant) | set(reservation_totals),
        key=str,
    ):
        balance = balance_by_tenant.get(tenant_id)
        expected = int(reservation_totals.get(tenant_id, 0) or 0)
        actual = int(balance.reserved or 0) if balance is not None else None
        if balance is None:
            findings.append(
                ReservedBalanceFinding(
                    category="missing_credit_balance",
                    tenant_id=str(tenant_id),
                    expected_reserved=expected,
                    actual_reserved=None,
                )
            )
        elif actual != expected:
            findings.append(
                ReservedBalanceFinding(
                    category="reserved_balance_mismatch",
                    tenant_id=str(tenant_id),
                    expected_reserved=expected,
                    actual_reserved=actual,
                )
            )
    return findings


async def inventory(*, detail_limit: int = 100) -> dict:
    """Return a bounded, privacy-safe report from a read-only transaction."""

    async with async_session() as db:
        task_rows = (
            await db.execute(
                select(MediaGenerationTask, CreditReservation)
                .outerjoin(
                    CreditReservation,
                    CreditReservation.id == MediaGenerationTask.reservation_id,
                )
                .order_by(MediaGenerationTask.created_at, MediaGenerationTask.id)
            )
        ).all()

        referenced = (
            select(MediaGenerationTask.id)
            .where(MediaGenerationTask.reservation_id == CreditReservation.id)
            .exists()
        )
        unlinked = list(
            (
                await db.execute(
                    select(CreditReservation)
                    .where(
                        CreditReservation.provider == "minimax",
                        CreditReservation.action.in_(LEGACY_MEDIA_ACTIONS),
                        CreditReservation.status.in_(OPEN_RESERVATION_STATUSES),
                        ~referenced,
                    )
                    .order_by(
                        CreditReservation.created_at,
                        CreditReservation.id,
                    )
                )
            ).scalars().all()
        )
        finalized_unlinked_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(CreditReservation)
                    .where(
                        CreditReservation.provider == "minimax",
                        CreditReservation.action.in_(LEGACY_MEDIA_ACTIONS),
                        CreditReservation.status == "finalized",
                        ~referenced,
                    )
                )
            ).scalar_one()
            or 0
        )
        balances = list(
            (await db.execute(select(CreditBalance))).scalars().all()
        )
        reserved_rows = (
            await db.execute(
                select(
                    CreditReservation.tenant_id,
                    func.sum(CreditReservation.amount),
                )
                .where(
                    CreditReservation.status.in_(OPEN_RESERVATION_STATUSES)
                )
                .group_by(CreditReservation.tenant_id)
            )
        ).all()
        # Explicit rollback documents and enforces the read-only contract even
        # if a future session hook begins a transaction automatically.
        await db.rollback()

    counts: dict[str, int] = {}
    findings: list[BindingFinding] = []
    for task, reservation in task_rows:
        category = classify_binding(task, reservation)
        counts[category] = counts.get(category, 0) + 1
        if (
            category not in {"canonical", "zero_cost_no_reservation"}
            and len(findings) < detail_limit
        ):
            findings.append(_finding(category, task, reservation))
    for reservation in unlinked:
        category = "unlinked_media_reservation"
        counts[category] = counts.get(category, 0) + 1
        if len(findings) < detail_limit:
            findings.append(_finding(category, reservation=reservation))
    counts["finalized_unlinked_legacy"] = finalized_unlinked_count

    reserved_balance_findings = classify_reserved_balances(
        balances,
        {
            tenant_id: int(total or 0)
            for tenant_id, total in reserved_rows
        },
    )
    for finding in reserved_balance_findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1

    blocking_categories = {
        "missing_reservation",
        "indeterminate_credit_reservation",
        "unresolved_task_closed_reservation",
        "terminal_task_reservation_state_mismatch",
        "unknown_task_status",
        "owner_mismatch",
        "reference_mismatch",
        "unlinked_media_reservation",
        "missing_credit_balance",
        "reserved_balance_mismatch",
    }
    blocking = sum(counts.get(category, 0) for category in blocking_categories)
    return {
        "read_only": True,
        "blocking_count": blocking,
        "relinkable_legacy_count": counts.get("relinkable_legacy", 0),
        "canonical_count": counts.get("canonical", 0),
        "zero_cost_no_reservation_count": counts.get(
            "zero_cost_no_reservation",
            0,
        ),
        "counts": dict(sorted(counts.items())),
        "details_truncated": (
            len(findings) >= detail_limit
            or len(reserved_balance_findings) > detail_limit
        ),
        "findings": [asdict(finding) for finding in findings],
        "reserved_balance_findings": [
            asdict(finding)
            for finding in reserved_balance_findings[:detail_limit]
        ],
        "finalized_unlinked_legacy_count": finalized_unlinked_count,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail-limit", type=int, default=100)
    parser.add_argument(
        "--fail-on-blocking",
        action="store_true",
        help="Exit 1 when unsafe or unlinked rows require operator review",
    )
    parser.add_argument(
        "--require-no-legacy",
        action="store_true",
        help="Also exit 1 when a safely relinkable legacy row remains",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    report = await inventory(detail_limit=max(1, min(args.detail_limit, 1000)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = bool(args.fail_on_blocking and report["blocking_count"])
    failed = failed or bool(
        args.require_no_legacy and report["relinkable_legacy_count"]
    )
    return 1 if failed else 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
