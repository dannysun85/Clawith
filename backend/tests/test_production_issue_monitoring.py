import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import production_issues
from app.schemas.production_issue import ClientIssueReportIn
from app.services import production_issue_monitor


ROOT = Path(__file__).parents[2]


def test_route_normalization_removes_queries_and_high_cardinality_ids():
    route = (
        "/api/agents/123e4567-e89b-42d3-a456-426614174000/"
        "tasks/123456789?token=must-not-survive"
    )

    assert production_issue_monitor.normalize_issue_route(route) == (
        "/api/agents/{uuid}/tasks/{id}"
    )


def test_monitor_index_fields_redact_secret_shaped_values():
    assert production_issue_monitor.normalize_issue_route(
        "/api/jobs/sk-secret-value-123456?token=also-secret"
    ) == "/api/jobs/[redacted]"
    assert production_issue_monitor._safe_operational_text(
        "WebSocket sk-secret-value-123456\nclose",
        100,
    ) == "WebSocket [redacted] close"


def test_monitor_metadata_is_allowlisted_and_credentials_are_redacted():
    clean = production_issue_monitor.sanitize_issue_metadata({
        "component": "AgentDetailPage",
        "provider": "minimax",
        "error_type": "Bearer secret-token-value",
        "model": "sk-123456789abcdef",
        "prompt": "customer private prompt",
        "content": "customer private content",
        "api_key": "should-not-survive",
        "token": "should-not-survive",
        "status_code": 503,
    })

    assert clean == {
        "component": "AgentDetailPage",
        "provider": "minimax",
        "error_type": "[redacted]",
        "model": "[redacted]",
        "status_code": 503,
    }


def test_client_report_contract_rejects_message_and_identity_fields():
    with pytest.raises(ValidationError):
        ClientIssueReportIn.model_validate({
            "category": "api",
            "error_code": "http_500",
            "summary": "raw server response",
            "tenant_id": str(uuid.uuid4()),
        })


@pytest.mark.parametrize(
    "payload",
    [
        {"category": "api", "error_code": "customer private sentence"},
        {
            "category": "runtime",
            "error_code": "WindowError",
            "operation": "render private customer content",
        },
        {
            "category": "runtime",
            "error_code": "WindowError",
            "metadata": {"component": "customer private prompt"},
        },
        {
            "category": "runtime",
            "error_code": "WindowError",
            "metadata": {"prompt": "must never be accepted"},
        },
        {
            "category": "api",
            "error_code": "http_500",
            "route": "/api/customer private prompt",
        },
    ],
)
def test_client_report_contract_rejects_free_form_diagnostic_text(payload):
    with pytest.raises(ValidationError):
        ClientIssueReportIn.model_validate(payload)


def test_client_report_contract_accepts_agent_context_but_not_tenant_override():
    agent_id = uuid.uuid4()

    report = ClientIssueReportIn.model_validate({
        "category": "websocket",
        "error_code": "close_1006",
        "agent_id": str(agent_id),
    })
    assert report.agent_id == agent_id

    with pytest.raises(ValidationError):
        ClientIssueReportIn.model_validate({
            "category": "websocket",
            "error_code": "close_1006",
            "agent_id": str(agent_id),
            "tenant_id": str(uuid.uuid4()),
        })


@pytest.mark.asyncio
async def test_client_agent_context_uses_the_product_access_policy(monkeypatch):
    requested_agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(tenant_id=tenant_id)

    async def allow_agent(db, checked_user, checked_agent_id):
        assert checked_user is user
        assert checked_agent_id == requested_agent_id
        return SimpleNamespace(id=requested_agent_id), "use"

    monkeypatch.setattr(production_issues, "check_agent_access", allow_agent)

    assert await production_issues._authorized_client_agent_id(
        object(), user, requested_agent_id
    ) == requested_agent_id


