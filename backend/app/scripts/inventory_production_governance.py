"""Read-only, identity-free release governance inventory.

The report intentionally contains aggregates only.  It does not print tenant,
user, Agent, order, reservation, task, issue, trace, message, Memory, prompt,
credential, or provider receipt identifiers.  Exact records are handled only
through separately authorized tenant-fenced remediation APIs.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session
from app.services.ceo_migration import build_ceo_migration_preview


SCHEMA_VERSION = "1.12.0"
PROHIBITED_IDENTITY_KEYS = frozenset(
    {
        "tenant_id",
        "user_id",
        "agent_id",
        "order_id",
        "reservation_id",
        "task_id",
        "issue_id",
        "trace_id",
        "fingerprint",
        "name",
        "email",
        "summary",
        "route",
        "evidence_ref",
        "reason",
    }
)


def summarize_ceo_previews(previews: list[dict]) -> dict:
    classifications = Counter(str(item.get("classification") or "unknown") for item in previews)
    return {
        "active_tenants_scanned": len(previews),
        "classification_counts": dict(sorted(classifications.items())),
        "candidate_count": sum(len(item.get("candidates") or []) for item in previews),
        "warning_count": sum(len(item.get("warnings") or []) for item in previews),
        "automatic_adoption_allowed": False,
        "automatic_archive_allowed": False,
    }


def assert_identity_free_report(value: object, *, path: str = "report") -> None:
    """Fail before printing if a future change adds an identity-bearing field."""

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in PROHIBITED_IDENTITY_KEYS or normalized.endswith("_id"):
                raise ValueError(f"identity-bearing report key is prohibited at {path}.{key}")
            assert_identity_free_report(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            assert_identity_free_report(nested, path=f"{path}[{index}]")


async def inventory() -> dict:
    settings = get_settings()
    async with async_session() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await db.execute(text("SET LOCAL statement_timeout = '30s'"))

        tenant_ids = list(
            (
                await db.execute(
                    text("SELECT id FROM tenants WHERE is_active IS TRUE ORDER BY id")
                )
            ).scalars()
        )

        media_debt = [
            dict(row)
            for row in (
                await db.execute(
                    text(
                        """
                        SELECT
                          status,
                          COALESCE(action, '<none>') AS action,
                          COALESCE(modality, '<none>') AS modality,
                          COALESCE(ref_type, '<none>') AS reference_type,
                          COALESCE(provider, '<none>') AS provider,
                          CASE
                            WHEN created_at > now() - interval '1 day' THEN '<=1d'
                            WHEN created_at > now() - interval '7 days' THEN '1d-7d'
                            WHEN created_at > now() - interval '30 days' THEN '7d-30d'
                            ELSE '>30d'
                          END AS age_bucket,
                          count(*)::int AS records,
                          COALESCE(sum(amount), 0)::bigint AS held_credits
                        FROM credit_reservations
                        WHERE status IN ('provider_inflight', 'settlement_ready')
                        GROUP BY 1,2,3,4,5,6
                        ORDER BY 1,2,3,4,5,6
                        """
                    )
                )
            ).mappings()
        ]

        ledger_integrity = dict(
            (
                await db.execute(
                    text(
                        """
                        WITH held AS (
                          SELECT tenant_id, sum(amount)::bigint AS expected_reserved
                          FROM credit_reservations
                          WHERE status IN ('reserved', 'provider_inflight', 'settlement_ready')
                          GROUP BY tenant_id
                        ), compared AS (
                          SELECT
                            COALESCE(cb.tenant_id, held.tenant_id) AS compared_tenant,
                            cb.tenant_id IS NULL AS missing_balance,
                            COALESCE(cb.reserved, 0)::bigint AS actual_reserved,
                            COALESCE(held.expected_reserved, 0)::bigint AS expected_reserved
                          FROM credit_balances cb
                          FULL OUTER JOIN held ON held.tenant_id = cb.tenant_id
                        )
                        SELECT
                          count(*) FILTER (WHERE missing_balance)::int AS missing_balances,
                          count(*) FILTER (
                            WHERE actual_reserved <> expected_reserved
                          )::int AS mismatched_balances,
                          COALESCE(sum(abs(actual_reserved - expected_reserved)), 0)::bigint
                            AS absolute_reserved_drift
                        FROM compared
                        """
                    )
                )
            ).mappings().one()
        )

        manual_pending = [
            dict(row)
            for row in (
                await db.execute(
                    text(
                        """
                        SELECT
                          type,
                          CASE
                            WHEN created_at > now() - interval '2 hours' THEN '<=2h'
                            WHEN created_at > now() - interval '7 days' THEN '2h-7d'
                            WHEN created_at > now() - interval '30 days' THEN '7d-30d'
                            ELSE '>30d'
                          END AS age_bucket,
                          count(*)::int AS records,
                          COALESCE(sum(amount_cents), 0)::bigint AS amount_cents
                        FROM payment_orders
                        WHERE provider = 'manual' AND status = 'pending'
                        GROUP BY 1,2
                        ORDER BY 1,2
                        """
                    )
                )
            ).mappings()
        ]

        operator_decisions = [
            dict(row)
            for row in (
                await db.execute(
                    text(
                        """
                        SELECT disposition, resulting_status, count(*)::int AS records
                        FROM payment_order_operator_decisions
                        GROUP BY disposition, resulting_status
                        ORDER BY disposition, resulting_status
                        """
                    )
                )
            ).mappings()
        ]

        open_issues = [
            dict(row)
            for row in (
                await db.execute(
                    text(
                        """
                        SELECT
                          source,
                          category,
                          severity,
                          COALESCE(error_code, '<none>') AS error_code,
                          COALESCE(operation, '<none>') AS operation,
                          COALESCE(release_version, '<none>') AS release_version,
                          count(*)::int AS issue_rollups,
                          COALESCE(sum(event_count), 0)::bigint AS events,
                          CASE
                            WHEN max(last_seen_at) > now() - interval '1 day' THEN '<=1d'
                            WHEN max(last_seen_at) > now() - interval '7 days' THEN '1d-7d'
                            ELSE '>7d'
                          END AS last_seen_bucket
                        FROM production_issues
                        WHERE status = 'open'
                        GROUP BY 1,2,3,4,5,6
                        ORDER BY events DESC, issue_rollups DESC
                        """
                    )
                )
            ).mappings()
        ]

        automation_intent = dict(
            (
                await db.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM agents
                           WHERE deleted_at IS NULL AND heartbeat_enabled IS TRUE)::int
                            AS heartbeat_agents,
                          (SELECT count(*) FROM okr_settings WHERE enabled IS TRUE)::int
                            AS okr_tenants,
                          (SELECT count(*) FROM okr_settings
                           WHERE daily_report_enabled IS TRUE)::int AS daily_report_tenants,
                          (SELECT count(*) FROM okr_settings
                           WHERE weekly_report_enabled IS TRUE)::int AS weekly_report_tenants,
                          (SELECT count(*) FROM agent_triggers
                           WHERE is_enabled IS TRUE AND is_system IS TRUE)::int
                            AS active_system_triggers
                        """
                    )
                )
            ).mappings().one()
        )

        ceo_previews = [
            await build_ceo_migration_preview(db, tenant_id=tenant_id)
            for tenant_id in tenant_ids
        ]
        await db.rollback()

    report = {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "identity_free": True,
        "media_provider_debt": media_debt,
        "credit_ledger_integrity": ledger_integrity,
        "manual_pending_orders": manual_pending,
        "manual_order_operator_decisions": operator_decisions,
        "open_production_issues": open_issues,
        "ceo_migration": summarize_ceo_previews(ceo_previews),
        "suspended_automation": {
            **automation_intent,
            "platform_heartbeat_enabled": bool(settings.HEARTBEAT_ENABLED),
            "platform_okr_automation_enabled": bool(settings.OKR_AUTOMATION_ENABLED),
            "tenant_intent_mutated": False,
        },
    }
    assert_identity_free_report(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on-ledger-drift",
        action="store_true",
        help="Exit 1 if materialized reserved Credits differ from open holds.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    report = await inventory()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    ledger = report["credit_ledger_integrity"]
    has_drift = bool(
        ledger["missing_balances"]
        or ledger["mismatched_balances"]
        or ledger["absolute_reserved_drift"]
    )
    return 1 if args.fail_on_ledger_drift and has_drift else 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
