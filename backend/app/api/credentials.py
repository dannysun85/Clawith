"""Credential pool API (账号池管理, platform_admin only).

Manages the platform API-key account pool: CRUD on credentials + real-time
pool health monitoring. Credentials are provider-scoped (one key serves
multiple models/modalities of a provider) and shared across all tenants.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import encrypt_data, require_role
from app.database import get_db
from app.models.llm import LLMCredential
from app.schemas.credentials import (
    CredentialCreateIn,
    CredentialHealthOut,
    CredentialOut,
    CredentialUpdateIn,
    CredentialVerificationOut,
)
from app.services.credential_verification import verify_provider_credential
from app.services.llm.load_balancer import get_credential_health
from app.services.llm.utils import get_credential_api_key

router = APIRouter(prefix="/credentials", tags=["credentials"])

settings = get_settings()
_PLATFORM_ADMIN = Depends(require_role("platform_admin"))


def _mask_key(cred: LLMCredential) -> str:
    key = get_credential_api_key(cred)
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def _to_out(cred: LLMCredential) -> CredentialOut:
    out = CredentialOut.model_validate(cred)
    out.api_key_masked = _mask_key(cred)
    return out


@router.get("", response_model=list[CredentialOut], dependencies=[_PLATFORM_ADMIN])
async def list_credentials(db: AsyncSession = Depends(get_db)):
    """List all credentials in the pool (platform admin)."""
    result = await db.execute(select(LLMCredential).order_by(LLMCredential.provider, LLMCredential.priority.desc()))
    return [_to_out(c) for c in result.scalars().all()]


@router.get("/health", response_model=list[CredentialHealthOut], dependencies=[_PLATFORM_ADMIN])
async def credentials_health():
    """Real-time pool health: usage / status / rate-limit counters per credential."""
    entries = await get_credential_health()
    out = []
    for e in entries:
        total = e["used_today"] + e["error_count"]
        success_rate = (e["used_today"] / total) if total > 0 else 1.0
        out.append(CredentialHealthOut(
            id=e["id"],
            provider=e["provider"],
            label=e["label"],
            status=e["status"],
            enabled=e["enabled"],
            modality_status=e["modality_status"],
            used_today=e["used_today"],
            daily_quota=e["daily_quota"],
            error_count=e["error_count"],
            success_rate=round(success_rate, 3),
            last_used_at=e["last_used_at"],
            rpm_limit=e["rpm_limit"],
            tpm_limit=e["tpm_limit"],
            rpm_current=e["rpm_current"],
            tpm_current=e["tpm_current"],
        ))
    return out


@router.post("", response_model=CredentialOut, status_code=status.HTTP_201_CREATED, dependencies=[_PLATFORM_ADMIN])
async def create_credential(data: CredentialCreateIn, db: AsyncSession = Depends(get_db)):
    """Add an API-key account to the pool."""
    cred = LLMCredential(
        provider=data.provider,
        label=data.label,
        api_key_encrypted=encrypt_data(data.api_key, settings.SECRET_KEY),
        base_url=data.base_url,
        capabilities=data.capabilities,
        daily_quota=data.daily_quota,
        weight=data.weight,
        priority=data.priority,
        status="unverified",
        rpm_limit=data.rpm_limit,
        tpm_limit=data.tpm_limit,
        window_5h_limit=data.window_5h_limit,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return _to_out(cred)


@router.patch("/{credential_id}", response_model=CredentialOut, dependencies=[_PLATFORM_ADMIN])
async def update_credential(credential_id: uuid.UUID, data: CredentialUpdateIn, db: AsyncSession = Depends(get_db)):
    """Update a credential (label/quota/weight/priority/capabilities/enabled)."""
    cred = await db.get(LLMCredential, credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    updates = data.model_dump(exclude_unset=True)
    new_api_key = updates.pop("api_key", None)
    if new_api_key is not None and not new_api_key.strip():
        raise HTTPException(status_code=422, detail="API key cannot be empty")
    api_key_changed = new_api_key is not None
    base_url_changed = "base_url" in updates and updates["base_url"] != cred.base_url
    for k, v in updates.items():
        setattr(cred, k, v)
    if api_key_changed:
        cred.api_key_encrypted = encrypt_data(new_api_key.strip(), settings.SECRET_KEY)
    if base_url_changed or api_key_changed:
        cred.status = "unverified"
        cred.error_count = 0
    if api_key_changed:
        # A replacement key is a different provider account, so inherited
        # model-quota circuits are no longer relevant. Changing only the API
        # host is not quota-recovery evidence for the same account.
        cred.modality_status = {}
    await db.commit()
    await db.refresh(cred)
    return _to_out(cred)


@router.post("/{credential_id}/verify", response_model=CredentialVerificationOut, dependencies=[_PLATFORM_ADMIN])
async def verify_credential(credential_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Run a read-only provider probe before admitting a credential to routing."""
    cred = await db.get(LLMCredential, credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    result = await verify_provider_credential(cred)
    cred.status = "healthy" if result.ok else "unverified"
    if result.ok:
        # Explicit administrator verification is the recovery boundary for a
        # degraded shared credential. Ordinary successful calls may clear a
        # consecutive counter, but must not silently re-admit an isolated key.
        cred.error_count = 0
        # Authentication verification does not prove that every independent
        # text/media allowance recovered. Scoped circuits are closed by an
        # observed successful call or the MiniMax remains poller.
    await db.commit()
    return CredentialVerificationOut(
        ok=result.ok,
        status=cred.status,
        provider_status=result.provider_status,
        model_count=result.model_count,
        message=result.message,
    )


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_PLATFORM_ADMIN])
async def delete_credential(credential_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Remove a credential from the pool."""
    cred = await db.get(LLMCredential, credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    await db.delete(cred)
    await db.commit()
