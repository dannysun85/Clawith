import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

from app.models.tenant import Tenant
from app.models.user import Identity


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "backend/scripts/manage_production_smoke_principals.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "manage_production_smoke_principals",
        RUNNER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload() -> dict[str, str]:
    return {
        "SMOKE_TENANT_EMAIL": "release-owner@example.invalid",
        "SMOKE_TENANT_PASSWORD": "Owner-Release-Smoke-Password-001!",
        "SMOKE_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "SMOKE_PLATFORM_ADMIN_EMAIL": "release-smoke-platform@release-smoke.invalid",
        "SMOKE_PLATFORM_ADMIN_PASSWORD": "Platform-Release-Smoke-Password-002!",
        "SMOKE_MEMBER_EMAIL": "release-smoke-member@release-smoke.invalid",
        "SMOKE_MEMBER_PASSWORD": "Member-Release-Smoke-Password-003!",
    }


def _credentials_file(tmp_path: Path) -> Path:
    path = tmp_path / "release.smoke-credentials.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_credentials_require_exact_synthetic_separated_principals():
    runner = _load_runner()

    credentials = runner.parse_credentials(_payload())

    assert str(credentials.tenant_id) == _payload()["SMOKE_TENANT_ID"]
    representation = repr(credentials)
    assert "Owner-Release-Smoke-Password" not in representation
    assert "Platform-Release-Smoke-Password" not in representation
    assert "Member-Release-Smoke-Password" not in representation


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "SMOKE_PLATFORM_ADMIN_EMAIL",
            "existing-admin@example.com",
            "platform_email_must_be_release_smoke_synthetic",
        ),
        (
            "SMOKE_MEMBER_EMAIL",
            "customer@example.com",
            "member_email_must_be_release_smoke_synthetic",
        ),
        (
            "SMOKE_MEMBER_PASSWORD",
            "short",
            "strong_passwords_required",
        ),
        (
            "SMOKE_MEMBER_EMAIL",
            "release-owner@example.invalid",
            "principal_emails_must_be_distinct",
        ),
    ],
)
def test_credentials_fail_closed(field: str, value: str, code: str):
    runner = _load_runner()
    payload = _payload()
    payload[field] = value

    with pytest.raises(runner.PrincipalManagerError) as exc:
        runner.parse_credentials(payload)

    assert exc.value.code == code


def test_credentials_file_rejects_loose_mode_and_symlink(tmp_path: Path):
    runner = _load_runner()
    path = _credentials_file(tmp_path)
    path.chmod(0o644)

    with pytest.raises(runner.PrincipalManagerError) as exc:
        runner.load_credentials_file(path, production=True)
    assert exc.value.code == "credentials_file_mode_invalid"

    path.chmod(0o600)
    link = tmp_path / "linked-credentials.json"
    link.symlink_to(path)
    with pytest.raises(runner.PrincipalManagerError) as exc:
        runner.load_credentials_file(link, production=True)
    assert exc.value.code == "credentials_file_unsafe"


def test_credentials_file_accepts_current_owner_mode_0600(tmp_path: Path):
    runner = _load_runner()
    path = _credentials_file(tmp_path)

    credentials = runner.load_credentials_file(path, production=True)

    assert credentials.member_email.endswith("@release-smoke.invalid")
    assert path.stat().st_uid == os.getuid()


def test_release_qa_boundary_rejects_customer_or_deletion_tenant():
    runner = _load_runner()
    release_qa = Tenant(
        id=uuid.uuid4(),
        name="Release QA v1.12.0",
        slug="release-qa-v1120",
        im_provider="web_only",
        is_active=True,
    )
    customer = Tenant(
        id=uuid.uuid4(),
        name="Customer Company",
        slug="customer-company",
        im_provider="web_only",
        is_active=True,
    )

    assert runner._release_qa_tenant(release_qa) is True
    assert runner._release_qa_tenant(customer) is False
    release_qa.is_active = False
    assert runner._release_qa_tenant(release_qa) is False


def test_login_ready_requires_password_active_verified_and_no_mfa():
    runner = _load_runner()
    identity = Identity(
        email="release-owner@example.invalid",
        password_hash="opaque",
        password_login_enabled=True,
        email_verified=True,
        is_active=True,
        mfa_enabled=False,
    )

    assert runner._identity_login_ready(identity) is True
    identity.mfa_enabled = True
    assert runner._identity_login_ready(identity) is False


@pytest.mark.asyncio
async def test_mutation_requires_apply_production_and_operation_id(tmp_path: Path):
    runner = _load_runner()
    path = _credentials_file(tmp_path)
    base = {
        "action": "provision",
        "credentials_file": path,
        "confirm_environment": "production",
        "confirm_tenant_id": _payload()["SMOKE_TENANT_ID"],
        "release_version": "1.12.2",
        "operation_id": str(uuid.uuid4()),
        "apply": False,
    }

    with pytest.raises(runner.PrincipalManagerError) as exc:
        await runner.run(argparse.Namespace(**base))
    assert exc.value.code == "mutating_action_requires_apply"

    base["apply"] = True
    base["confirm_environment"] = "test"
    with pytest.raises(runner.PrincipalManagerError) as exc:
        await runner.run(argparse.Namespace(**base))
    assert exc.value.code == "mutating_action_requires_production_confirmation"

    base["confirm_environment"] = "production"
    for invalid_operation_id in (
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        f"{{{uuid.uuid4()}}}",
    ):
        base["operation_id"] = invalid_operation_id
        with pytest.raises(runner.PrincipalManagerError) as exc:
            await runner.run(argparse.Namespace(**base))
        assert exc.value.code == "operation_id_invalid"


def test_unexpected_failure_is_redacted(tmp_path: Path, monkeypatch, capsys):
    runner = _load_runner()
    path = _credentials_file(tmp_path)

    async def fail_without_leaking(_args):
        raise RuntimeError(_payload()["SMOKE_TENANT_PASSWORD"])

    monkeypatch.setattr(runner, "run", fail_without_leaking)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUNNER),
            "--action",
            "inventory",
            "--credentials-file",
            str(path),
            "--confirm-environment",
            "production",
            "--confirm-tenant-id",
            _payload()["SMOKE_TENANT_ID"],
        ],
    )

    assert runner.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "action": "inventory",
        "error_code": "internal_error",
        "ok": False,
    }
    assert _payload()["SMOKE_TENANT_PASSWORD"] not in captured.err
