"""Canonical identity values shared by every authentication entry point."""

from __future__ import annotations

import re


def canonicalize_email(value: object | None) -> str | None:
    """Return the product-wide, case-insensitive email identity key."""
    if value is None:
        return None
    # PostgreSQL's persisted uniqueness contract is ``lower(email)``.  Keep
    # runtime canonicalization byte-for-byte aligned with that database rule;
    # Python ``casefold()`` is intentionally broader and can otherwise produce
    # a lookup key that the database expression index does not consider equal.
    normalized = str(value).strip().lower()
    return normalized or None


def canonicalize_phone(value: object | None) -> str | None:
    """Return the login-key form used for phone identity lookups."""
    if value is None:
        return None
    # Keep this byte-for-byte aligned with the historical persisted format.
    # Parentheses were never removed, so changing that at runtime would make
    # existing rows unreachable without a collision-aware data migration.
    normalized = re.sub(r"[\s\-+]", "", str(value).strip())
    return normalized or None


def normalize_username(value: object | None) -> str | None:
    """Strip human-entered usernames without changing case semantics."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def username_looks_like_contact(value: object | None) -> bool:
    """Reject usernames that would occupy the email or phone login namespace."""
    username = normalize_username(value)
    if not username:
        return False
    if "@" in username:
        return True
    phone_digits = re.sub(r"[\s\-+()]", "", username)
    return bool(
        phone_digits.isdigit()
        and 6 <= len(phone_digits) <= 20
        and re.fullmatch(r"[\d\s\-+()]+", username)
    )
