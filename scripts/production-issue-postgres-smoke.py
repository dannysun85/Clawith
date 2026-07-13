#!/usr/bin/env python3
"""PostgreSQL smoke for privacy-safe production issue aggregation and alerting."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.database import async_session
from app.models.agent import Agent  # noqa: F401 - register FK target metadata
from app.models.production_issue import ProductionIssue, ProductionIssueEvent
from app.models.tenant import Tenant  # noqa: F401 - register FK target metadata
from app.models.user import User  # noqa: F401 - register FK target metadata
from app.services.production_issue_monitor import (
    dispatch_production_issue_alerts,
    record_production_issue,
)


async def main() -> None:
    operation = f"postgres-smoke-{datetime.now(timezone.utc).timestamp()}"
    route = "/api/agents/123e4567-e89b-42d3-a456-426614174000/tasks/123456789?token=secret"
    metadata = {
        "component": "ProductionIssuePostgresSmoke",
        "provider": "minimax",
        "model": "sk-not-a-real-key-but-must-be-redacted",
        "status_code": 503,
        "prompt": "must never be persisted",
        "content": "must never be persisted",
        "api_key": "must never be persisted",
    }

    first_id = await record_production_issue(
        source="release_smoke",
        category="database",
        summary="Synthetic release-gate failure",
        error_code="synthetic_503",
        route=route,
        operation=operation,
        trace_id="release-smoke-1",
        metadata=metadata,
    )
    second_id = await record_production_issue(
        source="release_smoke",
        category="database",
        summary="Synthetic release-gate failure",
        error_code="synthetic_503",
        route=route,
        operation=operation,
        trace_id="release-smoke-2",
        metadata=metadata,
    )

    assert first_id is not None and second_id == first_id
    async with async_session() as db:
        issue = await db.get(ProductionIssue, first_id)
        assert issue is not None
        event_count = await db.scalar(
            select(func.count())
            .select_from(ProductionIssueEvent)
            .where(ProductionIssueEvent.issue_id == first_id)
        )
        assert issue.event_count == 2
        assert event_count == 2
        assert issue.route == "/api/agents/{uuid}/tasks/{id}"
        assert issue.last_metadata == {
            "component": "ProductionIssuePostgresSmoke",
            "provider": "minimax",
            "model": "[redacted]",
            "status_code": 503,
        }

        issue.status = "resolved"
        issue.resolved_at = datetime.now(timezone.utc)
        issue.acknowledged_at = datetime.now(timezone.utc)
        issue.alerted_at = datetime.now(timezone.utc)
        await db.commit()

    reopened_id = await record_production_issue(
        source="release_smoke",
        category="database",
        summary="Synthetic release-gate failure",
        error_code="synthetic_503",
        route=route,
        operation=operation,
        trace_id="release-smoke-3",
        metadata=metadata,
    )
    assert reopened_id == first_id

    async with async_session() as db:
        issue = await db.get(ProductionIssue, first_id)
        assert issue is not None
        assert issue.status == "open"
        assert issue.event_count == 3
        assert issue.resolved_at is None
        assert issue.acknowledged_at is None
        assert issue.alerted_at is None

    alerted_count = await dispatch_production_issue_alerts()
    assert alerted_count >= 1

    async with async_session() as db:
        issue = await db.get(ProductionIssue, first_id)
        assert issue is not None and issue.alerted_at is not None
        await db.delete(issue)
        await db.commit()
        remaining_events = await db.scalar(
            select(func.count())
            .select_from(ProductionIssueEvent)
            .where(ProductionIssueEvent.issue_id == first_id)
        )
        assert remaining_events == 0

    print("Production issue PostgreSQL aggregation and alert smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
