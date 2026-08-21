from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.okr import OKRSettings
from app.models.tool import Tool


def _user(*, platform_admin: bool = False):
    return SimpleNamespace(
        identity=SimpleNamespace(is_platform_admin=platform_admin),
        tenant_id="00000000-0000-0000-0000-000000000001",
        role="org_owner",
    )


def test_disabled_code_and_agentbay_surfaces_are_hidden_from_tenant_operators(monkeypatch):
    from app.api import tools
    from app import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(CODE_EXECUTION_ENABLED=False),
    )

    assert tools._tool_hidden_by_release_policy(
        Tool(name="execute_code", category="code"),
        _user(),
    )
    assert tools._tool_hidden_by_release_policy(
        Tool(name="agentbay_browser_navigate", category="agentbay"),
        _user(),
    )
    assert not tools._tool_hidden_by_release_policy(
        Tool(name="web_search", category="search"),
        _user(),
    )


def test_platform_operator_can_inspect_disabled_code_surface(monkeypatch):
    from app.api import tools
    from app import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(CODE_EXECUTION_ENABLED=False),
    )

    assert not tools._tool_hidden_by_release_policy(
        Tool(name="agentbay_browser_navigate", category="agentbay"),
        _user(platform_admin=True),
    )


def test_okr_settings_report_unavailable_automation_and_reject_new_intent(monkeypatch):
    from app.api import okr

    monkeypatch.setattr(okr.runtime_settings, "OKR_AUTOMATION_ENABLED", False)
    projection = okr._okr_automation_projection()

    assert projection == {
        "automation_available": False,
        "automation_unavailable_reason": "platform_automation_disabled",
    }

    persisted = OKRSettings(daily_report_enabled=False)
    with pytest.raises(HTTPException) as error:
        okr._validate_okr_automation_update(
            okr.OKRSettingsUpdate(daily_report_enabled=True),
            persisted,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "okr_automation_unavailable"


def test_okr_disabled_automation_still_allows_legacy_intent_cleanup(monkeypatch):
    from app.api import okr

    monkeypatch.setattr(okr.runtime_settings, "OKR_AUTOMATION_ENABLED", False)
    persisted = OKRSettings(daily_report_enabled=True)

    okr._validate_okr_automation_update(
        okr.OKRSettingsUpdate(daily_report_enabled=False),
        persisted,
    )


def test_douyin_status_separates_account_configuration_from_direct_publish(monkeypatch):
    from app.services.douyin import operations

    monkeypatch.setattr(operations, "is_configured", lambda: True)
    monkeypatch.setattr(operations, "direct_publish_enabled", lambda: False)
    monkeypatch.setattr(operations, "configured_scopes", lambda: ["video.list"])
    monkeypatch.setattr(operations, "callback_url", lambda: "https://example.test/callback")

    status = operations.DouyinOperationsService().config_status()

    assert status["configured"] is True
    assert status["direct_publish_available"] is False
    assert status["status"] == "ready"
