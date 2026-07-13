"""Pydantic schemas for the credential pool API (账号池管理)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CredentialCreateIn(BaseModel):
    provider: str
    label: str
    api_key: str
    base_url: str | None = None
    capabilities: list | None = None  # ["text","voice","image","video"]; None = all
    daily_quota: int | None = None
    weight: int = 1
    priority: int = 0
    rpm_limit: int | None = None  # max requests per minute (client-side throttle)
    tpm_limit: int | None = None  # max tokens per minute
    window_5h_limit: int | None = None  # Deprecated compatibility field; not enforced.


class CredentialUpdateIn(BaseModel):
    api_key: str | None = None
    label: str | None = None
    base_url: str | None = None
    capabilities: list | None = None
    daily_quota: int | None = None
    weight: int | None = None
    priority: int | None = None
    enabled: bool | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    window_5h_limit: int | None = None


class CredentialOut(BaseModel):
    id: uuid.UUID
    provider: str
    label: str
    base_url: str | None = None
    api_key_masked: str = ""
    capabilities: list | None = None
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