@pytest.mark.asyncio
async def test_unauthorized_client_agent_context_is_dropped(monkeypatch):
    async def deny_agent(_db, _user, _agent_id):
        raise HTTPException(status_code=403, detail="No access to this agent")

    monkeypatch.setattr(production_issues, "check_agent_access", deny_agent)

    assert await production_issues._authorized_client_agent_id(
        object(), SimpleNamespace(tenant_id=uuid.uuid4()), uuid.uuid4()
    ) is None


@pytest.mark.asyncio
async def test_client_issue_report_rate_limit_is_enforced_before_persistence(monkeypatch):
    persist = AsyncMock()
    authorize = AsyncMock()
    monkeypatch.setattr(production_issues, "record_production_issue", persist)
    monkeypatch.setattr(production_issues, "_authorized_client_agent_id", authorize)
    monkeypatch.setattr(
        production_issues,
        "_record_and_count_client_reports",
        AsyncMock(return_value=production_issues.CLIENT_REPORT_RATE_LIMIT + 1),
    )

    with pytest.raises(HTTPException) as exc_info:
        await production_issues.report_client_issue(
            ClientIssueReportIn.model_validate({
                "category": "api",
                "error_code": "http_500",
            }),
            SimpleNamespace(state=SimpleNamespace(trace_id="abc123")),
            SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
            object(),
        )

    assert exc_info.value.status_code == 429
    authorize.assert_not_awaited()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_issue_report_rate_counter_uses_a_bounded_redis_window(monkeypatch):
    calls = []

    class Redis:
        async def eval(self, *args):
            calls.append(args)
            return 12

    monkeypatch.setattr(production_issues, "get_redis", AsyncMock(return_value=Redis()))

    count = await production_issues._record_and_count_client_reports(uuid.uuid4())

    assert count == 12
    assert len(calls) == 1
    script, key_count, key, _cutoff, _now, member, limit, ttl = calls[0]
    assert key_count == 1
    assert key.startswith("production-issue-report:rate:")
    assert member.count(":") == 1
    assert limit == production_issues.CLIENT_REPORT_RATE_LIMIT
    assert ttl == (
        production_issues.CLIENT_REPORT_RATE_WINDOW_SECONDS * 2
    )
    assert "if count >= limit" in script
    assert script.index("if count >= limit") < script.index("redis.call('ZADD'")


def test_issue_fingerprint_groups_same_failure_across_tenants():
    first = production_issue_monitor.issue_fingerprint(
        source="client_api",
        category="api",
        error_code="http_500",
        route="/api/agents/123e4567-e89b-42d3-a456-426614174000",
        operation="GET",
    )
    second = production_issue_monitor.issue_fingerprint(
        source="client_api",
        category="api",
        error_code="http_500",
        route="/api/agents/625b6671-e7fd-45e6-a9f2-d12d0e6fb989?debug=1",
        operation="get",
    )

    assert first == second


@pytest.mark.parametrize(
    ("status", "severity", "event_count", "alerted", "expected"),
    [
        ("open", "critical", 1, None, True),
        ("open", "error", 3, None, True),
        ("open", "error", 2, None, False),
        ("acknowledged", "critical", 10, None, False),
        ("open", "critical", 10, datetime.now(timezone.utc), False),
    ],
)
def test_alert_gate_is_first_alert_only(status, severity, event_count, alerted, expected):
    issue = SimpleNamespace(
        status=status,
        severity=severity,
        event_count=event_count,
        alerted_at=alerted,
    )

    assert production_issue_monitor.issue_requires_alert(issue, threshold=3) is expected


def test_warning_issue_alert_stays_out_of_error_log_stream():
    assert production_issue_monitor._production_issue_alert_log_level("warning") == "warning"
    assert production_issue_monitor._production_issue_alert_log_level("error") == "error"
    assert production_issue_monitor._production_issue_alert_log_level("critical") == "error"


