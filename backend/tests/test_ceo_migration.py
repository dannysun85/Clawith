"""Contract tests for secret-free legacy CEO discovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException

from app.api import ceo as ceo_api
from app.services.ceo_migration import (
    LegacyCeoEvidence,
    classify_ceo_migration_state,
    is_legacy_ceo_candidate,
)


def _candidate(**overrides) -> LegacyCeoEvidence:
    values = {
        "agent_id": uuid.uuid4(),
        "name": "CEO",
        "is_system": False,
    }
    values.update(overrides)
    return LegacyCeoEvidence(**values)


@pytest.mark.parametrize(
    "identity",
    [
        {"name": "CEO", "role_description": "", "bio": "", "template_role_key": None},
        {"name": "林总", "role_description": "首席执行官", "bio": "", "template_role_key": None},
        {"name": "Executive", "role_description": "", "bio": "", "template_role_key": "ceo"},
    ],
)
def test_ceo_candidate_detection_uses_explicit_identity_markers(identity):
    assert is_legacy_ceo_candidate(**identity)


def test_ceo_candidate_detection_does_not_match_unrelated_agent():
    assert not is_legacy_ceo_candidate(
        name="Finance Analyst",
        role_description="Budget and cash-flow analysis",
        bio="Produces monthly reports",
        template_role_key="finance-analyst",
    )


def test_no_candidate_requires_explicit_enablement():
    classification, action, warnings = classify_ceo_migration_state(
        formal_ceo_agent_id=None,
        candidates=[],
    )

    assert classification == "none"
    assert "explicit" in action
    assert warnings == []


def test_clean_legacy_candidate_is_reviewable_but_not_auto_adopted():
    classification, action, warnings = classify_ceo_migration_state(
        formal_ceo_agent_id=None,
        candidates=[_candidate()],
    )

    assert classification == "legacy_clean_adoptable"
    assert "review manifest" in action
    assert "not automatic adoption" in warnings[0]


@pytest.mark.parametrize(
    "history",
    [
        {"session_count": 1},
        {"message_count": 1},
        {"active_trigger_count": 1},
        {"control_plane_revision_count": 1},
        {"control_plane_bytes": 1},
        {"has_last_activity": True},
    ],
)
def test_any_behavioral_history_requires_clean_ceo_and_archive_review(history):
    classification, action, warnings = classify_ceo_migration_state(
        formal_ceo_agent_id=None,
        candidates=[_candidate(**history)],
    )

    assert classification == "legacy_contaminated_archive"
    assert "clean formal CEO" in action
    assert "must not be copied" in warnings[0]


def test_multiple_candidates_fail_closed_to_manual_review():
    classification, _action, warnings = classify_ceo_migration_state(
        formal_ceo_agent_id=None,
        candidates=[_candidate(), _candidate()],
    )

    assert classification == "ambiguous_manual_review"
    assert "automatic adoption is unsafe" in warnings[0]


def test_formal_ceo_wins_without_merging_extra_candidates():
    formal_id = uuid.uuid4()
    classification, action, warnings = classify_ceo_migration_state(
        formal_ceo_agent_id=formal_id,
        candidates=[
            _candidate(agent_id=formal_id, is_system=True, template_role_key="ceo"),
            _candidate(),
        ],
    )

    assert classification == "formal_ceo"
    assert "without merging history" in action
    assert warnings


@pytest.mark.asyncio
async def test_migration_preview_requires_company_governor():
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    with pytest.raises(HTTPException) as exc:
        await ceo_api.get_ceo_migration_preview(
            current_user=user,
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_migration_preview_is_tenant_scoped_and_delegates_to_read_only_builder(
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role="org_admin")
    preview = {
        "schema_version": "1.12.0",
        "mode": "dry_run",
        "tenant_id": str(tenant_id),
    }
    builder = AsyncMock(return_value=preview)
    monkeypatch.setattr(ceo_api, "build_ceo_migration_preview", builder)

    result = await ceo_api.get_ceo_migration_preview(
        current_user=user,
        db=SimpleNamespace(),
    )

    assert result == preview
    builder.assert_awaited_once_with(SimpleNamespace(), tenant_id=tenant_id)
