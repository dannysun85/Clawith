from contextlib import asynccontextmanager
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.services import agent_tools
from app.services import email_service
from app.services import workspace_collaboration
from app.services.storage_runtime.base import StorageBackend, StorageEntry, StorageVersion, WriteCondition, ConditionalWriteResult
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.local import LocalStorageBackend


@asynccontextmanager
async def _noop_workspace_locks(*_args, **_kwargs):
    yield


@pytest.fixture(autouse=True)
def _isolate_storage_semantics_from_distributed_locking(monkeypatch):
    """These in-memory storage tests do not exercise the Redis lock backend."""
    monkeypatch.setattr(agent_tools, "workspace_locks", _noop_workspace_locks)
    monkeypatch.setattr(
        workspace_collaboration,
        "workspace_locks",
        _noop_workspace_locks,
    )


class MemoryStorageBackend(StorageBackend):
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.versions = {key: 1 for key in self.files}

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.files)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries: dict[str, StorageEntry] = {}
        for existing, data in self.files.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries.values(), key=lambda entry: (not entry.is_dir, entry.name))

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data
        self.versions[key] = self.versions.get(key, 0) + 1

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.versions.pop(key, None)

    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.files):
            if existing.startswith(prefix):
                self.files.pop(existing)
                self.versions.pop(existing, None)

    async def stat(self, key: str) -> StorageEntry:
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.files[key]))

    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.files:
            return StorageVersion(key=key, exists=False, is_dir=False)
        version = str(self.versions.get(key, 0))
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.files[key]),
            version_id=version,
            etag=version,
            content_hash=version,
        )

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        current = await self.get_version(key)
        if condition:
            if condition.require_absent and current.exists:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))


@pytest.mark.asyncio
async def test_agent_file_tools_use_storage_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/notes.md": b"# Notes\nneedle\n",
        f"{agent_id}/memory/memory.md": b"# Memory\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "workspace")
    read = await agent_tools._storage_read_file(agent_id, "workspace/notes.md")
    search = await agent_tools._storage_search_files(agent_id, "needle", path="workspace", file_pattern="*.md")
    found = await agent_tools._storage_find_files(agent_id, "*.md", path="workspace")

    assert "notes.md" in listing
    assert "needle" in read
    assert "workspace/notes.md:2" in search
    assert "workspace/notes.md" in found


@pytest.mark.asyncio
async def test_agent_file_tools_cannot_reach_service_private_recovery_evidence(monkeypatch):
    agent_id = uuid.uuid4()
    recovery_secret = b'{"ciphertext":"operator-only-secret"}'
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/notes.md": b"normal agent content\n",
            "_internal/provider_recovery/minimax/image/other/evidence.json": recovery_secret,
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "")
    read = await agent_tools._storage_read_file(
        agent_id,
        "_internal/provider_recovery/minimax/image/other/evidence.json",
    )
    search = await agent_tools._storage_search_files(
        agent_id,
        "operator.*secret",
        path=".",
    )

    assert "_internal" not in listing
    assert "operator-only-secret" not in read
    assert "operator-only-secret" not in search
    assert "No matches found" in search


@pytest.mark.asyncio
async def test_temp_workspace_materializes_only_requested_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
        f"{agent_id}/workspace/other.md": b"# Other\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        assert (temp_ws.root / "workspace" / "input.md").read_text(encoding="utf-8") == "# Input\n"
        assert not (temp_ws.root / "workspace" / "other.md").exists()
    finally:
        temp_ws.cleanup()


