"""Securely upsert and verify one platform credential from a bounded stdin payload.

The API key is accepted only on stdin so it never appears in the process list,
shell history, release environment, or command arguments.  Output and audit
records are deliberately secret-free.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
import sys

from pydantic import ValidationError
from sqlalchemy import select

# Standalone commands must load the complete FK registry before ORM flushes.
import app.models.agent  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.user  # noqa: F401
from app.config import get_settings
from app.core.security import encrypt_data
from app.database import async_session
from app.models.audit import AuditLog
from app.models.llm import LLMCredential
from app.schemas.credentials import CredentialCreateIn
from app.services.credential_verification import (
    build_credential_verification_receipt,
    verify_provider_credential,
)
from app.services.volcengine_agent_plan import PROVIDER


_MAX_STDIN_BYTES = 64 * 1024
_SECRET_LIKE_LABEL_PREFIXES = (
    "ark-",
    "sk-",
    "ak-",
    "aklt",
    "aiza",
    "eyj",
    "ghp_",
    "github_pat_",
    "xoxb-",
    "ya29.",
)
_TOKENISH_LABEL = re.compile(r"^[A-Za-z0-9._~-]+$")


class CredentialImportInputError(ValueError):
    """Safe validation error whose message never includes submitted secrets."""


def _label_looks_like_secret(value: str) -> bool:
    """Reject likely copy/paste of a credential into the display-name field."""

    normalized = value.strip()
    lowered = normalized.lower()
    if lowered.startswith(_SECRET_LIKE_LABEL_PREFIXES):
        return True
    if len(normalized) < 48 or _TOKENISH_LABEL.fullmatch(normalized) is None:
        return False
    character_classes = sum(
        (
            any(character.islower() for character in normalized),
            any(character.isupper() for character in normalized),
            any(character.isdigit() for character in normalized),
            any(not character.isalnum() for character in normalized),
        )
    )
    return character_classes >= 3


def _load_payload(raw: bytes) -> tuple[CredentialCreateIn, bool]:
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        raise CredentialImportInputError("credential payload is empty or too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialImportInputError("credential payload is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CredentialImportInputError("credential payload must be an object")

    enabled = value.pop("enabled", True)
    if not isinstance(enabled, bool):
        raise CredentialImportInputError("credential enabled must be a boolean")
    try:
        data = CredentialCreateIn.model_validate(value)
    except ValidationError:
        # Pydantic validation details may echo submitted values.  Never attach
        # the original exception or payload to this operator-facing error.
        raise CredentialImportInputError("credential payload validation failed") from None
    if data.provider != PROVIDER:
        raise CredentialImportInputError(
            "this importer accepts only volcengine_agent_plan credentials"
        )
    if _label_looks_like_secret(data.label):
        raise CredentialImportInputError("credential label resembles a secret")
    return data, enabled


def _audit_details(
    credential: LLMCredential,
    *,
    operation: str,
    provider_status: int | None,
    verified: bool,
) -> dict[str, object]:
    return {
        "operation": operation,
        "source": "secure_stdin_operator",
        "credential_id": str(credential.id),
        "provider": credential.provider,
        "base_url": credential.base_url,
        "plan_tier": credential.plan_tier,
        "capabilities": list(credential.capabilities or []),
        "enabled": credential.enabled,
        "priority": credential.priority,
        "weight": credential.weight,
        "daily_quota": credential.daily_quota,
        "rpm_limit": credential.rpm_limit,
        "tpm_limit": credential.tpm_limit,
        "window_5h_limit": credential.window_5h_limit,
        "verified": verified,
        "provider_status": provider_status,
        "release_id": os.environ.get("ASTRA_RELEASE_ID", "").strip() or None,
        "release_commit": os.environ.get("ASTRA_RELEASE_COMMIT", "").strip() or None,
    }


async def _upsert_and_verify(
    data: CredentialCreateIn,
    *,
    enabled: bool,
) -> dict[str, object]:
    settings = get_settings()
    async with async_session() as db:
        result = await db.execute(
            select(LLMCredential)
            .where(
                LLMCredential.provider == data.provider,
                LLMCredential.label == data.label,
                LLMCredential.tenant_id.is_(None),
            )
            .with_for_update()
        )
        matches = list(result.scalars().all())
        if len(matches) > 1:
            raise RuntimeError("multiple matching platform credentials require manual reconciliation")

        operation = "updated" if matches else "created"
        credential = matches[0] if matches else LLMCredential(
            provider=data.provider,
            label=data.label,
        )
        if not matches:
            db.add(credential)

        credential.api_key_encrypted = encrypt_data(data.api_key, settings.SECRET_KEY)
        credential.base_url = data.base_url
        credential.plan_tier = data.plan_tier
        credential.capabilities = data.capabilities
        credential.daily_quota = data.daily_quota
        credential.weight = data.weight
        credential.priority = data.priority
        credential.rpm_limit = data.rpm_limit
        credential.tpm_limit = data.tpm_limit
        credential.window_5h_limit = data.window_5h_limit
        credential.enabled = enabled
        credential.status = "unverified"
        credential.error_count = 0
        credential.modality_status = {}
        credential.last_verification_at = None
        credential.verification_receipt = None
        await db.flush()

        verification = await verify_provider_credential(credential)
        checked_at = datetime.now(timezone.utc)
        receipt = build_credential_verification_receipt(
            credential,
            checked_at=checked_at,
            ok=verification.ok,
            provider_status=verification.provider_status,
            model_count=verification.model_count,
        )
        credential.status = "healthy" if verification.ok else "unverified"
        credential.last_verification_at = checked_at
        credential.verification_receipt = receipt
        db.add(
            AuditLog(
                action="platform_credential_sync",
                details=_audit_details(
                    credential,
                    operation=operation,
                    provider_status=verification.provider_status,
                    verified=verification.ok,
                ),
            )
        )
        await db.commit()

        return {
            "ok": verification.ok,
            "operation": operation,
            "credential_id": str(credential.id),
            "provider": credential.provider,
            "plan_tier": credential.plan_tier,
            "capabilities": list(credential.capabilities or []),
            "status": credential.status,
            "provider_status": verification.provider_status,
            "model_count": verification.model_count,
            "verification_receipt_ref": receipt["receipt_ref"],
        }


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        data, enabled = _load_payload(raw)
        result = asyncio.run(_upsert_and_verify(data, enabled=enabled))
    except CredentialImportInputError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        # Exception messages from database drivers may contain bound values.
        # Emit only the type and a stable operator code.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "platform credential import failed",
                    "error_type": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
