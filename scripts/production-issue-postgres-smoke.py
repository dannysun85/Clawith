#!/usr/bin/env python3
"""PostgreSQL smoke for privacy-safe production issue aggregation and alerting."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent  # noqa: F401 - register FK target metadata
from app.models.notification import Notification
from app.models.production_issue import (
    ProductionIssue,
    ProductionIssueAlertDelivery,
    ProductionIssueEvent,
)
from app.models.tenant import Tenant  # noqa: F401 - register FK target metadata
from app.models.user import Identity, User
from app.services.production_issue_monitor import (
    _claim_production_issue_alert_deliveries,
    _deliver_webhook_claim,
    _production_issue_notification_ref_id,
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
        assert issue.alert_epoch == 2

        delivery_rows = list(
            (
                await db.execute(
                    select(ProductionIssueAlertDelivery).where(
                        ProductionIssueAlertDelivery.issue_id == first_id,
                        ProductionIssueAlertDelivery.alert_epoch == 2,
                    )
                )
            ).scalars().all()
        )
        assert [delivery.sink for delivery in delivery_rows] == ["notification"]
        assert delivery_rows[0].status == "pending"
        assert delivery_rows[0].payload_snapshot["issue_id"] == str(first_id)
        assert "prompt" not in str(delivery_rows[0].payload_snapshot).lower()

        # Give the smoke a real durable in-app sink.  The production release
        # separately verifies its external webhook configuration.
        owner_email = get_settings().SAAS_ADMIN_EMAIL.strip().lower()
        owner_identity_id = await db.scalar(
            select(Identity.id).where(func.lower(Identity.email) == owner_email)
        )
        if owner_identity_id is None:
            owner_identity_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"astra-production-smoke:{owner_email}",
            )
            await db.execute(
                pg_insert(Identity.__table__)
                .values(
                    id=owner_identity_id,
                    email=owner_email,
                    is_active=True,
                    is_platform_admin=True,
                    email_verified=True,
                )
                .on_conflict_do_nothing(index_elements=[Identity.__table__.c.id])
            )
        owner_user = await db.get(
            User,
            uuid.UUID("07500000-0000-4000-8000-000000000060"),
        )
        assert owner_user is not None
        owner_user.identity_id = owner_identity_id
        await db.commit()

    alerted_count = await dispatch_production_issue_alerts()
    assert alerted_count >= 1
    notification_ref_id = _production_issue_notification_ref_id(first_id, 2)

    async with async_session() as db:
        issue = await db.get(ProductionIssue, first_id)
        assert issue is not None and issue.alerted_at is not None
        assert issue.alert_notification_sent_at is not None
        assert issue.alert_attempts == 0
        deliveries = list(
            (
                await db.execute(
                    select(ProductionIssueAlertDelivery).where(
                        ProductionIssueAlertDelivery.issue_id == first_id,
                        ProductionIssueAlertDelivery.alert_epoch == 2,
                    )
                )
            ).scalars().all()
        )
        assert len(deliveries) == 1
        assert deliveries[0].status == "delivered"
        assert deliveries[0].delivered_at is not None
        notification_count = await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.ref_id == notification_ref_id,
                Notification.sender_name == "Astra Monitor",
                Notification.link == "/admin/saas?tab=production-issues",
            )
        )
        assert notification_count == 1

    # A second dispatcher pass must not duplicate the durable in-app delivery.
    assert await dispatch_production_issue_alerts() == 0

    # A claim that becomes obsolete before its webhook send must be fenced by
    # the parent Issue lock and must never reach the HTTP client.
    stale_operation = f"{operation}-stale-webhook"
    stale_issue_id = await record_production_issue(
        source="release_smoke",
        category="runtime",
        summary="Synthetic obsolete webhook",
        severity="critical",
        error_code="synthetic_stale_webhook",
        operation=stale_operation,
        metadata={"component": "ProductionIssuePostgresSmoke"},
    )
    assert stale_issue_id is not None
    async with async_session() as db:
        stale_delivery = (
            await db.execute(
                select(ProductionIssueAlertDelivery).where(
                    ProductionIssueAlertDelivery.issue_id == stale_issue_id,
                )
            )
        ).scalar_one()
        stale_delivery.sink = "webhook"
        stale_delivery.idempotency_key = (
            f"production-issue:{stale_issue_id}:1:webhook"
        )
        await db.commit()

    stale_claims = await _claim_production_issue_alert_deliveries()
    stale_claim = next(
        claim for claim in stale_claims if claim.issue_id == stale_issue_id
    )
    async with async_session() as db:
        stale_issue = await db.get(
            ProductionIssue,
            stale_issue_id,
            with_for_update=True,
        )
        assert stale_issue is not None
        stale_issue.status = "resolved"
        stale_issue.resolved_at = datetime.now(timezone.utc)
        await db.commit()

    class NoWebhookClient:
        called = False

        async def post(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("obsolete webhook reached HTTP client")

    no_webhook_client = NoWebhookClient()
    assert await _deliver_webhook_claim(
        no_webhook_client,
        asyncio.Semaphore(1),
        stale_claim,
        "https://alerts.example.invalid/hook",
    ) is False
    assert no_webhook_client.called is False
    async with async_session() as db:
        stale_delivery = await db.get(
            ProductionIssueAlertDelivery,
            stale_claim.delivery_id,
        )
        assert stale_delivery is not None
        assert stale_delivery.status == "delivered"
        stale_issue = await db.get(ProductionIssue, stale_issue_id)
        assert stale_issue is not None
        await db.delete(stale_issue)
        await db.commit()

    async with async_session() as db:
        notification_count = await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.ref_id == notification_ref_id,
                Notification.sender_name == "Astra Monitor",
                Notification.link == "/admin/saas?tab=production-issues",
            )
        )
        assert notification_count == 1
        issue = await db.get(ProductionIssue, first_id)
        assert issue is not None
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
