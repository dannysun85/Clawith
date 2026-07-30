import asyncio
import signal
import subprocess

import pytest

from app.services import agent_tools
from app.services import process_utils
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local import subprocess_backend as subprocess_backend_module
from app.services.sandbox.local.subprocess_backend import (
    SubprocessBackend,
    _bwrap_failure_error,
    _parse_memory_limit_bytes,
)


@pytest.mark.parametrize(
    "stderr",
    [
        "bwrap: No permissions to create new namespace, likely because the kernel does not allow it",
        "bwrap: Creating new namespace failed: Operation not permitted",
        "unshare failed: Operation not permitted",
    ],
)
def test_bwrap_namespace_failure_has_actionable_diagnostic(stderr):
    error = _bwrap_failure_error(stderr)

    assert error is not None
    assert "runtime cannot create the required Linux namespaces" in error
    assert "fail-closed" in error


def test_non_namespace_bwrap_failure_preserves_generic_error_path():
    assert _bwrap_failure_error("python: syntax error") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("256m", 256 * 1024**2),
        ("1g", 1024**3),
        ("1.5gb", int(1.5 * 1024**3)),
        ("1024kb", 1024**2),
    ],
)
def test_parse_memory_limit_bytes(value, expected):
    assert _parse_memory_limit_bytes(value) == expected


@pytest.mark.parametrize("value", ["", "0m", "-1g", "nope"])
def test_parse_memory_limit_bytes_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        _parse_memory_limit_bytes(value)


def test_bwrap_execution_kwargs_apply_resource_limits(tmp_path):
    backend = SubprocessBackend(SandboxConfig())

    kwargs = backend._build_exec_kwargs(tmp_path, 30, use_preexec=True)

    assert callable(kwargs["preexec_fn"])


class FakeProcess:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode = None
        self._finished = asyncio.Event()
        self.direct_signals: list[str] = []

    async def wait(self):
        await self._finished.wait()
        return self.returncode

    def finish(self, returncode: int):
        self.returncode = returncode
        self._finished.set()

    def terminate(self):
        self.direct_signals.append("terminate")
        self.finish(-signal.SIGTERM)

    def kill(self):
        self.direct_signals.append("kill")
        self.finish(-signal.SIGKILL)


class EmptyStream:
    async def read(self, _size: int):
        return b""


class FakeAsyncioProcess(FakeProcess):
    def __init__(self, pid: int = 4242):
        super().__init__(pid)
        self.stdout = EmptyStream()
        self.stderr = EmptyStream()


class FakePopenProcess:
    def __init__(self, pid: int = 4343):
        self.pid = pid
        self.returncode = None
        self.wait_calls = 0
        self.direct_signals: list[str] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def terminate(self):
        self.direct_signals.append("terminate")

    def kill(self):
        self.direct_signals.append("kill")


@pytest.mark.asyncio
async def test_terminate_process_group_reaps_after_sigterm(monkeypatch):
    proc = FakeProcess()
    signals = []

    def killpg(pid, sig):
        signals.append((pid, sig))
        proc.finish(-sig)

    monkeypatch.setattr(process_utils.os, "killpg", killpg)

    await process_utils.terminate_process_group(proc, grace_seconds=0.01)

    assert signals == [(proc.pid, signal.SIGTERM)]
    assert proc.returncode == -signal.SIGTERM
    assert proc.direct_signals == []


@pytest.mark.asyncio
async def test_terminate_process_group_escalates_to_sigkill(monkeypatch):
    proc = FakeProcess()
    signals = []

    def killpg(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            proc.finish(-sig)

    monkeypatch.setattr(process_utils.os, "killpg", killpg)

    await process_utils.terminate_process_group(proc, grace_seconds=0.01)

    assert signals == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert proc.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_terminate_process_group_kills_when_cleanup_is_cancelled(monkeypatch):
    proc = FakeProcess()
    term_sent = asyncio.Event()
    signals = []

    def killpg(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGTERM:
            term_sent.set()
        else:
            proc.finish(-sig)

    monkeypatch.setattr(process_utils.os, "killpg", killpg)
    cleanup = asyncio.create_task(
        process_utils.terminate_process_group(proc, grace_seconds=1)
    )
    await term_sent.wait()
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert signals == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert proc.returncode == -signal.SIGKILL


def test_terminate_popen_process_group_escalates_and_reaps(monkeypatch):
    proc = FakePopenProcess()
    signals = []

    def killpg(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            proc.returncode = -sig

    monkeypatch.setattr(process_utils.os, "killpg", killpg)

    process_utils.terminate_popen_process_group(proc, grace_seconds=0.01)

    assert signals == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert proc.returncode == -signal.SIGKILL
    assert proc.wait_calls == 2
    assert proc.direct_signals == []


@pytest.mark.asyncio
async def test_legacy_execute_timeout_starts_session_and_reaps_process(monkeypatch, tmp_path):
    proc = FakeAsyncioProcess()
    create_kwargs = {}
    terminated = []

    async def create_process(*_args, **kwargs):
        create_kwargs.update(kwargs)
        return proc

    async def terminate(candidate):
        terminated.append(candidate)
        candidate.finish(-signal.SIGTERM)

    monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(agent_tools, "terminate_process_group", terminate)

    result = await agent_tools._execute_code_legacy(
        tmp_path,
        {"language": "bash", "code": "echo hello", "timeout": 0.001},
    )

    assert "timed out" in result
    assert create_kwargs["start_new_session"] is True
    assert terminated == [proc]


@pytest.mark.asyncio
async def test_subprocess_backend_cancellation_reaps_process(monkeypatch, tmp_path):
    proc = FakeAsyncioProcess()
    created = asyncio.Event()
    create_kwargs = {}
    terminated = []

    async def create_process(*_args, **kwargs):
        create_kwargs.update(kwargs)
        created.set()
        return proc

    async def terminate(candidate):
        terminated.append(candidate)
        candidate.finish(-signal.SIGKILL)

    backend = SubprocessBackend(
        SandboxConfig(allow_unsafe_fallback_when_bwrap_missing=True)
    )
    async def ensure_workspace_venv(_path):
        return None

    monkeypatch.setattr(backend, "_ensure_workspace_venv", ensure_workspace_venv)
    monkeypatch.setattr(backend, "_build_bwrap_command", lambda *_args: None)
    monkeypatch.setattr(backend, "_build_host_command", lambda *_args: ["fake"])
    monkeypatch.setattr(
        subprocess_backend_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr(
        subprocess_backend_module,
        "terminate_process_group",
        terminate,
    )

    execution = asyncio.create_task(
        backend.execute(
            "print('hello')",
            "python",
            timeout=30,
            work_dir=str(tmp_path),
        )
    )
    await created.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert create_kwargs["start_new_session"] is True
    assert terminated == [proc]
