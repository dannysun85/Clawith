import pytest

from app.config import Settings


def test_production_rejects_default_secrets():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="change-me-in-production",
        JWT_SECRET_KEY="change-me-jwt-secret",
    )

    with pytest.raises(RuntimeError, match="SECRET_KEY, JWT_SECRET_KEY"):
        settings.validate_runtime_secrets()


def test_production_accepts_configured_secrets():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="app-secret-that-is-not-a-default",
        JWT_SECRET_KEY="jwt-secret-that-is-not-a-default",
    )

    settings.validate_runtime_secrets()


def test_development_keeps_local_defaults_available():
    Settings(ENVIRONMENT="development").validate_runtime_secrets()
