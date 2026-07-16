#!/usr/bin/env python3
"""PostgreSQL smoke for privacy-safe production issue aggregation and alerting."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

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
from app.scripts.verify_production_issue_alerts import (
    ReleaseIdentity,
    _create_or_resume_canary,
    _sink_configuration_fingerprint,
    _verify_and_resolve_if_delivered,
)
from app.services.production_issue_monitor import (
    _claim_production_issue_alert_deliveries,
    _deliver_webhook_claim,
    _production_issue_notification_ref_id,
    dispatch_production_issue_alerts,
    record_production_issue,
)


async def main() -> None:
    operation = f"postgres-smoke-{datetime.now(timezone.utc).timestamp()}"
    route = (
        "/api/agents/123e4567-e89b-42d3-a456-426614174000/tasks/123456789?token=secret"
    )
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
            )
            .scalars()
            .all()
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
            )
            .scalars()
            .all()
        )
        assert len(deliveries) == 1
        assert deliveries[0].status == "delivered"
        assert deliveries[0].delivered_at is not None
        assert deliveries[0].attribution_version == 1
        assert deliveries[0].delivered_by_release_id == "local"
        assert deliveries[0].delivered_by_release_commit == "0" * 40
        assert deliveries[0].delivered_by_worker_actor_id is not None
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

    # The release canary is deliberately warning/one-event, below the normal
    # incident threshold. It must still be claimed by the worker, resolved
    # only after its durable sink delivery, and remain at the same epoch when
    # a recovery run reconstructs evidence.
    release_identity = ReleaseIdentity(
        release_id=f"pg-smoke-{uuid.uuid4().hex}",
        version=get_settings().APP_VERSION,
        commit="a" * 40,
    )
    canary_worker_actor_id = uuid.uuid4()
    os.environ["ASTRA_RELEASE_ID"] = release_identity.release_id
    os.environ["ASTRA_RELEASE_VERSION"] = release_identity.version
    os.environ["ASTRA_RELEASE_COMMIT"] = release_identity.commit
    os.environ["ASTRA_ALERT_WORKER_ACTOR_ID"] = str(canary_worker_actor_id)
    canary_settings = SimpleNamespace(
        SAAS_ADMIN_EMAIL=get_settings().SAAS_ADMIN_EMAIL,
        PRODUCTION_ISSUE_ALERT_WEBHOOK_URL="",
        JWT_SECRET_KEY="postgres-smoke-jwt-secret-at-least-32-bytes",
    )
    expected_canary_sinks = frozenset({"notification"})
    canary_config_fingerprint = _sink_configuration_fingerprint(canary_settings)
    canary_issue_id = await _create_or_resume_canary(
        release_identity,
        canary_settings,
        expected_canary_sinks,
        canary_config_fingerprint,
    )
    assert await dispatch_production_issue_alerts() >= 1
    delivered, canary_snapshot = await _verify_and_resolve_if_delivered(
        canary_issue_id,
        expected_canary_sinks,
        release_identity,
        canary_config_fingerprint,
    )
    assert delivered is True
    assert canary_snapshot.status == "resolved"
    assert canary_snapshot.alert_epoch == 1
    assert canary_snapshot.deliveries["notification"]["status"] == "delivered"
    assert canary_snapshot.deliveries["notification"]["attribution_version"] == 1
    assert canary_snapshot.deliveries["notification"]["delivered_by"] == {
        "worker_actor_id": str(canary_worker_actor_id),
        "release_id": release_identity.release_id,
        "release_commit": release_identity.commit,
    }

    resumed_issue_id = await _create_or_resume_canary(
        release_identity,
        canary_settings,
        expected_canary_sinks,
        canary_config_fingerprint,
    )
    assert resumed_issue_id == canary_issue_id
    resumed, resumed_snapshot = await _verify_and_resolve_if_delivered(
        resumed_issue_id,
        expected_canary_sinks,
        release_identity,
        canary_config_fingerprint,
    )
    assert resumed is True
    assert resumed_snapshot.alert_epoch == 1
    assert await dispatch_production_issue_alerts() == 0

    # The same release and the same sink type must not reuse an old canary
    # after the configured alert owner changes. Temporarily relink the seeded
    # owner so enqueueing remains a real PostgreSQL-backed notification path.
    changed_owner_email = f"changed-alert-owner-{uuid.uuid4().hex}@example.test"
    async with async_session() as db:
        owner_identity = await db.get(Identity, owner_identity_id, with_for_update=True)
        assert owner_identity is not None
        original_owner_email = owner_identity.email
        owner_identity.email = changed_owner_email
        await db.commit()
    changed_canary_settings = SimpleNamespace(
        SAAS_ADMIN_EMAIL=changed_owner_email,
        PRODUCTION_ISSUE_ALERT_WEBHOOK_URL="",
        JWT_SECRET_KEY="postgres-smoke-jwt-secret-at-least-32-bytes",
    )
    changed_config_fingerprint = _sink_configuration_fingerprint(changed_canary_settings)
    assert changed_config_fingerprint != canary_config_fingerprint
    changed_canary_issue_id = await _create_or_resume_canary(
        release_identity,
        changed_canary_settings,
        expected_canary_sinks,
        changed_config_fingerprint,
    )
    assert changed_canary_issue_id != canary_issue_id
    async with async_session() as db:
        changed_canary_issue = await db.get(ProductionIssue, changed_canary_issue_id)
        assert changed_canary_issue is not None
        assert changed_canary_issue.status == "open"
        assert changed_canary_issue.event_count == 1
        assert changed_canary_issue.last_metadata["sink_config_fingerprint"] == (
            changed_config_fingerprint
        )
        owner_identity = await db.get(Identity, owner_identity_id, with_for_update=True)
        assert owner_identity is not None
        owner_identity.email = original_owner_email
        await db.delete(changed_canary_issue)
        await db.commit()

    canary_notification_ref_id = _production_issue_notification_ref_id(
        canary_issue_id,
        1,
    )
    async with async_session() as db:
        canary_issue = await db.get(ProductionIssue, canary_issue_id)
        assert canary_issue is not None
        assert canary_issue.status == "resolved"
        assert canary_issue.event_count == 1
        canary_event_count = await db.scalar(
            select(func.count())
            .select_from(ProductionIssueEvent)
            .where(ProductionIssueEvent.issue_id == canary_issue_id)
        )
        assert canary_event_count == 1
        canary_delivery_count = await db.scalar(
            select(func.count())
            .select_from(ProductionIssueAlertDelivery)
            .where(
                ProductionIssueAlertDelivery.issue_id == canary_issue_id,
                ProductionIssueAlertDelivery.alert_epoch == 1,
            )
        )
        assert canary_delivery_count == 1
        canary_titles = list(
            (
                await db.execute(
                    select(Notification.title).where(
                        Notification.ref_id == canary_notification_ref_id,
                        Notification.sender_name == "Astra Monitor",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert canary_titles == ["[演练] 生产告警通道验证"]
        await db.execute(
            Notification.__table__.delete().where(
                Notification.ref_id == canary_notification_ref_id
            )
        )
        await db.delete(canary_issue)
        await db.commit()

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
        stale_delivery.idempotency_key = f"production-issue:{stale_issue_id}:1:webhook"
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
    assert (
        await _deliver_webhook_claim(
            no_webhook_client,
            asyncio.Semaphore(1),
            stale_claim,
            "https://alerts.example.invalid/hook",
        )
        is False
    )
    assert no_webhook_client.called is False
    async with async_session() as db:
        stale_delivery = await db.get(
            ProductionIssueAlertDelivery,
            stale_claim.delivery_id,
        )
        assert stale_delivery is not None
        assert stale_delivery.status == "cancelled"
        assert stale_delivery.delivered_at is None
        assert stale_delivery.delivered_by_worker_actor_id is None
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
