from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parents[2]
HELPER = ROOT / "scripts/consume_break_glass_approval.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("consume_break_glass_approval", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _consume(module, tmp_path: Path, *, fault_hook=None):
    artifact = tmp_path / "approval.txt"
    artifact.write_text(
        "approval_id=release-approval\napproval_nonce=unique-nonce-1234\n",
        encoding="utf-8",
    )
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    nonce_sha256 = hashlib.sha256(b"unique-nonce-1234").hexdigest()
    path = module.consume_approval(
        ledger_dir=tmp_path / "ledger",
        artifact=artifact,
        artifact_sha256=artifact_sha256,
        nonce_sha256=nonce_sha256,
        release_id="20260715-commit-astra-saas",
        release_version="1.10.12",
        release_commit="a" * 40,
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        fault_hook=fault_hook,
    )
    return path, artifact.read_bytes(), artifact_sha256, nonce_sha256


def test_break_glass_nonce_publishes_complete_evidence_once(tmp_path):
    module = _load_helper()

    path, artifact, artifact_sha256, nonce_sha256 = _consume(module, tmp_path)

    assert path.name == f"{nonce_sha256}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["approval_artifact_sha256"] == artifact_sha256
    assert record["approval_nonce_sha256"] == nonce_sha256
    assert record["release_commit"] == "a" * 40
    assert record["release_version"] == "1.10.12"
    assert record["consumed_at_utc"] == "2026-07-15T12:00:00Z"
    assert module.base64.b64decode(record["approval_artifact_base64"]) == artifact

    with pytest.raises(module.ApprovalReplayError, match="already been used"):
        _consume(module, tmp_path)


def test_break_glass_crash_before_publish_does_not_consume_nonce(tmp_path):
    module = _load_helper()

    def fail_before_publish(stage: str):
        if stage == "after_temp_fsync":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _consume(module, tmp_path, fault_hook=fail_before_publish)

    assert list((tmp_path / "ledger").iterdir()) == []
    path, *_ = _consume(module, tmp_path)
    assert path.is_file()


def test_break_glass_crash_after_publish_keeps_complete_consumed_record(tmp_path):
    module = _load_helper()

    def fail_after_publish(stage: str):
        if stage == "after_publish":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _consume(module, tmp_path, fault_hook=fail_after_publish)

    records = list((tmp_path / "ledger").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["schema_version"] == 1
    with pytest.raises(module.ApprovalReplayError, match="already been used"):
        _consume(module, tmp_path)