@pytest.mark.asyncio
async def test_issue_capture_stores_only_sanitized_occurrence_metadata(monkeypatch):
    expected_id = uuid.uuid4()

    class Result:
        def scalar_one(self):
            return SimpleNamespace(
                id=expected_id,
                status="open",
                severity="error",
                event_count=1,
                alerted_at=None,
            )

    class Session:
        def __init__(self):
            self.added = []
            self.statement = None
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement):
            self.statement = statement
            return Result()

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            self.committed = True

    session = Session()
    monkeypatch.setattr(production_issue_monitor, "async_session", lambda: session)
    monkeypatch.setattr(
        production_issue_monitor,
        "get_settings",
        lambda: SimpleNamespace(
            APP_VERSION="1.10.8",
            PRODUCTION_ISSUE_ALERT_THRESHOLD=3,
        ),
    )

    issue_id = await production_issue_monitor.record_production_issue(
        source="client_api",
        category="api",
        summary="Client observed an API operation failure",
        error_code="http_503",
        route="/api/agents/123e4567-e89b-42d3-a456-426614174000?prompt=private",
        operation="GET",
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        metadata={"component": "fetch", "status_code": 503, "prompt": "private"},
    )

    assert issue_id == expected_id
    assert session.committed is True
    assert len(session.added) == 1
    event = session.added[0]
    assert event.route == "/api/agents/{uuid}"
    assert event.metadata_json == {"component": "fetch", "status_code": 503}


@pytest.mark.asyncio
async def test_issue_capture_derives_tenant_from_agent(monkeypatch):
    expected_issue_id = uuid.uuid4()
    expected_tenant_id = uuid.uuid4()

    class Result:
        def scalar_one(self):
            return SimpleNamespace(
                id=expected_issue_id,
                status="open",
                severity="error",
                event_count=1,
                alerted_at=None,
            )

    class Session:
        def __init__(self):
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return expected_tenant_id

        async def execute(self, _statement):
            return Result()

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            return None

    session = Session()
    monkeypatch.setattr(production_issue_monitor, "async_session", lambda: session)
    monkeypatch.setattr(
        production_issue_monitor,
        "get_settings",
        lambda: SimpleNamespace(
            APP_VERSION="1.10.8",
            PRODUCTION_ISSUE_ALERT_THRESHOLD=3,
        ),
    )
    agent_id = uuid.uuid4()

    issue_id = await production_issue_monitor.record_production_issue(
        source="channel_connector",
        category="channel",
        summary="Feishu channel connect failed",
        error_code="ClientException",
        operation="feishu.connect",
        agent_id=agent_id,
    )

    assert issue_id == expected_issue_id
    assert len(session.added) == 1
    assert session.added[0].agent_id == agent_id
    assert session.added[0].tenant_id == expected_tenant_id


def test_worker_and_production_compose_enable_monitoring_contract():
    main_source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/astra-poc/docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "start_production_issue_monitor_daemon" in main_source
    assert '("production_issue_monitor", start_production_issue_monitor_daemon())' in main_source
    assert "PRODUCTION_ISSUE_MONITOR_ENABLED" in compose
    assert "PRODUCTION_ISSUE_ALERT_THRESHOLD" in compose


@pytest.mark.asyncio
async def test_monitor_exits_after_consecutive_failures_for_worker_restart(
    monkeypatch,
):
    attempts = 0

    async def fail_flush():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database unavailable")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(
        production_issue_monitor,
        "get_settings",
        lambda: SimpleNamespace(
            APP_VERSION="1.10.12",
            PRODUCTION_ISSUE_MONITOR_INTERVAL_SECONDS=10,
        ),
    )
    monkeypatch.setattr(
        production_issue_monitor,
        "PRODUCTION_ISSUE_MONITOR_MAX_CONSECUTIVE_FAILURES",
        2,
    )
    monkeypatch.setattr(
        production_issue_monitor,
        "flush_failed_production_issue_captures",
        fail_flush,
    )
    monkeypatch.setattr(production_issue_monitor.asyncio, "sleep", no_wait)

    with pytest.raises(RuntimeError, match="failure threshold"):
        await production_issue_monitor.start_production_issue_monitor_daemon()

    assert attempts == 2