def test_explicit_media_paths_ignore_data_urls_and_never_fall_back_to_workspace():
    assert agent_tools._explicit_media_workspace_paths(
        "data:image/png;base64,AAAA",
        " workspace/images/product.png ",
        None,
    ) == ["workspace/images/product.png"]
    assert agent_tools._explicit_media_workspace_paths(None, "", "data:video/mp4;base64,AAAA") == []
    assert agent_tools._media_workspace_input_paths("uploads/product.png") == [
        "workspace/uploads/product.png"
    ]
    assert agent_tools._explicit_media_workspace_paths(
        "uploads/product.png",
        "https://example.invalid/product.png",
    ) == ["workspace/uploads/product.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_paths"),
    [
        (
            "generate_image_minimax",
            {
                "prompt": "product poster",
                "reference_image": "workspace/images/reference.png",
                "brand_asset": "data:image/png;base64,AAAA",
            },
            ["workspace/images/reference.png"],
        ),
        (
            "generate_video_minimax",
            {
                "prompt": "product video",
                "first_frame_image": "workspace/images/first.png",
                "last_frame_image": "workspace/images/last.png",
            },
            ["workspace/images/first.png", "workspace/images/last.png"],
        ),
        (
            "check_video_minimax",
            {"task_meta_path": "workspace/videos/task.json"},
            ["workspace/videos/task.json"],
        ),
    ],
)
async def test_minimax_dispatcher_materializes_only_explicit_media_inputs(
    monkeypatch,
    tool_name,
    arguments,
    expected_paths,
):
    captured: dict[str, object] = {}

    async def run_with_temp_workspace(_agent_id, _tenant_id, runner, **kwargs):
        captured["paths"] = kwargs.get("paths")
        return "ok"

    monkeypatch.setattr(agent_tools, "release_tool_denial_reason", lambda _name: None)
    monkeypatch.setattr(agent_tools, "_code_tool_denial_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", AsyncMock(return_value=str(uuid.uuid4())))
    monkeypatch.setattr(agent_tools, "_run_with_temp_workspace", run_with_temp_workspace)
    monkeypatch.setitem(agent_tools._TOOL_AUTONOMY_MAP, tool_name, None)

    result = await agent_tools.execute_tool(
        tool_name,
        arguments,
        uuid.uuid4(),
        uuid.uuid4(),
        session_id=str(uuid.uuid4()),
        saas_tier="lite",
    )

    assert result == "ok"
    assert captured["paths"] == expected_paths


@pytest.mark.asyncio
async def test_explicit_media_input_survives_unrelated_workspace_budget(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/large/unrelated.bin": b"x" * 16,
            f"{agent_id}/workspace/images/product.png": b"product",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "TOOL_MATERIALIZE_MAX_TOTAL_BYTES", 8)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=["workspace/images/product.png"],
    )
    try:
        assert (temp_ws.root / "workspace/images/product.png").read_bytes() == b"product"
        assert not (temp_ws.root / "workspace/large/unrelated.bin").exists()
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["other_agent", "external", "root_prefix_collision"])
async def test_send_channel_file_rejects_local_storage_symlink_before_materialization(
    monkeypatch,
    tmp_path,
    target_kind,
):
    storage_root = tmp_path / "storage"
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    source_agent_id = uuid.uuid4()
    source_workspace = storage_root / str(source_agent_id) / "workspace"
    source_workspace.mkdir(parents=True)
    if target_kind == "other_agent":
        victim = storage_root / str(uuid.uuid4()) / "workspace" / "private.txt"
    elif target_kind == "external":
        victim = tmp_path / "external" / "private.txt"
    else:
        victim = tmp_path / "storage-escape" / "private.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("other-agent-secret", encoding="utf-8")
    (source_workspace / "linked.txt").symlink_to(victim)

    storage = LocalStorageBackend(str(storage_root))
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    real_temporary_directory = agent_tools.tempfile.TemporaryDirectory
    created_temp_paths = []

    def make_temporary_directory(*args, **kwargs):
        temporary_directory = real_temporary_directory(*args, dir=temp_root, **kwargs)
        created_temp_paths.append(temporary_directory.name)
        return temporary_directory

    monkeypatch.setattr(agent_tools.tempfile, "TemporaryDirectory", make_temporary_directory)
    monkeypatch.setattr(
        agent_tools,
        "_get_agent_tenant_id",
        AsyncMock(return_value=None),
    )
    sender = AsyncMock(return_value=True)
    sender_token = agent_tools.channel_file_sender.set(sender)

    try:
        result = await agent_tools.execute_tool(
            "send_channel_file",
            {"file_path": "workspace/linked.txt"},
            source_agent_id,
            uuid.uuid4(),
        )
    finally:
        agent_tools.channel_file_sender.reset(sender_token)

    assert result.startswith("Tool execution error (send_channel_file): HTTPException")
    assert "other-agent-secret" not in result
    sender.assert_not_awaited()
    assert created_temp_paths
    assert all(not agent_tools.Path(path).exists() for path in created_temp_paths)
    assert victim.read_text(encoding="utf-8") == "other-agent-secret"


