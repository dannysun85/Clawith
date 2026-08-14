from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]
CLEANUP_SCRIPT = ROOT / "scripts/cleanup_astra_production_artifacts.py"
DEPLOY_SCRIPT = ROOT / "scripts/deploy-astra-production.sh"


def _load_cleanup_module():
    spec = importlib.util.spec_from_file_location(
        "cleanup_astra_production_artifacts",
        CLEANUP_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_cleanup_module()


def _release_id(index: int) -> str:
    return f"202608{index:02d}-120000-{index:012x}-{index:08x}-astra-saas"


def test_release_id_pattern_accepts_current_and_legacy_managed_names() -> None:
    assert cleanup.RELEASE_ID_PATTERN.fullmatch(
        "20260815-120000-000000000001-00000001-astra-saas"
    )
    assert cleanup.RELEASE_ID_PATTERN.fullmatch(
        "20260806-022030-aba742674a35-dfc49a84-clawith-saas"
    )


def _production_fixture(tmp_path: Path, count: int = 10) -> tuple[Path, list[str]]:
    app_root = tmp_path / "astra-poc"
    releases = app_root / "releases"
    backups = app_root / "backups"
    releases.mkdir(parents=True)
    backups.mkdir()
    release_ids = [_release_id(index) for index in range(1, count + 1)]
    for index, release_id in enumerate(release_ids, start=1):
        release = releases / release_id
        release.mkdir()
        (release / "payload").write_bytes(b"r" * index)
        backup = backups / release_id
        backup.mkdir()
        (backup / "db.sql.gz").write_bytes(b"b" * index)
    active_id = release_ids[-1]
    standby_id = release_ids[-2]
    (app_root / "current").symlink_to(releases / active_id)
    (app_root / "active-state").write_text(
        f"slot=b release={active_id}\n",
        encoding="utf-8",
    )
    (app_root / "active-release").write_text(f"{active_id}\n", encoding="utf-8")
    (app_root / "slot-a-release").write_text(
        f"{releases / standby_id}\n",
        encoding="utf-8",
    )
    (app_root / "slot-b-release").write_text(
        f"{releases / active_id}\n",
        encoding="utf-8",
    )
    (app_root / "cutover-state").write_text(
        f"complete slot=b release={active_id}\n",
        encoding="utf-8",
    )
    return app_root, release_ids


def test_plan_preserves_authoritative_and_recent_artifacts(tmp_path: Path) -> None:
    app_root, release_ids = _production_fixture(tmp_path)

    plan = cleanup.build_cleanup_plan(app_root, keep_recent=3, keep_daily_days=0)

    assert set(plan.protected_release_ids) == set(release_ids[-2:])
    assert set(plan.retained_recent_release_ids) == set(release_ids[-3:])
    assert set(plan.retained_recent_backup_ids) == set(release_ids[-3:])
    assert {target.release_id for target in plan.filesystem_targets} == set(
        release_ids[:-3]
    )


def test_apply_removes_only_planned_managed_directories(tmp_path: Path) -> None:
    app_root, release_ids = _production_fixture(tmp_path)
    unrelated = app_root / "backups" / "manual-do-not-delete"
    unrelated.mkdir()
    plan = cleanup.build_cleanup_plan(app_root, keep_recent=3, keep_daily_days=0)

    result = cleanup.apply_cleanup_plan(plan)

    assert set(result["removed_filesystem"]) == {
        target.path for target in plan.filesystem_targets
    }
    for release_id in release_ids[:-3]:
        assert not (app_root / "releases" / release_id).exists()
        assert not (app_root / "backups" / release_id).exists()
    for release_id in release_ids[-3:]:
        assert (app_root / "releases" / release_id).is_dir()
        assert (app_root / "backups" / release_id).is_dir()
    assert unrelated.is_dir()


def test_managed_named_symlink_fails_closed(tmp_path: Path) -> None:
    app_root, release_ids = _production_fixture(tmp_path, count=3)
    target = tmp_path / "outside"
    target.mkdir()
    suspicious_id = _release_id(11)
    (app_root / "backups" / suspicious_id).symlink_to(target)

    with pytest.raises(cleanup.CleanupSafetyError, match="not a real directory"):
        cleanup.build_cleanup_plan(app_root, keep_recent=2, keep_daily_days=0)

    assert (app_root / "releases" / release_ids[-1]).is_dir()
    assert target.is_dir()


def test_docker_targets_are_namespace_scoped_and_protect_slots() -> None:
    active_id = _release_id(10)
    old_id = _release_id(2)

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["docker", "image", "ls"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                f"astra-backend|{active_id}|active\n"
                f"astra-backend|{old_id}|old-backend\n"
                f"astra-browser-smoke|{old_id}|old-smoke\n"
                f"quantagent-python|{old_id}|foreign\n"
                "astra-backend|latest|legacy\n"
            ),
            stderr="",
        )

    assert cleanup.managed_docker_refs(
        protected_release_ids={active_id},
        runner=runner,
    ) == (
        f"astra-backend:{old_id}",
        f"astra-browser-smoke:{old_id}",
    )


def test_daily_retention_keeps_newest_artifact_per_day(tmp_path: Path) -> None:
    app_root, release_ids = _production_fixture(tmp_path)

    plan = cleanup.build_cleanup_plan(
        app_root,
        keep_recent=2,
        keep_daily_days=3,
    )

    assert set(plan.retained_daily_release_ids) == set(release_ids[-3:])
    assert set(plan.retained_daily_backup_ids) == set(release_ids[-3:])


def test_apply_cli_requires_deploy_lock_confirmation(tmp_path: Path) -> None:
    app_root, _release_ids = _production_fixture(tmp_path, count=3)
    result = subprocess.run(
        [
            sys.executable,
            str(CLEANUP_SCRIPT),
            "--app-root",
            str(app_root),
            "--apply",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--apply requires --lock-held" in result.stderr


def test_deploy_runs_retention_only_after_complete_cutover() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    completion = script.index(
        'write_cutover_state complete "$CANDIDATE_SLOT" "$RELEASE_ID"'
    )
    retention = script.index(
        'python3 "$RELEASE/scripts/cleanup_astra_production_artifacts.py"'
    )
    trap_release = script.index("trap - ERR HUP INT TERM", completion)

    assert completion < retention < trap_release
    retention_block = script[retention:trap_release]
    assert "--app-root \"$APP_ROOT\"" in retention_block
    assert "--keep-recent 7" in retention_block
    assert "--keep-daily-days 14" in retention_block
    assert "--prune-docker" in retention_block
    assert "--apply" in retention_block
    assert "--lock-held" in retention_block
    assert "--summary-only" in retention_block
    assert script[completion:retention].rstrip().endswith("if !")
