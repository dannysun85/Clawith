from app.services.system_setting_security import (
    CONFIGURED_SECRET_PLACEHOLDER,
    decrypt_system_setting_value,
    encrypt_system_setting_value,
    mask_system_setting_value,
)


def test_system_setting_secret_is_encrypted_and_masked():
    secured = encrypt_system_setting_value(
        "system_email_platform",
        {
            "SYSTEM_SMTP_HOST": "smtp.example.com",
            "SYSTEM_SMTP_PASSWORD": "plain-secret",
        },
    )

    assert secured["SYSTEM_SMTP_PASSWORD"] != "plain-secret"
    assert decrypt_system_setting_value(
        "system_email_platform",
        secured,
    )["SYSTEM_SMTP_PASSWORD"] == "plain-secret"

    masked = mask_system_setting_value("system_email_platform", secured)
    assert masked["SYSTEM_SMTP_PASSWORD"] == CONFIGURED_SECRET_PLACEHOLDER
    assert masked["_configured_secret_fields"] == ["SYSTEM_SMTP_PASSWORD"]
    assert "plain-secret" not in repr(masked)
    assert secured["SYSTEM_SMTP_PASSWORD"] not in repr(masked)


def test_configured_placeholder_preserves_existing_ciphertext():
    existing = encrypt_system_setting_value(
        "jina_api_key",
        {"api_key": "jina-secret"},
    )

    updated = encrypt_system_setting_value(
        "jina_api_key",
        {"api_key": CONFIGURED_SECRET_PLACEHOLDER},
        existing_value=existing,
    )

    assert updated == existing
    assert decrypt_system_setting_value("jina_api_key", updated)["api_key"] == "jina-secret"


def test_omitting_secret_from_replacement_clears_it():
    existing = encrypt_system_setting_value(
        "jina_api_key",
        {"api_key": "jina-secret"},
    )

    updated = encrypt_system_setting_value(
        "jina_api_key",
        {},
        existing_value=existing,
    )

    assert updated == {}


def test_quarantined_legacy_config_is_not_serialized_by_generic_api():
    masked = mask_system_setting_value(
        "legacy_tool_config_quarantine:deadbeef",
        {"config": {"api_key": "ciphertext"}, "runtime_enabled": False},
    )

    assert masked == {"runtime_enabled": False, "quarantined": True}
    assert "ciphertext" not in repr(masked)
