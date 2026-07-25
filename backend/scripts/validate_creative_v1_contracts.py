#!/usr/bin/env python3
"""Run the provider-free v1 creative compatibility gate for P0."""

from __future__ import annotations

import pytest


PROTECTED_V1_TESTS = (
    "tests/test_deliverable_workflows.py",
    "tests/test_brand_safe_media_contract.py",
    "tests/test_media_capabilities.py",
    "tests/test_websocket_message_queue.py",
    "tests/test_agent_runtime_chat_stream.py",
    "tests/test_agent_runtime_tool_outcome_contract.py",
)


def main() -> int:
    return pytest.main(["-q", *PROTECTED_V1_TESTS])


if __name__ == "__main__":
    raise SystemExit(main())
