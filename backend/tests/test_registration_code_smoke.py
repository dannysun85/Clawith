import importlib.util
from pathlib import Path
import sys

import pytest

from app.models.user import Identity, User


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts/registration_code_smoke.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("registration_code_smoke", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_smoke_admin_password_reset_enables_login_and_revokes_old_tokens(monkeypatch):
    runner = _load_runner()
    identity = Identity(
        email="registration-smoke-admin@clawith-smoke.com",
        username="registration_smoke_admin",
        password_hash="old-hash",
        password_login_enabled=False,
        auth_version=4,
        is_active=True,
        is_platform_admin=True,
        email_verified=True,
    )
    user = User(
        identity=identity,
        tenant_id=None,
        display_name="Old Smoke Admin",
        role="platform_admin",
        is_active=True,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter((Result(identity), Result(user)))
            self.committed = False

        async def execute(self, _statement):
            return next(self.results)

        async def flush(self):
            return None

        async def commit(self):
            self.committed = True

        def add(self, _value):
            raise AssertionError("existing smoke account should be reused")

    session = Session()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    import app.database

    monkeypatch.setattr(app.database, "async_session", SessionContext)

    result = await runner.ensure_smoke_platform_admin(
        identity.email,
        "ReplacementSmokePass123!",
    )

    assert result["role"] == "platform_admin"
    assert identity.password_login_enabled is True
    assert identity.auth_version == 5
    assert identity.password_hash != "old-hash"
    assert session.committed is True
