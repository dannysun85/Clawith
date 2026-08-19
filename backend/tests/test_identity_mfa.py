from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.mfa_crypto import (
    MFA_SECRET_ENVELOPE_PREFIX,
    open_mfa_secret,
    recovery_code_digest,
    seal_mfa_secret,
)
from app.core.security import (
    access_token_matches_identity,
    create_access_token,
    decode_access_token,
    mfa_access_error_code,
)
from app.models.identity_mfa import IdentityMfaChallenge
from app.services.mfa_service import (
    MFA_MAX_FAILED_ATTEMPTS,
    MfaChallengeError,
    create_mfa_challenge,
    decode_challenge_token,
    generate_recovery_codes,
    generate_totp_secret,
    identity_recommends_mfa,
    identity_requires_mfa,
    matching_totp_step,
    record_challenge_failure,
    require_live_challenge,
    totp_code,
    verify_identity_factor,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ChallengeDb:
    def __init__(self):
        self.challenge = None

    def add(self, value):
        self.challenge = value

    async def flush(self):
        return None

    async def execute(self, _statement):
        return _ScalarResult(self.challenge)


class _NoRecoveryDb:
    async def execute(self, _statement):
        raise AssertionError("TOTP verification must not query recovery codes")


def _user(*, role: str, enabled: bool, platform: bool = False):
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        is_platform_admin=platform,
        mfa_enabled=enabled,
        auth_version=4,
    )
    return SimpleNamespace(id=uuid.uuid4(), role=role, identity=identity)


def test_totp_matches_rfc_vector_and_only_small_clock_window() -> None:
    # RFC 6238 SHA-1 vector at T=59 is 94287082 for 8 digits; the 6-digit
    # profile used here is therefore the final six digits.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret, at_time=59) == "287082"

    current = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    prior = totp_code(secret, at_time=current - timedelta(seconds=30))
    too_old = totp_code(secret, at_time=current - timedelta(seconds=60))
    assert matching_totp_step(secret, prior, at_time=current) is not None
    assert matching_totp_step(secret, too_old, at_time=current) is None


def test_mfa_secret_envelope_is_authenticated_and_identity_bound_recovery_digest() -> None:
    secret = generate_totp_secret()
    envelope = seal_mfa_secret(secret)
    assert envelope.startswith(MFA_SECRET_ENVELOPE_PREFIX)
    assert secret not in envelope
    assert open_mfa_secret(envelope) == secret

    tampered = envelope[:-1] + ("A" if envelope[-1] != "A" else "B")
    with pytest.raises(ValueError, match="authentication failed"):
        open_mfa_secret(tampered)

    first_identity = uuid.uuid4()
    second_identity = uuid.uuid4()
    assert recovery_code_digest(first_identity, "ABCD-EFGH-IJKL-MNOP") == recovery_code_digest(
        first_identity,
        "abcdefghijklmnop",
    )
    assert recovery_code_digest(first_identity, "ABCD-EFGH-IJKL-MNOP") != recovery_code_digest(
        second_identity,
        "ABCD-EFGH-IJKL-MNOP",
    )


def test_recovery_codes_are_unique_high_entropy_one_time_shapes() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert all(len(code.replace("-", "")) == 16 for code in codes)
    assert all(code.count("-") == 3 for code in codes)


@pytest.mark.asyncio
async def test_challenge_is_signed_database_fenced_and_contains_no_setup_secret() -> None:
    db = _ChallengeDb()
    identity_id = uuid.uuid4()
    user_id = uuid.uuid4()
    secret = generate_totp_secret()
    challenge, token = await create_mfa_challenge(
        db,
        identity_id=identity_id,
        user_id=user_id,
        auth_version=7,
        purpose="setup",
        secret=secret,
    )
    claims = decode_challenge_token(token)
    assert claims == {
        "challenge_id": challenge.id,
        "identity_id": identity_id,
        "user_id": user_id,
        "purpose": "setup",
        "auth_version": 7,
    }
    assert secret not in token
    assert challenge.secret_envelope and secret not in challenge.secret_envelope
    assert await require_live_challenge(db, token, purposes={"setup"}) is challenge

    challenge.consumed_at = datetime.now(timezone.utc)
    with pytest.raises(MfaChallengeError, match="invalid or expired"):
        await require_live_challenge(db, token, purposes={"setup"})


