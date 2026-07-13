import uuid
from types import SimpleNamespace

import pytest

from app.services.media_incident_remediation import (
    _normalize_task_ids,
    _validate_task_scope,
)


def test_media_remediation_requires_exact_nonempty_ids_and_deduplicates():
    task_id = uuid.uuid4()
    assert _normalize_task_ids((task_id, task_id)) == (task_id,)
    with pytest.raises(ValueError, match="at least one"):
        _normalize_task_ids(())


def test_media_remediation_fails_closed_on_tenant_mismatch():
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, tenant_id=uuid.uuid4(), status="retrying")
    with pytest.raises(ValueError, match="outside the expected tenant"):
        _validate_task_scope([task], (task_id,), uuid.uuid4())


def test_media_remediation_never_terminalizes_successful_assets():
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, tenant_id=tenant_id, status="succeeded")
    with pytest.raises(ValueError, match="must never"):
        _validate_task_scope([task], (task_id,), tenant_id)
