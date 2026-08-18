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

# v2 pipeline coverage: the v1 contract files above must stay byte-compatible
# in behavior while these guard the new manifest/brief/compiler/QA seams.
V2_PIPELINE_TESTS = (
    "tests/test_creative_briefs.py",
    "tests/test_prompt_compiler.py",
    "tests/test_creative_brief_migration.py",
)


def main() -> int:
    return pytest.main(["-q", *PROTECTED_V1_TESTS, *V2_PIPELINE_TESTS])


if __name__ == "__main__":
    raise SystemExit(main())
