import uuid

import pytest

from app.services import channel_issue_reporting


@pytest.mark.asyncio
async def test_channel_issue_reporting_never_forwards_provider_error_text(monkeypatch):
    captured = {}

    async def fake_record_production_issue(**kwargs):
        captured.update(kwargs)
        return uuid.uuid4()

    monkeypatch.setattr(
        channel_issue_reporting,
        "record_production_issue",
        fake_record_production_issue,
    )
    agent_id = uuid.uuid4()

    await channel_issue_reporting.record_channel_issue(
        channel="DingTalk",
        operation="Connect",
        agent_id=agent_id,
        error_code="AuthenticationError",
    )

    assert captured == {
        "source": "channel_connector",
        "category": "channel",
        "summary": "Dingtalk channel connect failed",
        "severity": "error",
        "error_code": "AuthenticationError",
        "operation": "dingtalk.connect",
        "agent_id": agent_id,
        "metadata": {
            "provider": "dingtalk",
            "component": "dingtalk_connector",
            "error_type": "AuthenticationError",
        },
    }


@pytest.mark.asyncio
async def test_channel_issue_reporting_bounds_index_fields(monkeypatch):
    captured = {}

    async def fake_record_production_issue(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        channel_issue_reporting,
        "record_production_issue",
        fake_record_production_issue,
    )

    await channel_issue_reporting.record_channel_issue(
        channel="f" * 100,
        operation="o" * 100,
        agent_id=uuid.uuid4(),
        error_code="e" * 200,
        severity="critical",
    )

    assert len(captured["metadata"]["provider"]) == 40
    assert len(captured["operation"]) == 101
    assert len(captured["error_code"]) == 100
    assert captured["severity"] == "critical"
