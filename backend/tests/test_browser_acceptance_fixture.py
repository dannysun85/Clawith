"""Safety contracts for the destructive browser-acceptance fixture cleanup."""

from __future__ import annotations

import uuid

import pytest

from scripts.browser_acceptance_fixture import (
    ROLES,
    _assert_fixture_rows_owned,
    _cleanup_scope,
)


def _state() -> dict[str, object]:
    run_tag = "0123456789ab"
    identities = {
        role: {
            "id": str(uuid.uuid4()),
            "email": f"browser-{role}-{run_tag}@local.clawith.test",
            "secret": "not-used-by-cleanup",
            "user_id": str(uuid.uuid4()),
        }
        for role in ROLES
    }
    return {
        "schema_version": 1,
        "run_tag": run_tag,
        "password": "not-used-by-cleanup",
        "identities": identities,
        "tenants": {
            "primary": str(uuid.uuid4()),
            "secondary": str(uuid.uuid4()),
            "purge": str(uuid.uuid4()),
        },
        "agents": {
            "current_assistant": str(uuid.uuid4()),
            "retained_assistant": str(uuid.uuid4()),
            "managed_employee": str(uuid.uuid4()),
        },
        "additional_memberships": {"owner_secondary": str(uuid.uuid4())},
        "fixture_global_rows": {"assistant_template": str(uuid.uuid4())},
    }


def test_cleanup_scope_binds_destructive_ids_to_exact_fixture_names() -> None:
    scope = _cleanup_scope(_state())

    assert set(scope.identity_emails.values()) == {
        f"browser-{role}-{scope.run_tag}@local.clawith.test" for role in ROLES
    }
    assert set(scope.tenant_slugs.values()) == {
        f"browser-primary-{scope.run_tag}",
        f"browser-secondary-{scope.run_tag}",
        f"g11-purge-browser-{scope.run_tag}",
    }
    assert len(scope.users) == 7
    assert len(scope.agent_ids) == 3


def test_cleanup_scope_rejects_an_unowned_identity_before_database_deletes() -> None:
    state = _state()
    state["identities"]["owner"]["email"] = "admin@reeftotem.ai"  # type: ignore[index]

    with pytest.raises(
        RuntimeError,
        match="fixture_ownership_mismatch:identity:owner",
    ):
        _cleanup_scope(state)


def test_cleanup_scope_rejects_duplicate_tenant_targets() -> None:
    state = _state()
    state["tenants"]["secondary"] = state["tenants"]["primary"]  # type: ignore[index]

    with pytest.raises(RuntimeError, match="invalid_fixture_state:duplicate_tenant"):
        _cleanup_scope(state)


def test_cleanup_ownership_check_rejects_a_real_row_with_wrong_slug() -> None:
    tenant_id = uuid.uuid4()

    with pytest.raises(RuntimeError, match="fixture_ownership_mismatch:tenants"):
        _assert_fixture_rows_owned(
            "tenants",
            expected={tenant_id: "browser-primary-0123456789ab"},
            actual={tenant_id: "customer-production-tenant"},
        )


def test_cleanup_ownership_check_allows_already_missing_fixture_rows() -> None:
    tenant_id = uuid.uuid4()

    _assert_fixture_rows_owned(
        "tenants",
        expected={tenant_id: "browser-primary-0123456789ab"},
        actual={},
    )
