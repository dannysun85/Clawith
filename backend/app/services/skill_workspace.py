"""Agent workspace deployment and import rules for product Skills.

A Skill is an instruction package. Installing one into an Agent workspace must
never grant executable Tools; Tool grants are managed separately through
``AgentTool``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from app.services.storage_runtime.base import WriteCondition


_MANAGED_SKILL_MANIFEST = ".astra-managed.json"
_MANAGED_SKILL_MANIFEST_VERSION = 1
_IMPORTED_SKILL_MANIFEST = ".astra-import.json"
_IMPORTED_SKILL_MANIFEST_VERSION = 1
_MAX_SKILL_PACKAGE_BYTES = 512_000


class SkillWorkspaceError(ValueError):
    """Base error for an invalid Agent workspace Skill operation."""


class SkillWorkspaceConflictError(SkillWorkspaceError):
    """Raised when an import would overwrite an existing Skill folder."""


def validate_skill_folder_name(folder_name: str) -> str:
    """Return a safe, single-segment Skill folder name."""
    folder = str(folder_name or "").strip()
    if (
        not folder
        or len(folder) > 100
        or folder in {".", ".."}
        or "/" in folder
        or "\\" in folder
        or "\x00" in folder
    ):
        raise SkillWorkspaceError("Invalid skill folder name")
    return folder


def validate_skill_file_path(file_path: str) -> str:
    """Return a safe relative POSIX path inside a Skill folder."""
    path = str(file_path or "").strip()
    parsed = PurePosixPath(path)
    if (
        not path
        or len(path) > 500
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or "\\" in path
        or "\x00" in path
    ):
        raise SkillWorkspaceError("Invalid skill file path")
    return "SKILL.md" if path.casefold() == "skill.md" else path


def _skill_file_map(skill: Any) -> dict[str, str]:
    return {
        validate_skill_file_path(str(skill_file.path)): str(skill_file.content)
        for skill_file in skill.files
    }


def _skill_content_hash(files: Mapping[str, str]) -> str:
    """Hash both paths and content so registry edits trigger a workspace sync."""
    hasher = hashlib.sha256()
    for path, content in sorted(files.items()):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


async def _stored_skill_hash(storage: Any, prefix: str, paths: list[str]) -> str | None:
    current: dict[str, str] = {}
    for path in paths:
        key = f"{prefix}/{path}"
        if not await storage.is_file(key):
            return None
        current[path] = await storage.read_text(key, encoding="utf-8")
    return _skill_content_hash(current)


async def _relative_skill_files(storage: Any, prefix: str) -> set[str]:
    """List every file below a Skill folder using storage-neutral keys."""
    normalized = prefix.strip("/")
    pending = [normalized]
    files: set[str] = set()
    while pending:
        current = pending.pop()
        for entry in await storage.list_dir(current):
            key = entry.key.strip("/")
            if entry.is_dir:
                pending.append(key)
                continue
            marker = f"{normalized}/"
            if key.startswith(marker):
                files.add(key.removeprefix(marker))

    # Local storage intentionally hides .gitkeep from list_dir(). It still
    # counts as user-owned content when deciding whether a folder is safe to
    # adopt or replace.
    if await storage.is_file(f"{normalized}/.gitkeep"):
        files.add(".gitkeep")
    return files


async def _sync_managed_skill(
    storage: Any,
    prefix: str,
    skill: Any,
    *,
    provisioning: str = "automatic",
    accepted_legacy_hashes: frozenset[str] = frozenset(),
) -> tuple[str, int]:
    """Safely sync one registry Skill into an Agent workspace.

    Registry-managed files are updated only when the previous managed version
    is still intact. A user-edited or partially adopted folder is reported as
    a conflict and left untouched.
    """
    target_files = _skill_file_map(skill)
    target_paths = sorted(target_files)
    target_hash = _skill_content_hash(target_files)
    manifest_key = f"{prefix}/{_MANAGED_SKILL_MANIFEST}"
    target_source_skill_id = str(skill.id)
    target_source_tenant_id = str(skill.tenant_id) if skill.tenant_id else None
    if provisioning not in {"automatic", "user_selected"}:
        raise SkillWorkspaceError("Invalid managed Skill provisioning source")

    old_paths: list[str] = []
    if await storage.is_file(manifest_key):
        try:
            manifest = json.loads(await storage.read_text(manifest_key, encoding="utf-8"))
            raw_paths = manifest.get("paths", [])
            if not isinstance(raw_paths, list):
                return "conflict", 0
            old_paths = [validate_skill_file_path(path) for path in raw_paths]
            old_hash = str(manifest.get("content_hash") or "")
        except (SkillWorkspaceError, TypeError, ValueError, json.JSONDecodeError):
            return "conflict", 0
        if (
            manifest.get("schema_version") != _MANAGED_SKILL_MANIFEST_VERSION
            or not old_paths
            or len(old_paths) != len(set(old_paths))
            or not old_hash
        ):
            return "conflict", 0
        actual_paths = await _relative_skill_files(storage, prefix)
        if actual_paths != {*old_paths, _MANAGED_SKILL_MANIFEST}:
            return "conflict", 0
        if await _stored_skill_hash(storage, prefix, old_paths) != old_hash:
            return "conflict", 0
        if (
            old_paths == target_paths
            and old_hash == target_hash
            and manifest.get("source_skill_id") == target_source_skill_id
            and manifest.get("source_tenant_id") == target_source_tenant_id
            and manifest.get("provisioning") == provisioning
        ):
            return "unchanged", 0
    else:
        actual_paths = await _relative_skill_files(storage, prefix)
        if actual_paths:
            if actual_paths != set(target_paths):
                return "conflict", 0
            actual_hash = await _stored_skill_hash(storage, prefix, target_paths)
            if (
                actual_hash != target_hash
                and actual_hash not in accepted_legacy_hashes
            ):
                return "conflict", 0
            status = "adopted" if actual_hash == target_hash else "updated"
        else:
            status = "created"

        if status == "adopted":
            manifest = {
                "schema_version": _MANAGED_SKILL_MANIFEST_VERSION,
                "source_skill_id": target_source_skill_id,
                "source_tenant_id": target_source_tenant_id,
                "provisioning": provisioning,
                "content_hash": target_hash,
                "paths": target_paths,
            }
            await storage.write_text(
                manifest_key,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return status, 0
        old_paths = target_paths if status == "updated" else []

    for obsolete_path in sorted(set(old_paths) - set(target_paths)):
        obsolete_key = f"{prefix}/{obsolete_path}"
        if await storage.is_file(obsolete_key):
            await storage.delete(obsolete_key)

    for path, content in target_files.items():
        await storage.write_text(f"{prefix}/{path}", content, encoding="utf-8")

    manifest = {
        "schema_version": _MANAGED_SKILL_MANIFEST_VERSION,
        "source_skill_id": target_source_skill_id,
        "source_tenant_id": target_source_tenant_id,
        "provisioning": provisioning,
        "content_hash": target_hash,
        "paths": target_paths,
    }
    await storage.write_text(
        manifest_key,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ("updated" if old_paths else "created"), len(target_files)


async def _remove_stale_automatic_skill(
    storage: Any,
    prefix: str,
    *,
    accepted_legacy_hashes: frozenset[str] = frozenset(),
) -> str:
    """Remove a no-longer-desired Skill only when system ownership is proven.

    ``user_selected`` and externally imported packages are always preserved.
    Older workspaces predate the provisioning marker, so their historical
    ambient copies may only be removed when every path and byte matches a
    reviewed package hash. Any edit, extra file, malformed manifest, or
    ambiguous origin fails closed as ``conflict``.
    """
    actual_paths = await _relative_skill_files(storage, prefix)
    if not actual_paths:
        return "absent"
    if _IMPORTED_SKILL_MANIFEST in actual_paths:
        return "preserved"

    manifest_key = f"{prefix}/{_MANAGED_SKILL_MANIFEST}"
    if _MANAGED_SKILL_MANIFEST in actual_paths:
        try:
            manifest = json.loads(await storage.read_text(manifest_key, encoding="utf-8"))
            raw_paths = manifest.get("paths", [])
            if not isinstance(raw_paths, list):
                return "conflict"
            paths = [validate_skill_file_path(path) for path in raw_paths]
            stored_hash = str(manifest.get("content_hash") or "")
        except (SkillWorkspaceError, TypeError, ValueError, json.JSONDecodeError):
            return "conflict"

        provisioning = manifest.get("provisioning")
        if provisioning == "user_selected":
            return "preserved"
        if (
            manifest.get("schema_version") != _MANAGED_SKILL_MANIFEST_VERSION
            or not paths
            or len(paths) != len(set(paths))
            or not stored_hash
            or actual_paths != {*paths, _MANAGED_SKILL_MANIFEST}
        ):
            return "conflict"
        try:
            actual_hash = await _stored_skill_hash(storage, prefix, paths)
        except Exception:
            return "conflict"
        if actual_hash != stored_hash:
            return "conflict"
        if provisioning != "automatic" and stored_hash not in accepted_legacy_hashes:
            return "conflict"
    else:
        try:
            actual_hash = await _stored_skill_hash(storage, prefix, sorted(actual_paths))
        except Exception:
            return "conflict"
        if actual_hash not in accepted_legacy_hashes:
            return "conflict"

    await storage.delete_tree(prefix)
    return "removed"


async def deploy_skills_to_agent_workspace(
    agent_id: Any,
    skills: list[Any],
    *,
    provisioning: str = "automatic",
) -> dict[str, int]:
    """Deploy resolved registry Skills with managed-file protection."""
    from app.services.agent_manager import agent_manager
    from app.services.storage import get_storage_backend

    storage = get_storage_backend()
    agent_prefix = agent_manager._agent_storage_prefix(agent_id)
    stats = {
        "files": 0,
        "created": 0,
        "updated": 0,
        "adopted": 0,
        "unchanged": 0,
        "conflicts": 0,
    }
    for skill in skills:
        if not skill.files:
            continue
        status, changed_files = await _sync_managed_skill(
            storage,
            f"{agent_prefix}/skills/{validate_skill_folder_name(skill.folder_name)}",
            skill,
            provisioning=provisioning,
        )
        if status == "conflict":
            stats["conflicts"] += 1
        else:
            stats[status] += 1
        stats["files"] += changed_files
    return stats


def normalize_skill_snapshot_files(
    files: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Validate and normalize an external Skill package without writing it."""
    normalized: dict[str, str] = {}
    total_size = 0
    for item in files:
        path = validate_skill_file_path(str(item.get("path") or ""))
        if path in normalized:
            raise SkillWorkspaceError(f"Duplicate skill file path: {path}")
        content = item.get("content", "")
        if not isinstance(content, str):
            raise SkillWorkspaceError(f"Skill file must be UTF-8 text: {path}")
        if "\x00" in content:
            raise SkillWorkspaceError(f"Skill file contains null bytes: {path}")
        total_size += len(content.encode("utf-8"))
        if total_size > _MAX_SKILL_PACKAGE_BYTES:
            raise SkillWorkspaceError("Skill package exceeds 500KB")
        normalized[path] = content
    if "SKILL.md" not in normalized:
        raise SkillWorkspaceError("Skill package must contain a root SKILL.md")
    return normalized


