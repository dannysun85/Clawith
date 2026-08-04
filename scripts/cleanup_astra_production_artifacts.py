#!/usr/bin/env python3
"""Safely retire stale Clawith production release artifacts.

The command is intentionally dry-run by default. Applied cleanup must run
while the production deploy lock is held; the deployment workflow supplies
that guarantee after a successful cutover.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any


RELEASE_ID_PATTERN = re.compile(
    r"^\d{8}-\d{6}-[0-9a-f]{12}-[0-9a-f]{8}-clawith-saas$"
)
ACTIVE_STATE_PATTERN = re.compile(
    r"^slot=(?:a|b|legacy) release=(?P<release_id>[A-Za-z0-9._-]+)$"
)
CUTOVER_STATE_PATTERN = re.compile(
    r"^[a-z_]+ slot=(?:a|b|legacy) release=(?P<release_id>[A-Za-z0-9._-]+)$"
)
PENDING_DRAIN_PATTERN = re.compile(
    r"^[A-Za-z0-9._-]+ (?:3008|3009) (?P<release_path>\S+)$"
)
MANAGED_DOCKER_REPOSITORIES = frozenset({"astra-backend", "astra-browser-smoke"})
DEFAULT_KEEP_RECENT = 7
DEFAULT_KEEP_DAILY_DAYS = 14
MAX_METADATA_BYTES = 4096


class CleanupSafetyError(RuntimeError):
    """Raised before mutation when production artifact authority is ambiguous."""


@dataclass(frozen=True)
class ArtifactTarget:
    kind: str
    path: str
    release_id: str
    estimated_bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    app_root: str
    protected_release_ids: tuple[str, ...]
    retained_recent_release_ids: tuple[str, ...]
    retained_recent_backup_ids: tuple[str, ...]
    retained_daily_release_ids: tuple[str, ...]
    retained_daily_backup_ids: tuple[str, ...]
    filesystem_targets: tuple[ArtifactTarget, ...]
    docker_targets: tuple[str, ...]


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _read_metadata(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise CleanupSafetyError(f"metadata is not a regular file: {path}")
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise CleanupSafetyError(f"metadata is too large: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise CleanupSafetyError(f"metadata is empty or multiline: {path}")
    return value


def _require_release_id(value: str, *, source: Path) -> str:
    if RELEASE_ID_PATTERN.fullmatch(value) is None:
        raise CleanupSafetyError(f"unmanaged release id in {source}: {value}")
    return value


def _release_id_from_path(
    value: str,
    *,
    source: Path,
    releases_root: Path,
) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise CleanupSafetyError(f"release pointer is not absolute: {source}")
    if candidate.is_symlink() or not candidate.is_dir():
        raise CleanupSafetyError(f"release pointer is not a real directory: {source}")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != releases_root or candidate != resolved:
        raise CleanupSafetyError(f"release pointer escapes managed root: {source}")
    return _require_release_id(resolved.name, source=source)


def _optional_release_path_pointer(
    path: Path,
    *,
    releases_root: Path,
) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _release_id_from_path(
        _read_metadata(path),
        source=path,
        releases_root=releases_root,
    )


def collect_protected_release_ids(app_root: Path) -> set[str]:
    if not app_root.is_absolute():
        raise CleanupSafetyError("app root must be absolute")
    if app_root.is_symlink() or not app_root.is_dir():
        raise CleanupSafetyError("app root must be an existing real directory")
    app_root = app_root.resolve(strict=True)
    releases_root = app_root / "releases"
    if releases_root.is_symlink() or not releases_root.is_dir():
        raise CleanupSafetyError("managed releases root is missing or symlinked")
    releases_root = releases_root.resolve(strict=True)

    current = app_root / "current"
    if not current.is_symlink():
        raise CleanupSafetyError("current must be a managed release symlink")
    current_target = current.resolve(strict=True)
    if current_target.parent != releases_root or current_target.is_symlink():
        raise CleanupSafetyError("current escapes the managed releases root")
    protected = {_require_release_id(current_target.name, source=current)}

    active_state_path = app_root / "active-state"
    active_state = _read_metadata(active_state_path)
    active_match = ACTIVE_STATE_PATTERN.fullmatch(active_state)
    if active_match is None:
        raise CleanupSafetyError("active-state has an invalid format")
    protected.add(
        _require_release_id(
            active_match.group("release_id"),
            source=active_state_path,
        )
    )

    active_release_path = app_root / "active-release"
    if active_release_path.exists() or active_release_path.is_symlink():
        protected.add(
            _require_release_id(
                _read_metadata(active_release_path),
                source=active_release_path,
            )
        )

    for slot in ("a", "b"):
        slot_path = app_root / f"slot-{slot}-release"
        release_id = _optional_release_path_pointer(
            slot_path,
            releases_root=releases_root,
        )
        if release_id:
            protected.add(release_id)

    cutover_state_path = app_root / "cutover-state"
    if cutover_state_path.exists() or cutover_state_path.is_symlink():
        cutover_state = _read_metadata(cutover_state_path)
        cutover_match = CUTOVER_STATE_PATTERN.fullmatch(cutover_state)
        if cutover_match is None:
            raise CleanupSafetyError("cutover-state has an invalid format")
        protected.add(
            _require_release_id(
                cutover_match.group("release_id"),
                source=cutover_state_path,
            )
        )

    pending_drain_path = app_root / "pending-drain"
    if pending_drain_path.exists() or pending_drain_path.is_symlink():
        pending_drain = _read_metadata(pending_drain_path)
        pending_match = PENDING_DRAIN_PATTERN.fullmatch(pending_drain)
        if pending_match is None:
            raise CleanupSafetyError("pending-drain has an invalid format")
        protected.add(
            _release_id_from_path(
                pending_match.group("release_path"),
                source=pending_drain_path,
                releases_root=releases_root,
            )
        )

    missing = [
        release_id
        for release_id in sorted(protected)
        if not (releases_root / release_id).is_dir()
        or (releases_root / release_id).is_symlink()
    ]
    if missing:
        raise CleanupSafetyError(
            "protected release directory is missing: " + ", ".join(missing)
        )
    return protected


def _managed_directories(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise CleanupSafetyError(f"managed artifact root is missing or symlinked: {root}")
    managed: list[Path] = []
    for candidate in root.iterdir():
        if RELEASE_ID_PATTERN.fullmatch(candidate.name) is None:
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise CleanupSafetyError(f"managed artifact is not a real directory: {candidate}")
        if candidate.resolve(strict=True).parent != root.resolve(strict=True):
            raise CleanupSafetyError(f"managed artifact escapes its root: {candidate}")
        managed.append(candidate)
    return sorted(managed, key=lambda path: path.name)


def _tree_size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name for name in directories if not (root_path / name).is_symlink()
        ]
        for name in files:
            child = root_path / name
            if not child.is_symlink():
                total += child.stat().st_size
    return total


def _daily_anchor_ids(paths: Sequence[Path], keep_daily_days: int) -> set[str]:
    if not paths or keep_daily_days == 0:
        return set()
    newest_day = max(
        date.fromisoformat(f"{path.name[:4]}-{path.name[4:6]}-{path.name[6:8]}")
        for path in paths
    )
    cutoff = newest_day - timedelta(days=keep_daily_days - 1)
    newest_by_day: dict[date, str] = {}
    for path in paths:
        release_day = date.fromisoformat(
            f"{path.name[:4]}-{path.name[4:6]}-{path.name[6:8]}"
        )
        if release_day < cutoff:
            continue
        newest_by_day[release_day] = max(newest_by_day.get(release_day, ""), path.name)
    return set(newest_by_day.values())


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def managed_docker_refs(
    *,
    protected_release_ids: set[str],
    runner: CommandRunner = _default_runner,
) -> tuple[str, ...]:
    result = runner(
        [
            "docker",
            "image",
            "ls",
            "--format",
            "{{.Repository}}|{{.Tag}}|{{.ID}}",
        ]
    )
    if result.returncode != 0:
        raise CleanupSafetyError("docker image inventory failed")
    targets: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise CleanupSafetyError("docker image inventory has an invalid format")
        repository, tag, _image_id = parts
        if repository not in MANAGED_DOCKER_REPOSITORIES:
            continue
        if RELEASE_ID_PATTERN.fullmatch(tag) is None:
            continue
        if tag not in protected_release_ids:
            targets.add(f"{repository}:{tag}")
    return tuple(sorted(targets))


def build_cleanup_plan(
    app_root: Path,
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    keep_daily_days: int = DEFAULT_KEEP_DAILY_DAYS,
    include_docker: bool = False,
    runner: CommandRunner = _default_runner,
) -> CleanupPlan:
    if keep_recent < 2 or keep_recent > 50:
        raise CleanupSafetyError("keep_recent must be between 2 and 50")
    if keep_daily_days < 0 or keep_daily_days > 365:
        raise CleanupSafetyError("keep_daily_days must be between 0 and 365")
    app_root = app_root.resolve(strict=True)
    protected = collect_protected_release_ids(app_root)
    releases = _managed_directories(app_root / "releases")
    backups = _managed_directories(app_root / "backups")
    retained_releases = {path.name for path in releases[-keep_recent:]}
    retained_backups = {path.name for path in backups[-keep_recent:]}
    daily_releases = _daily_anchor_ids(releases, keep_daily_days)
    daily_backups = _daily_anchor_ids(backups, keep_daily_days)

    targets: list[ArtifactTarget] = []
    for kind, paths, retained, daily in (
        ("release", releases, retained_releases, daily_releases),
        ("backup", backups, retained_backups, daily_backups),
    ):
        for path in paths:
            if path.name in protected or path.name in retained or path.name in daily:
                continue
            targets.append(
                ArtifactTarget(
                    kind=kind,
                    path=str(path),
                    release_id=path.name,
                    estimated_bytes=_tree_size(path),
                )
            )

    docker_targets = (
        managed_docker_refs(protected_release_ids=protected, runner=runner)
        if include_docker
        else ()
    )
    return CleanupPlan(
        app_root=str(app_root),
        protected_release_ids=tuple(sorted(protected)),
        retained_recent_release_ids=tuple(sorted(retained_releases)),
        retained_recent_backup_ids=tuple(sorted(retained_backups)),
        retained_daily_release_ids=tuple(sorted(daily_releases)),
        retained_daily_backup_ids=tuple(sorted(daily_backups)),
        filesystem_targets=tuple(targets),
        docker_targets=docker_targets,
    )


def _remove_artifact_target(app_root: Path, target: ArtifactTarget) -> None:
    kind_root = app_root / ("releases" if target.kind == "release" else "backups")
    candidate = Path(target.path)
    if (
        RELEASE_ID_PATTERN.fullmatch(target.release_id) is None
        or candidate.name != target.release_id
        or candidate.parent != kind_root
        or candidate.is_symlink()
        or not candidate.is_dir()
        or candidate.resolve(strict=True).parent != kind_root.resolve(strict=True)
    ):
        raise CleanupSafetyError(f"artifact changed after planning: {candidate}")
    shutil.rmtree(candidate)


def apply_cleanup_plan(
    plan: CleanupPlan,
    *,
    runner: CommandRunner = _default_runner,
) -> dict[str, Any]:
    app_root = Path(plan.app_root).resolve(strict=True)
    current_protected = collect_protected_release_ids(app_root)
    if current_protected != set(plan.protected_release_ids):
        raise CleanupSafetyError("release authority changed after planning")

    removed_filesystem: list[str] = []
    for target in plan.filesystem_targets:
        _remove_artifact_target(app_root, target)
        removed_filesystem.append(target.path)

    removed_docker: list[str] = []
    docker_errors: list[dict[str, Any]] = []
    current_docker_targets = (
        set(
            managed_docker_refs(
                protected_release_ids=current_protected,
                runner=runner,
            )
        )
        if plan.docker_targets
        else set()
    )
    for image_ref in plan.docker_targets:
        if image_ref not in current_docker_targets:
            continue
        result = runner(["docker", "image", "rm", image_ref])
        if result.returncode == 0:
            removed_docker.append(image_ref)
        else:
            docker_errors.append(
                {
                    "image_ref": image_ref,
                    "returncode": result.returncode,
                }
            )

    return {
        "removed_filesystem": removed_filesystem,
        "removed_docker": removed_docker,
        "docker_errors": docker_errors,
    }


def _disk_snapshot(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise CleanupSafetyError("report path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--keep-recent", type=int, default=DEFAULT_KEEP_RECENT)
    parser.add_argument(
        "--keep-daily-days",
        type=int,
        default=DEFAULT_KEEP_DAILY_DAYS,
    )
    parser.add_argument("--prune-docker", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--lock-held",
        action="store_true",
        help="Confirm that the caller already holds APP_ROOT/deploy.lock.",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.apply and not args.lock_held:
        raise CleanupSafetyError("--apply requires --lock-held")

    before = _disk_snapshot(args.app_root)
    plan = build_cleanup_plan(
        args.app_root,
        keep_recent=args.keep_recent,
        keep_daily_days=args.keep_daily_days,
        include_docker=args.prune_docker,
    )
    result: dict[str, Any] = {
        "removed_filesystem": [],
        "removed_docker": [],
        "docker_errors": [],
    }
    if args.apply:
        result = apply_cleanup_plan(plan)
    after = _disk_snapshot(args.app_root)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "keep_recent": args.keep_recent,
        "keep_daily_days": args.keep_daily_days,
        "plan": {
            **asdict(plan),
            "estimated_filesystem_bytes": sum(
                target.estimated_bytes for target in plan.filesystem_targets
            ),
        },
        "result": result,
        "disk_before": before,
        "disk_after": after,
        "disk_reclaimed": max(0, after["free"] - before["free"]),
    }
    if args.report:
        _write_report(args.report, payload)
    output = payload
    if args.summary_only:
        output = {
            "mode": payload["mode"],
            "protected_count": len(plan.protected_release_ids),
            "filesystem_target_count": len(plan.filesystem_targets),
            "docker_target_count": len(plan.docker_targets),
            "estimated_filesystem_bytes": payload["plan"][
                "estimated_filesystem_bytes"
            ],
            "removed_filesystem_count": len(result["removed_filesystem"]),
            "removed_docker_count": len(result["removed_docker"]),
            "docker_error_count": len(result["docker_errors"]),
            "disk_reclaimed": payload["disk_reclaimed"],
        }
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if result["docker_errors"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CleanupSafetyError as exc:
        print(f"cleanup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