def test_local_storage_rejects_absolute_and_parent_traversal_keys(tmp_path):
    storage = LocalStorageBackend(str(tmp_path / "storage"))

    with pytest.raises(HTTPException) as absolute_error:
        storage._full_path("/etc/passwd")
    with pytest.raises(HTTPException) as traversal_error:
        storage._full_path("agent/workspace/../../other-agent/private.txt")

    assert absolute_error.value.status_code == 403
    assert traversal_error.value.status_code == 403


@pytest.mark.asyncio
async def test_local_storage_normal_read_write_and_listing_remain_available(tmp_path):
    storage = LocalStorageBackend(str(tmp_path / "storage"))

    await storage.write_bytes("agent-id/workspace/report.txt", b"normal-content")

    assert await storage.read_bytes("agent-id/workspace/report.txt") == b"normal-content"
    entries = await storage.list_dir("agent-id/workspace")
    assert [(entry.name, entry.key, entry.is_dir) for entry in entries] == [
        ("report.txt", "agent-id/workspace/report.txt", False),
    ]


@pytest.mark.parametrize(
    "rel_path",
    [
        "../other-agent/workspace/private.txt",
        "workspace/../../other-agent/private.txt",
        "/absolute/private.txt",
        r"C:\\private.txt",
    ],
)
def test_agent_storage_key_rejects_untrusted_scope_escape(rel_path):
    with pytest.raises(ValueError, match="must be relative"):
        agent_storage_key(uuid.uuid4(), rel_path)


@pytest.mark.asyncio
async def test_send_file_to_agent_rejects_scope_escape_before_storage_read(
    monkeypatch,
    tmp_path,
):
    source_agent_id = uuid.uuid4()
    victim_agent_id = uuid.uuid4()
    storage_root = tmp_path / "storage"
    victim_file = storage_root / str(victim_agent_id) / "workspace" / "private.txt"
    victim_file.parent.mkdir(parents=True)
    victim_file.write_text("victim-private-content", encoding="utf-8")

    storage = LocalStorageBackend(str(storage_root))
    is_file = AsyncMock(wraps=storage.is_file)
    read_bytes = AsyncMock(wraps=storage.read_bytes)
    monkeypatch.setattr(storage, "is_file", is_file)
    monkeypatch.setattr(storage, "read_bytes", read_bytes)
    storage_factory = Mock(return_value=storage)
    monkeypatch.setattr(agent_tools, "get_storage_backend", storage_factory)

    result = await agent_tools._send_file_to_agent(
        source_agent_id,
        {
            "agent_name": "target-agent",
            "file_path": f"../{victim_agent_id}/workspace/private.txt",
        },
    )

    assert "Source file path must stay within the Agent workspace" in result
    assert "victim-private-content" not in result
    storage_factory.assert_not_called()
    is_file.assert_not_awaited()
    read_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_attachment_rejects_scope_escape_before_storage_or_smtp(
    monkeypatch,
    tmp_path,
):
    source_agent_id = uuid.uuid4()
    victim_agent_id = uuid.uuid4()
    workspace_root = tmp_path / "storage" / str(source_agent_id)
    workspace_root.mkdir(parents=True)
    victim_file = tmp_path / "storage" / str(victim_agent_id) / "private.txt"
    victim_file.parent.mkdir(parents=True)
    victim_file.write_text("victim-email-content", encoding="utf-8")

    storage = LocalStorageBackend(str(tmp_path / "storage"))
    read_bytes = AsyncMock(wraps=storage.read_bytes)
    smtp_send = AsyncMock()
    monkeypatch.setattr(storage, "read_bytes", read_bytes)
    storage_factory = Mock(return_value=storage)
    monkeypatch.setattr(email_service, "get_storage_backend", storage_factory)
    monkeypatch.setattr(email_service, "send_smtp_email", smtp_send)

    result = await email_service.send_email(
        {
            "email_address": "sender@example.com",
            "auth_code": "test-auth-code",
            "smtp_host": "smtp.example.com",
        },
        "recipient@example.com",
        "subject",
        "body",
        attachments=[f"../{victim_agent_id}/private.txt"],
        workspace_path=workspace_root,
        agent_id=source_agent_id,
    )

    assert result == "❌ Attachment path must stay within the Agent workspace."
    assert "victim-email-content" not in result
    storage_factory.assert_not_called()
    read_bytes.assert_not_awaited()
    smtp_send.assert_not_called()


