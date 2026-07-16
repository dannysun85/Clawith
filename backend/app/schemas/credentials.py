"""Pydantic schemas for the credential pool API (账号池管理)."""

import uuid
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.modalities import CANONICAL_MODALITIES, canonicalize_modalities


SUPPORTED_CREDENTIAL_PROVIDERS = {
    "anthropic",
    "custom",
    "deepseek",
    "gemini",
    "kimi",
    "minimax",
    "openai",
    "qwen",
    "zhipu",
}


def _validate_required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be empty")
    return normalized


def _validate_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("base_url must not contain userinfo, query parameters, or fragments")
    return normalized


def _validate_capabilities(value: list[str] | None) -> list[str] | None:
    # NULL is the explicit legacy "all capabilities" contract. An empty list
    # is rejected because PostgreSQL routing treats it as no capabilities.
    if value is None:
        return None
    normalized = canonicalize_modalities(value)
    unsupported = sorted(set(normalized) - set(CANONICAL_MODALITIES))
    if unsupported:
        raise ValueError(f"unsupported credential capabilities: {', '.join(unsupported)}")
    if not normalized:
        raise ValueError("capabilities must contain at least one modality or be null for all")
    return normalized


class CredentialCreateIn(BaseModel):
    provider: str
    label: str
    api_key: str
    base_url: str | None = None
    capabilities: list[str] | None = None  # None = all; [] is invalid
    daily_quota: int | None = Field(default=None, ge=1)
    weight: int = Field(default=1, ge=1, le=1000)
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    rpm_limit: int | None = Field(default=None, ge=1)  # max requests per minute
    tpm_limit: int | None = Field(default=None, ge=1)  # max tokens per minute
    window_5h_limit: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")

    _normalize_capabilities = field_validator("capabilities")(_validate_capabilities)
    _normalize_required_text = field_validator("label", "api_key")(_validate_required_text)
    _normalize_base_url = field_validator("base_url")(_validate_base_url)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_CREDENTIAL_PROVIDERS:
            raise ValueError("unsupported credential provider")
        return normalized

    @model_validator(mode="after")
    def require_custom_base_url(self):
        if self.provider == "custom" and not self.base_url:
            raise ValueError("custom provider requires base_url")
        return self


class CredentialUpdateIn(BaseModel):
    api_key: str | None = None
    label: str | None = None
    base_url: str | None = None
    capabilities: list[str] | None = None
    daily_quota: int | None = Field(default=None, ge=1)
    weight: int | None = Field(default=None, ge=1, le=1000)
    priority: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    enabled: bool | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    window_5h_limit: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")

    _normalize_capabilities = field_validator("capabilities")(_validate_capabilities)
    _normalize_base_url = field_validator("base_url")(_validate_base_url)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value):
        if isinstance(value, dict):
            nullable_fields = {
                "base_url",
                "capabilities",
                "daily_quota",
                "rpm_limit",
                "tpm_limit",
                "window_5h_limit",
            }
            invalid = sorted(
                key
                for key, item in value.items()
                if item is None and key not in nullable_fields
            )
            if invalid:
                raise ValueError(
                    f"explicit null is not allowed for: {', '.join(invalid)}"
                )
        return value

    @field_validator("label", "api_key")
    @classmethod
    def normalize_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_required_text(value)


class CredentialOut(BaseModel):
    id: uuid.UUID
    provider: str
    label: str
    base_url: str | None = None
    api_key_masked: str = ""
    capabilities: list[str] | None = None
    modality_status: dict[str, dict[str, str]] | None = None
    daily_quota: int | None = None
    used_today: int = 0
    status: str = "unverified"
    error_count: int = 0
    weight: int = 1
    priority: int = 0
    last_used_at: datetime | None = None
    enabled: bool = True
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    window_5h_limit: int | None = None
    tenant_id: uuid.UUID | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CredentialVerificationOut(BaseModel):
    ok: bool
    status: str
    provider_status: int | None = None
    model_count: int | None = None
    message: str | None = None


class CredentialHealthOut(BaseModel):
    id: uuid.UUID
    provider: str
    label: str
    status: str
    enabled: bool
    modality_status: dict[str, dict[str, str]] | None = None
    used_today: int
    daily_quota: int | None = None
    error_count: int
    success_rate: float  # used_today / (used_today + error_count); 1.0 if no calls
    last_used_at: datetime | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    rpm_current: int = 0  # requests in last 60s
    tpm_current: int = 0  # tokens in last 60s

    model_config = ConfigDict(from_attributes=True)
