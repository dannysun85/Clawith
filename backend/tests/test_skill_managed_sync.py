from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.services.skill_workspace import (
    _remove_stale_automatic_skill,
    _skill_content_hash,
    _sync_managed_skill,
    deploy_skills_to_agent_workspace,
)
from app.services.storage_runtime.base import StorageEntry


class MemoryStorage:
    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def read_text(self, key: str, **_kwargs) -> str:
        return self.files[key]

    async def write_text(self, key: str, content: str, **_kwargs) -> None:
        self.files[key] = content

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)

    async def delete_tree(self, key: str) -> None:
        prefix = f"{key.rstrip('/')}/"
        self.files = {
            path: content
            for path, content in self.files.items()
            if path != key and not path.startswith(prefix)
        }

    async def is_dir(self, key: str) -> bool:
        prefix = f"{key.rstrip('/')}/"
        return any(path.startswith(prefix) for path in self.files)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = f"{key.rstrip('/')}/"
        children: dict[str, bool] = {}
        for path in self.files:
            if not path.startswith(prefix):
                continue
            remainder = path.removeprefix(prefix)
            if not remainder:
                continue
            name, separator, _rest = remainder.partition("/")
            children[name] = children.get(name, False) or bool(separator)
        return [
            StorageEntry(
                name=name,
                key=f"{key.rstrip('/')}/{name}",
                is_dir=is_dir,
            )
            for name, is_dir in sorted(children.items())
        ]


def _skill(content: str, *, extra: bool = False, folder_name: str = "example"):
    files = [SimpleNamespace(path="SKILL.md", content=content)]
    if extra:
        files.append(SimpleNamespace(path="references/guide.md", content="guide"))
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        folder_name=folder_name,
        files=files,
    )


@pytest.mark.asyncio
async def test_managed_skill_create_update_and_obsolete_cleanup() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/example"

    status, changed = await _sync_managed_skill(
        storage,
        prefix,
        _skill("v1", extra=True),
    )
    assert (status, changed) == ("created", 2)
    assert storage.files[f"{prefix}/SKILL.md"] == "v1"
    assert f"{prefix}/.astra-managed.json" in storage.files

    status, changed = await _sync_managed_skill(storage, prefix, _skill("v2"))
    assert (status, changed) == ("updated", 1)
    assert storage.files[f"{prefix}/SKILL.md"] == "v2"
    assert f"{prefix}/references/guide.md" not in storage.files

    status, changed = await _sync_managed_skill(storage, prefix, _skill("v2"))
    # A different registry identity with identical content still refreshes the
    # provenance marker rather than being treated as the same source.
    assert (status, changed) == ("updated", 1)


@pytest.mark.asyncio
async def test_managed_skill_unchanged_registry_source_is_not_rewritten() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/example"
    skill = _skill("registry")
    await _sync_managed_skill(storage, prefix, skill)

    status, changed = await _sync_managed_skill(storage, prefix, skill)

    assert (status, changed) == ("unchanged", 0)


@pytest.mark.asyncio
async def test_managed_skill_preserves_user_edits_as_conflict() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/example"
    await _sync_managed_skill(storage, prefix, _skill("registry-v1"))
    storage.files[f"{prefix}/SKILL.md"] = "user customization"

    status, changed = await _sync_managed_skill(storage, prefix, _skill("registry-v2"))

    assert (status, changed) == ("conflict", 0)
    assert storage.files[f"{prefix}/SKILL.md"] == "user customization"


