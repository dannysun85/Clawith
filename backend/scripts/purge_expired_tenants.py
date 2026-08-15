#!/usr/bin/env python3
"""Run a bounded tenant-purge batch with sanitized receipts.

Dry-run is the default.  Physical execution additionally requires the service
guard (development/test, loopback database, dedicated database name, explicit
environment flag, and fixture tenant slug) plus the CLI confirmation flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.database import async_session, engine
from app.services.tenant_purge import (
    TenantPurgeError,
    dry_run_tenant_purge,
    execute_tenant_purge,
    list_due_tenant_ids,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan or execute due tenant purges")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="execute after a fresh dry-run (local fixture guard still applies)",
    )
    parser.add_argument(
        "--confirm-local-fixture-purge",
        action="store_true",
        help="required with --execute to prevent accidental destructive invocation",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.execute and not args.confirm_local_fixture_purge:
        raise SystemExit("--execute requires --confirm-local-fixture-purge")

    async with async_session() as db:
        tenant_ids = await list_due_tenant_ids(db, batch_size=args.batch_size)
        await db.rollback()

    outcomes: list[dict[str, object]] = []
    for tenant_id in tenant_ids:
        try:
            async with async_session() as db:
                plan = await dry_run_tenant_purge(db, tenant_id)
            outcome: dict[str, object] = {
                "tenant_id": str(tenant_id),
                "dry_run": "passed",
                "rows_total": plan["rows_total"],
                "plan_digest": plan["plan_digest"],
            }
            if args.execute:
                receipt = await execute_tenant_purge(tenant_id)
                outcome.update(
                    {
                        "execution": receipt["status"],
                        "receipt_hash": receipt["receipt_hash"],
                    }
                )
            outcomes.append(outcome)
        except TenantPurgeError as exc:
            outcomes.append(
                {
                    "tenant_id": str(tenant_id),
                    "status": "blocked",
                    "error_code": exc.code,
                }
            )

    print(
        json.dumps(
            {
                "mode": "execute" if args.execute else "dry_run",
                "selected": len(tenant_ids),
                "outcomes": outcomes,
            },
            sort_keys=True,
        )
    )
    return 0 if all(item.get("status") != "blocked" for item in outcomes) else 2


def main() -> None:
    args = _parser().parse_args()

    async def run() -> int:
        try:
            return await _run(args)
        finally:
            await engine.dispose()

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