@pytest.mark.asyncio
async def test_execute_tool_list_files_does_not_create_persistent_workspace(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    async def _tenant(_agent_id):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _tenant)

    result = await agent_tools.execute_tool("list_files", {"path": "workspace"}, agent_id, agent_id)

    assert "input.md" in result
    assert not (tmp_path / str(agent_id)).exists()


@pytest.mark.asyncio
async def test_edit_workspace_missing_text_is_an_idempotent_warning(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/notes.md"
    storage = MemoryStorageBackend({storage_key: b"already updated\n"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    result = await agent_tools._execute_workspace_mutation(
        "edit_file",
        {
            "path": "workspace/notes.md",
            "old_string": "stale text",
            "new_string": "replacement",
        },
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        session_id=None,
    )

    assert result.startswith("⚠️ No changes made")
    assert storage.files[storage_key] == b"already updated\n"


def test_legacy_edit_file_missing_text_is_an_idempotent_warning(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    target = workspace / "notes.md"
    target.write_text("already updated\n", encoding="utf-8")

    result = agent_tools._edit_file(
        workspace,
        "notes.md",
        "stale text",
        "replacement",
    )

    assert result.startswith("⚠️ No changes made")
    assert target.read_text(encoding="utf-8") == "already updated\n"


@pytest.mark.asyncio
async def test_write_workspace_file_does_not_mirror_to_local_for_non_local_storage(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="hello",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"hello"
    assert not (tmp_path / str(agent_id) / "workspace" / "test.md").exists()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("## Identity\n- **Role**: Incident Response Lead\n", "Incident Response Lead"),
        ("## 身份\n- **角色**：增长运营负责人\n", "增长运营负责人"),
        ("# Soul\nNo explicit role field here.\n", None),
    ],
)
def test_extract_soul_role_description(content, expected):
    assert workspace_collaboration._extract_soul_role_description(content) == expected


@pytest.mark.asyncio
async def test_write_soul_syncs_agent_role_description(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    agent = type("AgentRecord", (), {"role_description": "Old role"})()

    class ScalarResult:
        def scalar_one_or_none(self):
            return agent

    class RoleDB:
        def __init__(self):
            self.flush_calls = 0

        async def execute(self, _statement):
            return ScalarResult()

        async def flush(self):
            self.flush_calls += 1

    db = RoleDB()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.write_workspace_file(
        db=db,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="soul.md",
        content="## Identity\n- **Role**: Reliability Engineer\n",
        actor_type="user",
        actor_id=uuid.uuid4(),
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert agent.role_description == "Reliability Engineer"
    assert db.flush_calls == 1
    assert storage.files[f"{agent_id}/soul.md"].decode() == "## Identity\n- **Role**: Reliability Engineer\n"


@pytest.mark.asyncio
async def test_flush_temp_workspace_only_writes_changed_files(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
        f"{agent_id}/workspace/other.md": b"# Other\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Updated\n", encoding="utf-8")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["updated"] == ["workspace/input.md"]
    assert "workspace/other.md" in result["skipped"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Updated\n"
    assert storage.files[f"{agent_id}/workspace/other.md"] == b"# Other\n"


@pytest.mark.asyncio
async def test_flush_temp_workspace_fails_on_conflict(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Local change\n", encoding="utf-8")
        await storage.write_bytes(f"{agent_id}/workspace/input.md", b"# Remote change\n")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["conflicted"] == ["workspace/input.md"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Remote change\n"


@pytest.mark.asyncio
async def test_write_workspace_file_fails_on_expected_version_conflict(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/test.md": b"old",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/test.md")
    await storage.write_bytes(f"{agent_id}/workspace/test.md", b"remote-new")
    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="local-new",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"remote-new"


@pytest.mark.asyncio
async def test_move_workspace_path_fails_when_source_changes(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/source.md": b"old",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/source.md")
    await storage.write_bytes(f"{agent_id}/workspace/source.md", b"remote-new")
    result = await workspace_collaboration.move_workspace_path(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        source_path="workspace/source.md",
        destination_path="workspace/dest.md",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_source_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert f"{agent_id}/workspace/dest.md" not in storage.files


@pytest.mark.asyncio
async def test_delete_workspace_directory_uses_prefix_existence(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/dir/a.txt": b"a",
        f"{agent_id}/workspace/dir/nested/b.txt": b"b",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.delete_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/dir",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert f"{agent_id}/workspace/dir/a.txt" not in storage.files
    assert f"{agent_id}/workspace/dir/nested/b.txt" not in storage.files
