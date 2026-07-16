import pytest

from app.core.channel_secrets import (
    EncryptedChannelJSON,
    EncryptedChannelText,
    is_channel_secret_envelope,
    open_channel_secret,
    seal_channel_secret,
)
from app.scripts.verify_channel_secrets import CHANNEL_SECRET_STORAGE_QUERY


def test_channel_text_is_authenticated_and_encrypted_at_rest():
    column_type = EncryptedChannelText(purpose="app_secret")
    stored = column_type.process_bind_param("customer-bot-secret", None)

    assert is_channel_secret_envelope(stored)
    assert "customer-bot-secret" not in stored
    assert column_type.process_result_value(stored, None) == "customer-bot-secret"

    tampered = stored[:-1] + ("A" if stored[-1] != "A" else "B")
    with pytest.raises(ValueError, match="authentication failed"):
        column_type.process_result_value(tampered, None)


def test_channel_envelopes_are_bound_to_their_field_purpose():
    stored = seal_channel_secret("shared-value", purpose="app_secret")

    with pytest.raises(ValueError, match="authentication failed"):
        open_channel_secret(stored, purpose="verification_token")


def test_channel_json_encrypts_the_complete_object_and_dual_reads_legacy_json():
    column_type = EncryptedChannelJSON(purpose="extra_config")
    config = {
        "connection_mode": "webhook",
        "nested": {"future_unknown_secret": "private-token"},
    }
    stored = column_type.process_bind_param(config, None)

    assert is_channel_secret_envelope(stored)
    assert "webhook" not in stored
    assert "private-token" not in stored
    assert column_type.process_result_value(stored, None) == config
    assert column_type.process_result_value(config, None) == config
    assert column_type.process_result_value('{"legacy":true}', None) == {
        "legacy": True
    }


def test_channel_text_dual_reads_pre_migration_plaintext():
    column_type = EncryptedChannelText(purpose="encrypt_key")

    assert column_type.process_result_value("legacy-signing-secret", None) == (
        "legacy-signing-secret"
    )


def test_channel_secret_verifier_accepts_legacy_json_column_type():
    assert "extra_config::text NOT LIKE 'enc:channel:v1:%'" in str(
        CHANNEL_SECRET_STORAGE_QUERY
    )
