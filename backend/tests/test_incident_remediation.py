import uuid

import pytest

from app.services.incident_remediation import (
    _matches_any_secret,
    incident_reference,
    remediate,
)


def test_incident_reference_is_deterministic_and_namespaced():
    first = incident_reference("prod-2026-07-12-ppt")
    second = incident_reference("prod-2026-07-12-ppt")
    other = incident_reference("prod-2026-07-12-video")

    assert first == second
    assert first != other
    assert isinstance(first, uuid.UUID)


def test_incident_reference_rejects_blank_key():
    with pytest.raises(ValueError, match="incident_key"):
        incident_reference("  ")


def test_secret_matching_never_requires_logging_secret_values():
    assert _matches_any_secret(["safe", "sk-example-value"], "sk-example-value") is True
    assert _matches_any_secret(["safe"], "sk-example-value") is False
    assert _matches_any_secret(["safe"], "") is False


@pytest.mark.asyncio
async def test_remediation_validates_arguments_before_opening_database():
    with pytest.raises(ValueError, match="tenant_id"):
        await remediate(refund_credits=10, incident_key="incident")
    with pytest.raises(ValueError, match="provided together"):
        await remediate(trigger_agent_id=uuid.uuid4())
    with pytest.raises(ValueError, match="cannot be negative"):
        await remediate(refund_credits=-1)
