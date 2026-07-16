import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.schemas.schemas import ChannelConfigOut


def _channel(**overrides):
    values = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "channel_type": "teams",
        "app_id": "public-client-id",
        "app_secret": "top-level-app-secret",
        "encrypt_key": "top-level-encrypt-key",
        "verification_token": "top-level-verification-token",
        "is_configured": True,
        "is_connected": False,
        "last_tested_at": None,
        "extra_config": {},
        "created_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_channel_config_response_exposes_status_not_credentials():
    output = ChannelConfigOut.model_validate(
        _channel(
            extra_config={
                "connection_mode": "webhook",
                "service_url": "https://oauth-token.example/conversation",
                "botSecret": "camel-case-secret",
                "nested": {
                    "accessToken": "nested-token",
                    "display_name": "public-name",
                },
                "items": [
                    {"client_secret": "list-secret", "region": "cn"},
                ],
            }
        )
    )

    payload = output.model_dump()
    serialized = output.model_dump_json()
    assert payload["app_id"] == "public-client-id"
    assert payload["app_secret_configured"] is True
    assert payload["encrypt_key_configured"] is True
    assert payload["verification_token_configured"] is True
    assert payload["extra_config"] == {
        "connection_mode": "webhook",
        "nested": {"display_name": "public-name"},
        "items": [{"region": "cn"}],
    }
    assert set(payload["configured_secret_fields"]) == {
        "service_url",
        "botSecret",
        "nested.accessToken",
        "items.client_secret",
    }
    for secret in (
        "top-level-app-secret",
        "top-level-encrypt-key",
        "top-level-verification-token",
        "oauth-token.example",
        "camel-case-secret",
        "nested-token",
        "list-secret",
    ):
        assert secret not in serialized


def test_channel_config_status_dict_round_trip_keeps_no_legacy_secret_fields():
    output = ChannelConfigOut.model_validate(
        {
            "id": uuid.uuid4(),
            "agent_id": uuid.uuid4(),
            "channel_type": "slack",
            "app_id": None,
            "app_secret": "legacy-secret-must-be-dropped",
            "app_secret_configured": True,
            "encrypt_key_configured": True,
            "verification_token_configured": False,
            "configured_secret_fields": ["signing_secret"],
            "is_configured": True,
            "is_connected": False,
            "extra_config": {"connection_mode": "webhook"},
            "created_at": datetime.now(timezone.utc),
        }
    )

    assert output.app_secret_configured is True
    assert "legacy-secret-must-be-dropped" not in output.model_dump_json()
    assert not {"app_secret", "encrypt_key", "verification_token"}.intersection(
        output.model_dump()
    )
