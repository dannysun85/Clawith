import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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


@pytest.mark.asyncio
async def test_issue_capture_stores_only_sanitized_occurrence_metadata(monkeypatch):
    expected_id = uuid.uuid4()

    class Result:
        def scalar_one(self):
            return expected_id

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
        lambda: SimpleNamespace(APP_VERSION="1.10.8"),
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
            return expected_issue_id

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
        lambda: SimpleNamespace(APP_VERSION="1.10.8"),
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
