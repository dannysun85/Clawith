"""Encrypted-at-rest contracts for complete identity-provider configs."""

import pytest

from app.core.identity_provider_secrets import (
    EncryptedIdentityProviderJSON,
    is_identity_provider_config_envelope,
)
from app.scripts.verify_identity_provider_secrets import (
    IDENTITY_PROVIDER_SECRET_STORAGE_QUERY,
)


def test_identity_provider_config_is_authenticated_and_encrypted_at_rest():
    column_type = EncryptedIdentityProviderJSON()
    config = {
        "client_id": "public-client-id",
        "client_secret": "top-secret",
        "service_account": {"private_key": "nested-private-key"},
    }

    stored = column_type.process_bind_param(config, None)

    assert is_identity_provider_config_envelope(stored)
    assert "top-secret" not in stored
    assert "nested-private-key" not in stored
    assert column_type.process_result_value(stored, None) == config

    tampered = stored[:-1] + ("A" if stored[-1] != "A" else "B")
    with pytest.raises(ValueError, match="authentication failed"):
        column_type.process_result_value(tampered, None)


def test_identity_provider_config_dual_reads_legacy_json_during_cutover():
    column_type = EncryptedIdentityProviderJSON()

    assert column_type.process_result_value({"legacy": True}, None) == {
        "legacy": True
    }
    assert column_type.process_result_value('{"legacy":true}', None) == {
        "legacy": True
    }
    assert column_type.process_result_value(None, None) is None


def test_identity_provider_secret_verifier_requires_envelope_prefix():
    query = str(IDENTITY_PROVIDER_SECRET_STORAGE_QUERY)

    assert "enc:idp:v1:%" in query
    assert "config IS NOT NULL" in query
