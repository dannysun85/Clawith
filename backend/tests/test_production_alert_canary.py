"""Release-bound production alert delivery canary contracts."""

from datetime import datetime, timezone
import inspect
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.scripts import verify_production_issue_alerts as alert_canary


ROOT = Path(__file__).resolve().parents[2]


def _settings(**overrides):
    values = {
        "APP_VERSION": "1.10.12",
        "JWT_SECRET_KEY": "test-secret-that-is-at-least-thirty-two-bytes",
        "PRODUCTION_ISSUE_MONITOR_ENABLED": True,
        "SAAS_ADMIN_EMAIL": "owner@example.test",
        "PRODUCTION_ISSUE_ALERT_WEBHOOK_URL": ("https://alerts.example.test/astra"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _release_env(monkeypatch, release_id="1.10.12-abc123"):
    monkeypatch.setenv("ASTRA_RELEASE_ID", release_id)
    monkeypatch.setenv("ASTRA_RELEASE_VERSION", "1.10.12")
    monkeypatch.setenv("ASTRA_RELEASE_COMMIT", "a" * 40)


def test_release_identity_must_be_safe_and_match_candidate_environment(monkeypatch):
    _release_env(monkeypatch)

    identity = alert_canary._validated_release_identity("1.10.12-abc123")

    assert identity == alert_canary.ReleaseIdentity(
        release_id="1.10.12-abc123",
        version="1.10.12",
        commit="a" * 40,
    )
    with pytest.raises(alert_canary.AlertCanaryVerificationError):
        alert_canary._validated_release_identity("1.10.12-other")
    with pytest.raises(alert_canary.AlertCanaryVerificationError):
        alert_canary._validated_release_identity("release/with/path")


def test_configured_sinks_fail_closed_without_notification_or_webhook():
    with pytest.raises(
        alert_canary.AlertCanaryVerificationError,
        match="no production alert sink configured",
    ):
        alert_canary._configured_sinks(
            _settings(
                SAAS_ADMIN_EMAIL="",
                PRODUCTION_ISSUE_ALERT_WEBHOOK_URL="",
            )
        )

    assert alert_canary._configured_sinks(_settings()) == frozenset({"notification", "webhook"})


def test_sink_fingerprint_is_keyed_and_does_not_expose_configuration():
    settings = _settings()

    fingerprint = alert_canary._sink_configuration_fingerprint(settings)

    assert fingerprint.startswith("hmac-sha256:")
    assert len(fingerprint) == len("hmac-sha256:") + 64
    assert settings.SAAS_ADMIN_EMAIL not in fingerprint
    assert settings.PRODUCTION_ISSUE_ALERT_WEBHOOK_URL not in fingerprint


def test_canary_identity_changes_when_release_code_or_sink_configuration_changes():
    identity = alert_canary.ReleaseIdentity(
        release_id="1.10.12-abc123",
        version="1.10.12",
        commit="a" * 40,
    )
    original_config = alert_canary._sink_configuration_fingerprint(_settings())
    changed_config = alert_canary._sink_configuration_fingerprint(
        _settings(PRODUCTION_ISSUE_ALERT_WEBHOOK_URL="https://alerts.example.test/changed")
    )
    changed_commit = alert_canary.ReleaseIdentity(
        release_id=identity.release_id,
        version=identity.version,
        commit="b" * 40,
    )

    original_fingerprint = alert_canary._canary_fingerprint(identity, original_config)

    assert original_config != changed_config
    assert original_fingerprint != alert_canary._canary_fingerprint(identity, changed_config)
    assert original_fingerprint != alert_canary._canary_fingerprint(changed_commit, original_config)

    issue = SimpleNamespace(
        fingerprint=original_fingerprint,
        source=alert_canary.RELEASE_ALERT_CANARY_SOURCE,
        operation=alert_canary._canary_operation(identity.release_id),
        release_version=identity.version,
        alert_epoch=1,
        last_metadata=alert_canary._canary_metadata(identity, original_config),
    )
    alert_canary._assert_canary_issue_identity(issue, identity, original_config)

    issue.last_metadata = alert_canary._canary_metadata(identity, changed_config)
    with pytest.raises(
        alert_canary.AlertCanaryVerificationError,
        match="identity drifted",
    ):
        alert_canary._assert_canary_issue_identity(issue, identity, original_config)


@pytest.mark.asyncio
async def test_preflight_validates_owner_and_webhook_policy_without_creating_issue(
    monkeypatch,
):
    _release_env(monkeypatch)
    validate_configuration = AsyncMock()
    monkeypatch.setattr(alert_canary, "get_settings", _settings)
    monkeypatch.setattr(
        alert_canary,
        "_validate_alert_configuration",
        validate_configuration,
    )

    identity, sinks, fingerprint = await alert_canary.verify_production_alert_configuration(
        release_id="1.10.12-abc123",
    )

    assert identity.version == "1.10.12"
    assert sinks == frozenset({"notification", "webhook"})
    assert fingerprint.startswith("hmac-sha256:")
    validate_configuration.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivery_canary_is_passive_and_resolves_after_worker_delivery(
    monkeypatch,
):
    release_id = "1.10.12-abc123"
    _release_env(monkeypatch, release_id)
    issue_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    snapshot = alert_canary.AlertCanarySnapshot(
        issue_id=issue_id,
        alert_epoch=1,
        status="resolved",
        alerted_at=now,
        resolved_at=now,
        deliveries={
            "notification": {
                "status": "delivered",
                "attempts": 1,
                "error_code": None,
                "delivered_at": now.isoformat(),
                "idempotency_key": "notification-key",
                "attribution_version": 1,
                "delivered_by": {
                    "worker_actor_id": "11111111-1111-4111-8111-111111111111",
                    "release_id": release_id,
                    "release_commit": "a" * 40,
                },
            },
            "webhook": {
                "status": "delivered",
                "attempts": 1,
                "error_code": None,
                "delivered_at": now.isoformat(),
                "idempotency_key": "webhook-key",
                "attribution_version": 1,
                "delivered_by": {
                    "worker_actor_id": "11111111-1111-4111-8111-111111111111",
                    "release_id": release_id,
                    "release_commit": "a" * 40,
                },
            },
        },
    )
    identity = alert_canary.ReleaseIdentity(
        release_id=release_id,
        version="1.10.12",
        commit="a" * 40,
    )
    settings = _settings()
    config_fingerprint = alert_canary._sink_configuration_fingerprint(settings)
    verify_config = AsyncMock(
        return_value=(
            identity,
            frozenset({"notification", "webhook"}),
            config_fingerprint,
        )
    )
    create_or_resume = AsyncMock(return_value=issue_id)
    verify = AsyncMock(return_value=(True, snapshot))
    monkeypatch.setattr(alert_canary, "get_settings", lambda: settings)
    monkeypatch.setattr(
        alert_canary,
        "verify_production_alert_configuration",
        verify_config,
    )
    monkeypatch.setattr(
        alert_canary,
        "_create_or_resume_canary",
        create_or_resume,
    )
    monkeypatch.setattr(
        alert_canary,
        "_verify_and_resolve_if_delivered",
        verify,
    )

    result = await alert_canary.verify_production_issue_alerts(
        release_id=release_id,
        timeout_seconds=60,
        poll_interval_seconds=0.25,
    )

    assert result == (identity, config_fingerprint, snapshot)
    create_or_resume.assert_awaited_once_with(
        identity,
        settings,
        frozenset({"notification", "webhook"}),
        config_fingerprint,
    )
    verify.assert_awaited_once_with(
        issue_id,
        frozenset({"notification", "webhook"}),
        identity,
        config_fingerprint,
    )
    source = inspect.getsource(alert_canary)
    assert "dispatch_production_issue_alerts" not in source
    assert "record_production_issue" not in source


def test_canary_uses_issue_then_delivery_lock_order():
    source = inspect.getsource(alert_canary._verify_and_resolve_if_delivered)

    assert source.index("db.get(ProductionIssue") < source.index("select(ProductionIssueAlertDelivery)")
    assert source.index("_snapshot_is_delivered") < source.index('issue.status = "resolved"')


def test_canary_returns_scalar_id_after_transaction_expiration_boundaries():
    source = inspect.getsource(alert_canary._create_or_resume_canary)

    assert "issue_id = issue.id" in source
    assert "return issue.id" not in source
    assert source.count("return issue_id") == 2
    assert source.index("issue_id = issue.id") < source.index("await db.commit()")


def test_canary_standalone_process_loads_event_foreign_key_metadata():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.scripts import verify_production_issue_alerts; "
                "from app.models.production_issue import ProductionIssueEvent; "
                "assert [table.name for table in "
                "ProductionIssueEvent.__mapper__._sorted_tables] == "
                "['production_issue_events']"
            ),
        ],
        cwd=ROOT / "backend",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_canary_epoch_is_immutable_after_first_creation():
    create_source = inspect.getsource(alert_canary._create_or_resume_canary)
    verify_source = inspect.getsource(alert_canary._verify_and_resolve_if_delivered)
    identity_source = inspect.getsource(alert_canary._assert_canary_issue_identity)

    assert "_assert_canary_issue_identity" in create_source
    assert "_assert_canary_issue_identity" in verify_source
    assert "int(issue.alert_epoch or 1) != 1" in identity_source


