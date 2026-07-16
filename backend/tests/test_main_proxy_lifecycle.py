import asyncio
from pathlib import Path

import pytest

from app import main


class EmptyStream:
    async def read(self):
        return b""


class FakeProxyProcess:
    def __init__(self):
        self.pid = 4545
        self.returncode = None
        self.stderr = EmptyStream()
        self._finished = asyncio.Event()

    async def wait(self):
        await self._finished.wait()
        return self.returncode

    def finish(self):
        self.returncode = -15
        self._finished.set()


def test_release_version_info_prefers_deployment_environment(monkeypatch):
    monkeypatch.setenv("ASTRA_RELEASE_VERSION", "1.10.5")
    monkeypatch.setenv("ASTRA_RELEASE_COMMIT", "deadbee")
    monkeypatch.setenv("ASTRA_RELEASE_ID", "release-123")

    assert main._load_version_info() == {
        "version": "1.10.5",
        "commit": "deadbee",
        "release_id": "release-123",
    }


@pytest.mark.asyncio
async def test_ss_local_process_and_credential_file_are_managed(monkeypatch):
    proc = FakeProxyProcess()
    create_kwargs = {}
    terminated = []

    async def create_process(*_args, **kwargs):
        create_kwargs.update(kwargs)
        return proc

    async def no_sleep(_seconds):
        return None

    async def terminate(candidate):
        terminated.append(candidate)
        candidate.finish()

    monkeypatch.setattr(main.shutil, "which", lambda _name: "/usr/bin/ss-local")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main, "terminate_process_group", terminate)
    monkeypatch.delenv("SS_CONFIG_FILE", raising=False)
    monkeypatch.setenv("SS_SERVER", "127.0.0.1")
    monkeypatch.setenv("SS_PORT", "8388")
    monkeypatch.setenv("SS_PASSWORD", "test-secret")
    monkeypatch.setenv("SS_METHOD", "aes-256-gcm")

    resource = await main._start_ss_local()

    assert resource is not None
    _, config_path = resource
    assert Path(config_path).exists()
    assert create_kwargs["start_new_session"] is True

    await main._stop_ss_local(resource)

    assert terminated == [proc]
    assert not Path(config_path).exists()
