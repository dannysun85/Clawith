"""Credential pool API (账号池管理, configured SaaS owner only).

Manages the platform API-key account pool: CRUD on credentials + real-time
pool health monitoring. Credentials are provider-scoped (one key serves
multiple models/modalities of a provider) and shared across all tenants.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import encrypt_data, get_saas_admin
from app.database import get_db
from app.models.llm import LLMCredential
from app.schemas.credentials import (
    CredentialCreateIn,
    CredentialHealthOut,
    CredentialOut,
    CredentialUpdateIn,
    CredentialVerificationOut,
)
from app.services.credential_verification import (
    build_credential_verification_receipt,
    verify_provider_credential,
)
from app.services.llm.load_balancer import get_credential_health
from app.services.llm.utils import get_credential_api_key
from app.services.volcengine_agent_plan import (
    ALLOWED_PLAN_TIERS,
    PROVIDER as VOLCENGINE_AGENT_PLAN_PROVIDER,
    VIDEO_CAPABLE_PLAN_TIERS,
    normalize_base_url as normalize_volcengine_agent_plan_base_url,
)

router = APIRouter(prefix="/credentials", tags=["credentials"])

settings = get_settings()
_SAAS_OWNER = Depends(get_saas_admin)


def _mask_key(cred: LLMCredential) -> str:
    key = get_credential_api_key(cred)
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def _to_out(cred: LLMCredential) -> CredentialOut:
    out = CredentialOut.model_validate(cred)
    out.api_key_masked = _mask_key(cred)
    return out


def _validate_provider_account_contract(
    *,
    provider: str,
    base_url: str | None,
    plan_tier: str | None,
    capabilities: list[str] | None,
) -> tuple[str | None, str | None]:
    if provider != VOLCENGINE_AGENT_PLAN_PROVIDER:
        if plan_tier is not None:
            raise HTTPException(
                status_code=422,
                detail="plan_tier is only valid for volcengine_agent_plan",
            )
        return base_url, None
    normalized_plan = str(plan_tier or "").strip().lower()
    if normalized_plan not in ALLOWED_PLAN_TIERS:
        raise HTTPException(
            status_code=422,
            detail="volcengine_agent_plan requires plan_tier: small, medium, large, or max",
        )
    supported = set(capabilities or ())
    if not supported or supported.difference({"text", "image", "audio", "video"}):
        raise HTTPException(
            status_code=422,
            detail="volcengine_agent_plan requires explicit text/image/audio/video capabilities",
        )
    if "video" in supported and normalized_plan not in VIDEO_CAPABLE_PLAN_TIERS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Agent Plan video requires Large or Max and a current "
                "operator-reviewed model policy"
            ),
        )
    try:
        normalized_base_url = normalize_volcengine_agent_plan_base_url(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return normalized_base_url, normalized_plan


@router.get("", response_model=list[CredentialOut], dependencies=[_SAAS_OWNER])
async def list_credentials(db: AsyncSession = Depends(get_db)):
    """List all credentials in the pool (platform admin)."""
    result = await db.execute(select(LLMCredential).order_by(LLMCredential.provider, LLMCredential.priority.desc()))
    return [_to_out(c) for c in result.scalars().all()]


@router.get("/health", response_model=list[CredentialHealthOut], dependencies=[_SAAS_OWNER])
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


@router.post("", response_model=CredentialOut, status_code=status.HTTP_201_CREATED, dependencies=[_SAAS_OWNER])
async def create_credential(data: CredentialCreateIn, db: AsyncSession = Depends(get_db)):
    """Add an API-key account to the pool."""
    base_url, plan_tier = _validate_provider_account_contract(
        provider=data.provider,
        base_url=data.base_url,
        plan_tier=data.plan_tier,
        capabilities=data.capabilities,
    )
    cred = LLMCredential(
        provider=data.provider,
        label=data.label,
        api_key_encrypted=encrypt_data(data.api_key, settings.SECRET_KEY),
        base_url=base_url,
        plan_tier=plan_tier,
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


@router.patch("/{credential_id}", response_model=CredentialOut, dependencies=[_SAAS_OWNER])
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
    effective_base_url, effective_plan_tier = _validate_provider_account_contract(
        provider=cred.provider,
        base_url=updates.get("base_url", cred.base_url),
        plan_tier=updates.get("plan_tier", getattr(cred, "plan_tier", None)),
        capabilities=updates.get("capabilities", cred.capabilities),
    )
    if "base_url" in updates or cred.provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        updates["base_url"] = effective_base_url
    if "plan_tier" in updates or cred.provider == VOLCENGINE_AGENT_PLAN_PROVIDER:
        updates["plan_tier"] = effective_plan_tier
    base_url_changed = "base_url" in updates and updates["base_url"] != cred.base_url
    plan_tier_changed = (
        "plan_tier" in updates
        and updates["plan_tier"] != getattr(cred, "plan_tier", None)
    )
    capabilities_changed = (
        "capabilities" in updates
        and updates["capabilities"] != cred.capabilities
    )
    for k, v in updates.items():
        setattr(cred, k, v)
    if api_key_changed:
        cred.api_key_encrypted = encrypt_data(new_api_key.strip(), settings.SECRET_KEY)
    if base_url_changed or plan_tier_changed or capabilities_changed or api_key_changed:
        cred.status = "unverified"
        cred.error_count = 0
        cred.last_verification_at = None
        cred.verification_receipt = None
    if base_url_changed or plan_tier_changed or capabilities_changed or api_key_changed:
        # Routing-relevant account configuration defines which provider
        # resources the stored modality circuits describe.  Any such change
        # invalidates those observations; keeping them would make a freshly
        # synchronized tier/capability look unavailable until an old circuit
        # happened to expire.
        cred.modality_status = {}
    await db.commit()
    await db.refresh(cred)
    return _to_out(cred)


@router.post("/{credential_id}/verify", response_model=CredentialVerificationOut, dependencies=[_SAAS_OWNER])
async def verify_credential(credential_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Run a read-only provider probe before admitting a credential to routing."""
    cred = await db.get(LLMCredential, credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    result = await verify_provider_credential(cred)
    checked_at = datetime.now(timezone.utc)
    receipt = build_credential_verification_receipt(
        cred,
        checked_at=checked_at,
        ok=result.ok,
        provider_status=result.provider_status,
        model_count=result.model_count,
    )
    cred.status = "healthy" if result.ok else "unverified"
    cred.last_verification_at = checked_at
    cred.verification_receipt = receipt
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
        receipt=receipt,
    )


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_SAAS_OWNER])
async def delete_credential(credential_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Remove a credential from the pool."""
    cred = await db.get(LLMCredential, credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    await db.delete(cred)
    await db.commit()
