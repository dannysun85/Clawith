"""Pure helpers for validating persisted credential verification evidence."""

from __future__ import annotations

from datetime import datetime


def credential_verification_receipt(credential) -> dict[str, object] | None:
    """Return the validated latest account-probe receipt for current config.

    The receipt proves only provider authentication for the stored account
    contract.  It does not prove a media generation entitlement, output
    quality, or commercial readiness.
    """

    receipt = getattr(credential, "verification_receipt", None)
    verified_at = getattr(credential, "last_verification_at", None)
    credential_id = getattr(credential, "id", None)
    provider = str(getattr(credential, "provider", "") or "").strip().lower()
    if (
        not isinstance(receipt, dict)
        or receipt.get("kind") != "credential_auth_probe"
        or not isinstance(receipt.get("ok"), bool)
        or str(receipt.get("credential_id") or "") != str(credential_id or "")
        or str(receipt.get("provider") or "").strip().lower() != provider
        or not isinstance(verified_at, datetime)
    ):
        return None
    checked_at = str(receipt.get("checked_at") or "").strip()
    if not checked_at or checked_at != verified_at.isoformat():
        return None
    return dict(receipt)


def current_credential_verification_receipt(credential) -> dict[str, object] | None:
    """Return a successful current-config account verification receipt."""

    receipt = credential_verification_receipt(credential)
    if receipt is None or receipt.get("ok") is not True:
        return None
    return receipt