def test_canary_rejects_delivery_from_the_wrong_release_worker():
    now = datetime.now(timezone.utc)
    identity = alert_canary.ReleaseIdentity(
        release_id="1.10.12-abc123",
        version="1.10.12",
        commit="a" * 40,
    )
    snapshot = alert_canary.AlertCanarySnapshot(
        issue_id=uuid.uuid4(),
        alert_epoch=1,
        status="open",
        alerted_at=now,
        resolved_at=None,
        deliveries={
            "notification": {
                "status": "delivered",
                "delivered_at": now.isoformat(),
                "attribution_version": 1,
                "delivered_by": {
                    "worker_actor_id": "11111111-1111-4111-8111-111111111111",
                    "release_id": "old-release",
                    "release_commit": "b" * 40,
                },
            }
        },
    )

    with pytest.raises(
        alert_canary.AlertCanaryVerificationError,
        match="identity drifted",
    ):
        alert_canary._snapshot_is_delivered(
            snapshot,
            frozenset({"notification"}),
            identity,
        )


def test_canary_total_timeout_covers_configuration_creation_and_polling():
    source = inspect.getsource(alert_canary.verify_production_issue_alerts)

    timeout_context = source.index("async with asyncio.timeout(timeout)")
    assert timeout_context < source.index("verify_production_alert_configuration")
    assert timeout_context < source.index("_create_or_resume_canary")
    assert timeout_context < source.index("_verify_and_resolve_if_delivered")


