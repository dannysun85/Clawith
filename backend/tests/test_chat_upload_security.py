import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app.api import upload as upload_api


class FakeStorage:
    def __init__(self):
        self.writes: list[tuple[str, bytes, str | None]] = []

    async def exists(self, _key: str) -> bool:
        return False

    async def write_bytes(self, key: str, content: bytes, content_type: str | None = None):
        self.writes.append((key, content, content_type))


def make_upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


@pytest.mark.asyncio
async def test_chat_upload_requires_access_to_requested_agent(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    storage = FakeStorage()
    checked: list[tuple[object, object, uuid.UUID]] = []

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        checked.append((db, current_user, requested_agent_id))
        return SimpleNamespace(id=requested_agent_id), "use"

    async def fake_ensure_local_path(_key):
        target = tmp_path / "note.txt"
        target.write_text("private note", encoding="utf-8")
        return target

    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(upload_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(upload_api, "ensure_local_path", fake_ensure_local_path)

    result = await upload_api.upload_file(
        file=make_upload("note.txt", b"private note"),
        agent_id=str(agent_id),
        current_user=user,
        db="db-session",
    )

    assert checked == [("db-session", user, agent_id)]
    assert storage.writes[0][0] == f"{agent_id}/workspace/uploads/note.txt"
    assert result["extracted_text"] == "private note"


@pytest.mark.asyncio
async def test_chat_upload_rejects_invalid_agent_id():
    with pytest.raises(HTTPException) as exc_info:
        await upload_api.upload_file(
            file=make_upload("note.txt", b"private note"),
            agent_id="../../another-agent",
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db="db-session",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid agent_id"


@pytest.mark.asyncio
async def test_chat_upload_stops_reading_above_size_limit():
    with pytest.raises(HTTPException) as exc_info:
        await upload_api._read_upload_with_limit(make_upload("large.bin", b"1234"), limit=3)

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_oversized_image_is_rejected_before_storage_write(monkeypatch):
    storage = FakeStorage()
    monkeypatch.setattr(upload_api, "get_storage_backend", lambda: storage)

    with pytest.raises(HTTPException) as exc_info:
        await upload_api.upload_file(
            file=make_upload("large.png", b"x" * (10 * 1024 * 1024 + 1)),
            agent_id="",
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db="db-session",
        )

    assert exc_info.value.status_code == 413
    assert storage.writes == []
