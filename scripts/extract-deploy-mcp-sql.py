#!/usr/bin/env python3
"""Print the exact MCP deployment SQL block for disposable-Postgres smoke tests."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts/deploy-astra-production.sh"
BLOCKS = {
    "quarantine": "SQL_MCP_QUARANTINE",
    "restore": "SQL_MCP_RESTORE",
}


def extract_block(name: str) -> str:
    marker = BLOCKS[name]
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    opener = f"<<'{marker}'\n"
    start = source.index(opener) + len(opener)
    end = source.index(f"\n{marker}", start)
    return source[start:end] + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("block", choices=tuple(BLOCKS))
    args = parser.parse_args()
    print(extract_block(args.block), end="")


if __name__ == "__main__":
    main()