def test_deploy_runs_alert_preflight_before_maintenance_and_delivery_before_cutover():
    deploy = (ROOT / "scripts/deploy-astra-production.sh").read_text(encoding="utf-8")

    preflight_call = deploy.index("validating production alert sink configuration")
    maintenance = deploy.index("enabling explicit Web/API/WebSocket maintenance fence")
    delivery = deploy.index("verifying production alert delivery pipeline")
    business_verified = deploy.index('write_cutover_state candidate_business_verified "$CANDIDATE_SLOT" "$RELEASE_ID"')
    nginx_cutover = deploy.index("switching the verified maintenance fence")

    assert '--release-id "$target_release_id" --preflight-only' in deploy
    assert "compose_project_timed 45 5" in deploy
    assert "compose_project_timed 210 10" in deploy
    assert "ASTRA_ALERT_WORKER_ACTOR_ID" in deploy
    assert 'delivery.get("attribution_version") != 1' in deploy
    assert 'delivered_by.get("worker_actor_id")' in deploy
    assert preflight_call < maintenance
    assert delivery < business_verified < nginx_cutover
    assert "production-alert-preflight.json" in deploy
    assert "production-alert-canary.json" in deploy
    assert 'echo "$PRODUCTION_ISSUE_ALERT_WEBHOOK_URL"' not in deploy
