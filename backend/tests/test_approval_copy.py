from app.services import autonomy_service


def test_approval_resolution_copy_matches_enabled_worker(monkeypatch):
    monkeypatch.setattr(
        autonomy_service,
        "APPROVAL_AUTOMATIC_EXECUTION_ENABLED",
        True,
    )

    title, body = autonomy_service._approval_resolution_copy("approved")

    assert title == "approved — queued for execution"
    assert "queued the signed action" in body
    assert "completed" in body
    assert "paused" not in f"{title} {body}"


def test_approval_resolution_copy_matches_paused_worker(monkeypatch):
    monkeypatch.setattr(
        autonomy_service,
        "APPROVAL_AUTOMATIC_EXECUTION_ENABLED",
        False,
    )

    title, body = autonomy_service._approval_resolution_copy("approved")

    assert title == "approved — execution paused"
    assert "paused" in body


def test_rejected_approval_never_claims_execution():
    title, body = autonomy_service._approval_resolution_copy("rejected")

    assert title == "rejected"
    assert body == "Approval rejected. The action will not execute."
