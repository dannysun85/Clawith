"""Run a real local execute_code smoke check inside a release container."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local.subprocess_backend import SubprocessBackend


SMOKE_MARKER = "clawith-sandbox-ok"


async def _run() -> int:
    agent_data_dir = Path(os.getenv("AGENT_DATA_DIR", "/data/agents")).resolve()
    work_dir = agent_data_dir / ".sandbox-smoke"
    shutil.rmtree(work_dir, ignore_errors=True)

    try:
        backend = SubprocessBackend(
            SandboxConfig(
                allow_network=False,
                allow_unsafe_fallback_when_bwrap_missing=False,
            )
        )
        result = await backend.execute(
            f"print({SMOKE_MARKER!r})",
            "python",
            timeout=20,
            work_dir=str(work_dir),
            agent_id=".sandbox-smoke",
        )
        if result.success and result.stdout.strip() == SMOKE_MARKER:
            print(SMOKE_MARKER)
            return 0

        print(
            "sandbox smoke failed: "
            f"exit_code={result.exit_code} error={result.error!r} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