def test_first_alert_creates_a_privacy_safe_saas_owner_notification():
    issue = SimpleNamespace(
        id=uuid.uuid4(),
        severity="critical",
        summary="Model provider operation failed",
        event_count=3,
        route="/api/agents/{uuid}",
        operation="chat",
        category="llm_provider",
    )

    notification = production_issue_monitor._production_issue_notification(issue, uuid.uuid4())

    assert notification.title == "[严重] 生产问题告警"
    assert notification.link == "/admin/saas?tab=production-issues"
    assert "prompt" not in notification.body.lower()
    assert str(issue.id) not in notification.body


def test_warning_alert_notification_is_not_labeled_as_error():
    issue = SimpleNamespace(
        id=uuid.uuid4(),
        severity="warning",
        summary="Provider media plan is temporarily exhausted",
        event_count=3,
        route=None,
        operation="video",
        category="media",
    )

    notification = production_issue_monitor._production_issue_notification(issue, uuid.uuid4())

    assert notification.title == "[警告] 生产问题告警"


def test_alert_notification_uses_the_claimed_epoch_snapshot():
    issue = SimpleNamespace(
        id=uuid.uuid4(),
        alert_epoch=7,
        severity="critical",
        summary="Mutated live summary",
        event_count=99,
        route="/mutated-live-route",
        operation="mutated.operation",
        category="mutated-category",
    )
    snapshot = {
        "alert_epoch": 2,
        "severity": "warning",
        "summary": "Frozen provider warning",
        "event_count": 3,
        "route": "/api/frozen/{uuid}",
        "operation": "frozen.operation",
        "category": "llm_provider",
    }

    notification = production_issue_monitor._production_issue_notification(
        issue,
        uuid.uuid4(),
        payload=snapshot,
        alert_epoch=2,
    )

    assert notification.title == "[警告] 生产问题告警"
    assert notification.body == "Frozen provider warning · 3 次 · /api/frozen/{uuid}"
    assert notification.ref_id == (
        production_issue_monitor._production_issue_notification_ref_id(issue.id, 2)
    )
    assert "Mutated" not in notification.body


@pytest.mark.asyncio
async def test_alert_finalizer_locks_issue_before_delivery(monkeypatch):
    issue_id = uuid.uuid4()
    delivery_id = uuid.uuid4()
    claim_token = uuid.uuid4()
    issue = SimpleNamespace(
        id=issue_id,
        status="open",
        alert_epoch=1,
        alerted_at=None,
        alert_attempts=0,
        alert_next_attempt_at=None,
        alert_last_error_code=None,
        alert_notification_sent_at=None,
    )
    delivery = SimpleNamespace(
        id=delivery_id,
        issue_id=issue_id,
        alert_epoch=1,
        sink="webhook",
        status="delivering",
        attempts=1,
        claim_token=claim_token,
        claimed_at=datetime.now(timezone.utc),
        delivered_at=None,
        next_attempt_at=None,
        last_error_code=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return delivery

    class Session:
        def __init__(self):
            self.events = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, object_id, *, with_for_update=False):
            self.events.append(("issue", model, object_id, with_for_update))
            return issue

        async def execute(self, _statement):
            self.events.append(("delivery",))
            return Result()

        async def flush(self):
            self.events.append(("flush",))

        async def scalar(self, _statement):
            return 0

        async def commit(self):
            self.events.append(("commit",))

    session = Session()
    monkeypatch.setattr(production_issue_monitor, "async_session", lambda: session)
    claim = production_issue_monitor.AlertDeliveryClaim(
        delivery_id=delivery_id,
        issue_id=issue_id,
        alert_epoch=1,
        sink="webhook",
        idempotency_key="production-issue:test:1:webhook",
        claim_token=claim_token,
        payload={},
    )

    assert await production_issue_monitor._finalize_alert_delivery(
        claim,
        success=True,
    ) is True
    assert [event[0] for event in session.events[:2]] == ["issue", "delivery"]


