"""CLI for exact, dry-run-by-default media incident remediation."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

# Standalone commands must load the full FK registry before ORM flushes.
import app.models.activity_log  # noqa: F401
import app.models.agent  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.llm  # noqa: F401
import app.models.media_generation  # noqa: F401
import app.models.notification  # noqa: F401
import app.models.subscription  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.user  # noqa: F401
from app.services.media_incident_remediation import remediate_media_tasks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", action="append", required=True, help="Exact media task UUID; repeat for more")
    parser.add_argument("--incident-key", required=True, help="Stable incident identifier for audit")
    parser.add_argument("--expected-tenant-id", help="Fail closed if any task belongs to another tenant")
    parser.add_argument("--apply", action="store_true", help="Apply changes; omitted means read-only dry-run")
    return parser


async def _run(args: argparse.Namespace) -> None:
    result = await remediate_media_tasks(
        task_ids=tuple(uuid.UUID(value) for value in args.task_id),
        incident_key=args.incident_key,
        expected_tenant_id=(uuid.UUID(args.expected_tenant_id) if args.expected_tenant_id else None),
        apply=args.apply,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