@pytest.mark.asyncio
async def test_deploy_managed_skills_counts_conflicts_without_raising(monkeypatch) -> None:
    from app.services.agent_manager import agent_manager
    from app.services import storage as storage_service

    storage = MemoryStorage()
    skill = _skill("registry-v1")
    prefix = "agents/a/skills/example"
    await _sync_managed_skill(storage, prefix, skill)
    storage.files[f"{prefix}/SKILL.md"] = "user customization"

    monkeypatch.setattr(storage_service, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(
        agent_manager,
        "_agent_storage_prefix",
        lambda _agent_id: "agents/a",
    )

    stats = await deploy_skills_to_agent_workspace(
        uuid.uuid4(),
        [skill],
        provisioning="user_selected",
    )

    assert stats["conflicts"] == 1
    assert stats["files"] == 0
    assert storage.files[f"{prefix}/SKILL.md"] == "user customization"


@pytest.mark.asyncio
async def test_managed_skill_preserves_user_added_files_as_conflict() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/example"
    await _sync_managed_skill(storage, prefix, _skill("registry-v1"))
    storage.files[f"{prefix}/notes/private.md"] = "user-owned"

    status, changed = await _sync_managed_skill(storage, prefix, _skill("registry-v2"))

    assert (status, changed) == ("conflict", 0)
    assert storage.files[f"{prefix}/SKILL.md"] == "registry-v1"
    assert storage.files[f"{prefix}/notes/private.md"] == "user-owned"


@pytest.mark.asyncio
async def test_managed_skill_adopts_an_exact_legacy_copy() -> None:
    prefix = "agents/a/skills/example"
    storage = MemoryStorage({f"{prefix}/SKILL.md": "registry"})

    status, changed = await _sync_managed_skill(storage, prefix, _skill("registry"))

    assert (status, changed) == ("adopted", 0)
    assert f"{prefix}/.astra-managed.json" in storage.files


@pytest.mark.asyncio
async def test_managed_skill_does_not_adopt_a_partial_folder() -> None:
    prefix = "agents/a/skills/example"
    storage = MemoryStorage({f"{prefix}/SKILL.md": "registry"})

    status, changed = await _sync_managed_skill(
        storage,
        prefix,
        _skill("registry", extra=True),
    )

    assert (status, changed) == ("conflict", 0)
    assert f"{prefix}/.astra-managed.json" not in storage.files


@pytest.mark.asyncio
async def test_stale_automatic_skill_is_removed_only_when_intact() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/old-role"
    await _sync_managed_skill(storage, prefix, _skill("registry"))

    assert await _remove_stale_automatic_skill(storage, prefix) == "removed"
    assert not any(path.startswith(f"{prefix}/") for path in storage.files)


@pytest.mark.asyncio
async def test_stale_automatic_skill_preserves_user_edit_as_conflict() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/old-role"
    await _sync_managed_skill(storage, prefix, _skill("registry"))
    storage.files[f"{prefix}/SKILL.md"] = "user edit"

    assert await _remove_stale_automatic_skill(storage, prefix) == "conflict"
    assert storage.files[f"{prefix}/SKILL.md"] == "user edit"


@pytest.mark.asyncio
async def test_stale_user_selected_skill_is_never_removed() -> None:
    storage = MemoryStorage()
    prefix = "agents/a/skills/optional"
    await _sync_managed_skill(
        storage,
        prefix,
        _skill("registry"),
        provisioning="user_selected",
    )

    assert await _remove_stale_automatic_skill(storage, prefix) == "preserved"
    assert storage.files[f"{prefix}/SKILL.md"] == "registry"


@pytest.mark.asyncio
async def test_exact_historical_ambient_copy_without_manifest_is_removed() -> None:
    prefix = "agents/a/skills/old-ambient"
    files = {"SKILL.md": "historical registry bytes"}
    storage = MemoryStorage(
        {f"{prefix}/{path}": content for path, content in files.items()}
    )

    status = await _remove_stale_automatic_skill(
        storage,
        prefix,
        accepted_legacy_hashes=frozenset({_skill_content_hash(files)}),
    )

    assert status == "removed"
    assert not any(path.startswith(f"{prefix}/") for path in storage.files)


@pytest.mark.asyncio
async def test_unproven_or_external_stale_skill_is_preserved() -> None:
    prefix = "agents/a/skills/optional"
    storage = MemoryStorage({f"{prefix}/SKILL.md": "user-owned"})

    assert await _remove_stale_automatic_skill(storage, prefix) == "conflict"
    storage.files[f"{prefix}/.astra-import.json"] = "{}"
    assert await _remove_stale_automatic_skill(storage, prefix) == "preserved"
    assert storage.files[f"{prefix}/SKILL.md"] == "user-owned"
