"""LLM model management tenant authorization regression tests.

Ported from upstream 182d343a. The local SaaS control plane is stricter than
upstream: LLM providers/models/routes are platform-operated assets, so every
management endpoint requires platform authority and the cross-tenant org-admin
path upstream had to close does not exist here. These tests pin that boundary.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.enterprise import (
    _require_platform_model_admin,
    list_llm_models,
    set_default_llm_model,
)


def _user(tenant_id: uuid.UUID | None, role: str = "org_admin", platform_identity: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=tenant_id,
        role=role,
        identity=SimpleNamespace(is_platform_admin=platform_identity),
    )


def test_org_admin_fails_platform_model_admin_check() -> None:
    with pytest.raises(HTTPException, match="platform admin") as error:
        _require_platform_model_admin(_user(uuid.uuid4()))

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_cannot_list_models_even_for_own_tenant() -> None:
    db = AsyncMock()

    with pytest.raises(HTTPException) as error:
        await list_llm_models(
            tenant_id=None,
            current_user=_user(uuid.uuid4()),
            db=db,
        )

    assert error.value.status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_admin_cannot_mutate_models_in_any_tenant() -> None:
    db = AsyncMock()
    org_admin = _user(uuid.uuid4())

    with pytest.raises(HTTPException) as set_default_error:
        await set_default_llm_model(uuid.uuid4(), current_user=org_admin, db=db)

    assert set_default_error.value.status_code == 403


def test_platform_admin_passes_platform_model_admin_check() -> None:
    _require_platform_model_admin(_user(None, role="member", platform_identity=True))