@pytest.mark.asyncio
async def test_obsolete_notification_delivery_completes_without_notifying(monkeypatch):
    issue_id = uuid.uuid4()
    delivery_id = uuid.uuid4()
    claim_token = uuid.uuid4()
    issue = SimpleNamespace(
        id=issue_id,
        status="open",
        alert_epoch=2,
        alerted_at=None,
        alert_attempts=0,
        alert_next_attempt_at=None,
        alert_last_error_code=None,
        alert_notification_sent_at=None,
    )
    delivery = SimpleNamespace(
        id=delivery_id,
        issue_id=issue_id,
        alert_epoch=1,
        sink="notification",
        status="delivering",
        attempts=1,
        claim_token=claim_token,
        claimed_at=datetime.now(timezone.utc),
        delivered_at=None,
        next_attempt_at=None,
        last_error_code=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return delivery

    class Session:
        def __init__(self):
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return issue

        async def execute(self, _statement):
            return Result()

        async def flush(self):
            return None

        async def scalar(self, _statement):
            return 0

        async def commit(self):
            return None

        def add(self, value):
            self.added.append(value)

    session = Session()
    monkeypatch.setattr(production_issue_monitor, "async_session", lambda: session)
    monkeypatch.setattr(
        production_issue_monitor,
        "get_settings",
        lambda: SimpleNamespace(SAAS_ADMIN_EMAIL="owner@example.com"),
    )
    claim = production_issue_monitor.AlertDeliveryClaim(
        delivery_id=delivery_id,
        issue_id=issue_id,
        alert_epoch=1,
        sink="notification",
        idempotency_key="production-issue:test:1:notification",
        claim_token=claim_token,
        payload={"alert_epoch": 1},
    )

    assert await production_issue_monitor._deliver_notification_claim(claim) is False
    assert delivery.status == "delivered"
    assert issue.alert_notification_sent_at is None
    assert session.added == []


@pytest.mark.asyncio
async def test_obsolete_webhook_delivery_never_calls_the_http_client(monkeypatch):
    issue_id = uuid.uuid4()
    delivery_id = uuid.uuid4()
    claim_token = uuid.uuid4()
    issue = SimpleNamespace(
        id=issue_id,
        status="open",
        alert_epoch=2,
        alerted_at=None,
        alert_attempts=0,
        alert_next_attempt_at=None,
        alert_last_error_code=None,
        alert_notification_sent_at=None,
    )
    delivery = SimpleNamespace(
        id=delivery_id,
        issue_id=issue_id,
        alert_epoch=1,
        sink="webhook",
        status="delivering",
        attempts=1,
        claim_token=claim_token,
        claimed_at=datetime.now(timezone.utc),
        delivered_at=None,
        next_attempt_at=None,
        last_error_code=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return delivery

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return issue

        async def execute(self, _statement):
            return Result()

        async def flush(self):
            return None

        async def scalar(self, _statement):
            return 0

        async def commit(self):
            return None

    client = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("stale webhook")))
    monkeypatch.setattr(production_issue_monitor, "async_session", lambda: Session())
    claim = production_issue_monitor.AlertDeliveryClaim(
        delivery_id=delivery_id,
        issue_id=issue_id,
        alert_epoch=1,
        sink="webhook",
        idempotency_key="production-issue:test:1:webhook",
        claim_token=claim_token,
        payload={"alert_epoch": 1},
    )

    assert await production_issue_monitor._deliver_webhook_claim(
        client,
        production_issue_monitor.asyncio.Semaphore(1),
        claim,
        "https://alerts.example.test/hook",
    ) is False
    client.post.assert_not_awaited()
    assert delivery.status == "delivered"


def test_reopened_issue_uses_a_distinct_stable_notification_identity():
    issue_id = uuid.uuid4()

    first = production_issue_monitor._production_issue_notification_ref_id(
        issue_id,
        1,
    )
    replay = production_issue_monitor._production_issue_notification_ref_id(
        issue_id,
        1,
    )
    reopened = production_issue_monitor._production_issue_notification_ref_id(
        issue_id,
        2,
    )

    assert first == replay
    assert reopened != first


