from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from pydantic import ValidationError

from app.api.enterprise import (
    TestEmailRequest as SystemEmailTestRequest,
    send_test_email_endpoint,
)


def test_system_email_test_recipient_requires_a_valid_email_address() -> None:
    with pytest.raises(ValidationError):
        SystemEmailTestRequest(email="not-an-email")


@pytest.mark.asyncio
async def test_system_email_test_reports_only_smtp_acceptance(monkeypatch) -> None:
    send_test_email = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.system_email_service.send_test_email",
        send_test_email,
    )
    request = SystemEmailTestRequest(email="qa@example.com")

    response = await send_test_email_endpoint(
        request,
        current_user=SimpleNamespace(id="platform-operator"),
        db=SimpleNamespace(),
    )

    send_test_email.assert_awaited_once_with("qa@example.com", db=ANY)
    assert response == {
        "success": True,
        "evidence_level": "smtp_accepted",
        "recipient": "qa@example.com",
        "message": (
            "SMTP server accepted the test message for qa@example.com; "
            "inbox delivery is not proven."
        ),
    }
