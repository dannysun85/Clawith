"""Helpers for reliably stopping subprocess trees."""

import asyncio
import os
import signal
import subprocess
from typing import Any

from loguru import logger


def _signal_process_group(proc: Any, sig: signal.Signals) -> None:
    """Signal a subprocess session, falling back to the direct child."""
    try:
        os.killpg(proc.pid, sig)
        return
    except ProcessLookupError:
        return
    except (AttributeError, OSError):
        # Windows and restricted runtimes may not expose process groups.  The
        # callers still use start_new_session on POSIX, so this is only a
        # compatibility fallback rather than the normal execution path.
        pass

    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


async def terminate_process_group(proc: Any, *, grace_seconds: float = 2.0) -> None:
    """Terminate and reap a process and every child in its process group.

    The process must have been started with ``start_new_session=True`` on
    POSIX.  A bounded graceful shutdown is followed by SIGKILL so timeout and
    cancellation paths cannot leave descendants running in the API container.
    """
    if proc is None:
        return

    if proc.returncode is not None:
        try:
            await proc.wait()
        except (ProcessLookupError, ChildProcessError):
            pass
        return

    wait_task = asyncio.create_task(proc.wait())
    _signal_process_group(proc, signal.SIGTERM)

    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        _signal_process_group(proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace_seconds)
        except asyncio.TimeoutError:
            logger.error(
                "[Process] Timed out reaping subprocess group pid={} after SIGKILL",
                proc.pid,
            )
    except asyncio.CancelledError:
        # Cleanup itself may be interrupted by request/task cancellation.  Kill
        # immediately and make one bounded reap attempt before propagating it.
        _signal_process_group(proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=grace_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        raise
    finally:
        if not wait_task.done():
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)


def terminate_popen_process_group(proc: Any, *, grace_seconds: float = 2.0) -> None:
    """Synchronous counterpart for ``subprocess.Popen`` process groups."""
    if proc is None or proc.poll() is not None:
        return

    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGKILL)

    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.error(
            "[Process] Timed out reaping Popen process group pid={} after SIGKILL",
            proc.pid,
        )


async def settle_tasks(
    tasks: list[asyncio.Task[Any]],
    *,
    timeout: float = 2.0,
) -> list[Any]:
    """Bound waiting for stream-reader tasks and always consume exceptions."""
    if not tasks:
        return []

    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    for task in pending:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if task in done and isinstance(result, BaseException):
            logger.warning(
                "[Process] Stream reader failed error_type={}",
                type(result).__name__,
            )
    return results