def test_monitor_health_turns_unhealthy_after_the_db_loop_deadline(monkeypatch):
    started_at = datetime.now(timezone.utc)
    monkeypatch.setattr(
        production_issue_monitor,
        "_monitor_started_at",
        started_at,
    )
    monkeypatch.setattr(
        production_issue_monitor,
        "_monitor_last_db_loop_success_at",
        None,
    )
    monkeypatch.setattr(production_issue_monitor, "_monitor_interval_seconds", 30)

    healthy = production_issue_monitor.production_issue_monitor_health(
        now=started_at + production_issue_monitor.timedelta(seconds=119),
    )
    stale = production_issue_monitor.production_issue_monitor_health(
        now=started_at + production_issue_monitor.timedelta(seconds=121),
    )

    assert healthy["healthy"] is True
    assert healthy["deadline_seconds"] == 120
    assert stale["healthy"] is False


def test_failed_external_alert_schedules_retry_without_false_delivery():
    now = datetime.now(timezone.utc)
    issue = SimpleNamespace(
        alert_attempts=0,
        alert_next_attempt_at=None,
        alert_last_error_code=None,
        alerted_at=None,
    )

    production_issue_monitor._schedule_alert_retry(
        issue,
        now=now,
        error_code="ConnectTimeout",
    )

    assert issue.alerted_at is None
    assert issue.alert_attempts == 1
    assert issue.alert_last_error_code == "ConnectTimeout"
    assert issue.alert_next_attempt_at == now + production_issue_monitor.timedelta(seconds=30)


def test_alert_retry_backoff_is_capped_at_one_hour():
    now = datetime.now(timezone.utc)
    issue = SimpleNamespace(
        alert_attempts=100,
        alert_next_attempt_at=None,
        alert_last_error_code=None,
        alerted_at=None,
    )

    production_issue_monitor._schedule_alert_retry(
        issue,
        now=now,
        error_code="ReadTimeout",
    )

    assert issue.alert_next_attempt_at == now + production_issue_monitor.timedelta(hours=1)


@pytest.mark.asyncio
async def test_database_outage_queues_only_sanitized_issue_data(monkeypatch):
    class BrokenSession:
        async def __aenter__(self):
            raise TimeoutError("Bearer private-token prompt=customer-secret")

        async def __aexit__(self, *_args):
            return None

    production_issue_monitor._failed_capture_queue.clear()
    monkeypatch.setattr(
        production_issue_monitor,
        "async_session",
        lambda: BrokenSession(),
    )

    issue_id = await production_issue_monitor.record_production_issue(
        source="trigger_runtime",
        category="database",
        summary="Trigger runtime operation failed",
        error_code="TimeoutError",
        operation="claim_trigger_executions",
        metadata={
            "component": "trigger_daemon",
            "prompt": "customer-secret",
            "api_key": "private-token",
        },
    )

    assert issue_id is None
    assert len(production_issue_monitor._failed_capture_queue) == 1
    queued = production_issue_monitor._failed_capture_queue[0]
    assert queued["metadata"] == {"component": "trigger_daemon"}
    assert "customer-secret" not in str(queued)
    assert "private-token" not in str(queued)


@pytest.mark.asyncio
async def test_monitor_flushes_transient_capture_queue(monkeypatch):
    queued = {
        "source": "trigger_runtime",
        "category": "database",
        "summary": "Trigger runtime operation failed",
        "severity": "error",
        "error_code": "TimeoutError",
        "route": None,
        "operation": "claim_trigger_executions",
        "tenant_id": None,
        "user_id": None,
        "agent_id": None,
        "trace_id": None,
        "metadata": {"component": "trigger_daemon"},
    }
    production_issue_monitor._failed_capture_queue.clear()
    production_issue_monitor._failed_capture_queue.append(queued)
    persist = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(
        production_issue_monitor,
        "record_production_issue",
        persist,
    )

    flushed = await production_issue_monitor.flush_failed_production_issue_captures()

    assert flushed == 1
    assert not production_issue_monitor._failed_capture_queue
    persist.assert_awaited_once_with(**queued)