async def import_skill_snapshot_to_agent_workspace(
    *,
    storage: Any,
    agent_prefix: str,
    folder_name: str,
    files: Sequence[Mapping[str, Any]],
    source: str,
) -> dict[str, Any]:
    """Install a user-requested external Skill snapshot without overwriting.

    The import marker claims the target folder before its files are written so
    concurrent imports cannot silently mix two packages. Existing folders are
    deliberately left untouched; the user must rename or delete them first.
    """
    folder = validate_skill_folder_name(folder_name)
    normalized = normalize_skill_snapshot_files(files)
    prefix = f"{agent_prefix}/skills/{folder}"
    if await storage.exists(prefix) or await storage.is_dir(prefix):
        raise SkillWorkspaceConflictError(
            f"Skill folder '{folder}' already exists; rename or delete it before importing"
        )

    manifest = {
        "schema_version": _IMPORTED_SKILL_MANIFEST_VERSION,
        "source": str(source or "external")[:1000],
        "content_hash": _skill_content_hash(normalized),
        "paths": sorted(normalized),
    }
    marker_key = f"{prefix}/{_IMPORTED_SKILL_MANIFEST}"
    marker_result = await storage.write_bytes_if_match(
        marker_key,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        condition=WriteCondition(require_absent=True),
        content_type="application/json; charset=utf-8",
    )
    if not marker_result.ok:
        raise SkillWorkspaceConflictError(
            f"Skill folder '{folder}' is being imported or already exists"
        )

    try:
        for path, content in normalized.items():
            await storage.write_text(f"{prefix}/{path}", content, encoding="utf-8")
    except Exception:
        await storage.delete_tree(prefix)
        raise

    return {
        "folder_name": folder,
        "files_written": len(normalized),
        "files": sorted(normalized),
    }