@pytest.mark.asyncio
async def test_challenge_rejects_expiry_purpose_and_cross_identity_mismatch() -> None:
    issued_at = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    db = _ChallengeDb()
    challenge, token = await create_mfa_challenge(
        db,
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        auth_version=3,
        purpose="login",
        now=issued_at,
    )

    with pytest.raises(MfaChallengeError, match="invalid or expired"):
        await require_live_challenge(
            db,
            token,
            purposes={"login"},
            now=challenge.expires_at,
        )
    with pytest.raises(MfaChallengeError, match="invalid or expired"):
        await require_live_challenge(
            db,
            token,
            purposes={"setup"},
            now=issued_at + timedelta(seconds=1),
        )

    original_identity_id = challenge.identity_id
    challenge.identity_id = uuid.uuid4()
    with pytest.raises(MfaChallengeError, match="invalid or expired"):
        await require_live_challenge(
            db,
            token,
            purposes={"login"},
            now=issued_at + timedelta(seconds=1),
        )
    challenge.identity_id = original_identity_id


@pytest.mark.asyncio
async def test_totp_factor_rejects_replay_of_same_time_step() -> None:
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    secret = generate_totp_secret()
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        mfa_secret_envelope=seal_mfa_secret(secret),
        mfa_last_totp_step=None,
    )
    code = totp_code(secret, at_time=now)
    assert await verify_identity_factor(_NoRecoveryDb(), identity=identity, code=code, now=now) == "totp"
    assert await verify_identity_factor(_NoRecoveryDb(), identity=identity, code=code, now=now) is None


def test_mfa_gate_matrix_and_access_token_assurance_claim() -> None:
    member = _user(role="member", enabled=False)
    owner = _user(role="org_owner", enabled=False)
    platform = _user(role="platform_admin", enabled=False, platform=True)
    enabled_member = _user(role="member", enabled=True)

    assert mfa_access_error_code({"mfa": False}, member) is None
    assert mfa_access_error_code({"mfa": False}, owner) is None
    assert mfa_access_error_code({"mfa": False}, platform) is None
    assert mfa_access_error_code({"mfa": False}, enabled_member) == "mfa_challenge_required"
    assert mfa_access_error_code({"mfa": True}, enabled_member) is None
    assert identity_requires_mfa(owner) is False
    assert identity_recommends_mfa(owner) is True
    assert identity_recommends_mfa(platform) is True
    assert identity_recommends_mfa(member) is False

    token = create_access_token(
        str(enabled_member.id),
        enabled_member.role,
        auth_version=enabled_member.identity.auth_version,
        mfa_verified=True,
    )
    payload = decode_access_token(token)
    assert payload["mfa"] is True
    assert access_token_matches_identity(payload, enabled_member.identity)
    enabled_member.identity.auth_version += 1
    assert not access_token_matches_identity(payload, enabled_member.identity)


def test_failed_challenge_is_consumed_at_bounded_attempt_limit() -> None:
    challenge = IdentityMfaChallenge(
        id=uuid.uuid4(),
        identity_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        purpose="login",
        auth_version=1,
        failed_attempts=MFA_MAX_FAILED_ATTEMPTS - 1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    record_challenge_failure(challenge)
    assert challenge.failed_attempts == MFA_MAX_FAILED_ATTEMPTS
    assert challenge.consumed_at is not None


def test_privileged_role_may_disable_mfa() -> None:
    owner = _user(role="org_owner", enabled=True)
    assert identity_requires_mfa(owner) is False
    assert identity_recommends_mfa(owner) is True
