#!/usr/bin/env python3
"""Atomically consume one production break-glass approval nonce.

The nonce is considered consumed only when a complete, fsynced JSON evidence
record has been linked into the root-owned ledger.  A crash before publication
leaves the nonce reusable; a crash after publication leaves a complete record.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat


HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_RELEASE_ID = re.compile(r"[A-Za-z0-9._-]{1,255}")
FULL_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
MAX_APPROVAL_BYTES = 16_384


class ApprovalReplayError(RuntimeError):
    """Raised when the approval nonce already has a durable ledger record."""


class ApprovalEvidenceError(RuntimeError):
    """Raised when approval evidence cannot be validated or persisted safely."""


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ApprovalEvidenceError("approval artifact is missing or unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ApprovalEvidenceError("approval artifact must be a regular file")
        if metadata.st_size > MAX_APPROVAL_BYTES:
            raise ApprovalEvidenceError("approval artifact is too large")
        chunks: list[bytes] = []
        remaining = MAX_APPROVAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_APPROVAL_BYTES:
            raise ApprovalEvidenceError("approval artifact is too large")
        return payload
    finally:
        os.close(descriptor)


def _open_ledger(path: Path) -> int:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ApprovalEvidenceError("break-glass ledger is missing or unsafe") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ApprovalEvidenceError("break-glass ledger must be a directory")
    if metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise ApprovalEvidenceError("break-glass ledger has an unexpected owner")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise ApprovalEvidenceError("break-glass ledger permissions must be 0700")
    return descriptor


def consume_approval(
    *,
    ledger_dir: Path,
    artifact: Path,
    artifact_sha256: str,
    nonce_sha256: str,
    release_id: str,
    release_version: str,
    release_commit: str,
    now: datetime | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> Path:
    """Publish a complete approval record exactly once for ``nonce_sha256``."""

    if HEX_SHA256.fullmatch(artifact_sha256) is None:
        raise ApprovalEvidenceError("approval artifact digest is invalid")
    if HEX_SHA256.fullmatch(nonce_sha256) is None:
        raise ApprovalEvidenceError("approval nonce digest is invalid")
    if SAFE_RELEASE_ID.fullmatch(release_id) is None:
        raise ApprovalEvidenceError("release id is invalid")
    if not release_version or "\n" in release_version or "\r" in release_version:
        raise ApprovalEvidenceError("release version is invalid")
    if FULL_GIT_COMMIT.fullmatch(release_commit) is None:
        raise ApprovalEvidenceError("release commit is invalid")

    artifact_bytes = _read_regular_file(artifact)
    actual_digest = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_digest != artifact_sha256:
        raise ApprovalEvidenceError("approval artifact digest mismatch")

    consumed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record = {
        "approval_artifact_base64": base64.b64encode(artifact_bytes).decode("ascii"),
        "approval_artifact_sha256": artifact_sha256,
        "approval_nonce_sha256": nonce_sha256,
        "consumed_at_utc": consumed_at.isoformat().replace("+00:00", "Z"),
        "release_commit": release_commit,
        "release_id": release_id,
        "release_version": release_version,
        "schema_version": 1,
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()

    ledger_fd = _open_ledger(ledger_dir)
    final_name = f"{nonce_sha256}.json"
    temporary_name = f".{nonce_sha256}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            os.stat(final_name, dir_fd=ledger_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ApprovalReplayError("break-glass approval nonce has already been used")

        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=ledger_fd)
        temporary_created = True
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise ApprovalEvidenceError("could not write break-glass evidence")
                view = view[written:]
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        if fault_hook is not None:
            fault_hook("after_temp_fsync")

        try:
            os.link(
                temporary_name,
                final_name,
                src_dir_fd=ledger_fd,
                dst_dir_fd=ledger_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ApprovalReplayError(
                "break-glass approval nonce has already been used"
            ) from exc
        os.fsync(ledger_fd)
        if fault_hook is not None:
            fault_hook("after_publish")
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=ledger_fd)
                os.fsync(ledger_fd)
            except FileNotFoundError:
                pass
        os.close(ledger_fd)

    return ledger_dir / final_name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--nonce-sha256", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--release-commit", required=True)
    args = parser.parse_args()
    try:
        record_path = consume_approval(
            ledger_dir=args.ledger_dir,
            artifact=args.artifact,
            artifact_sha256=args.artifact_sha256,
            nonce_sha256=args.nonce_sha256,
            release_id=args.release_id,
            release_version=args.release_version,
            release_commit=args.release_commit,
        )
    except (ApprovalReplayError, ApprovalEvidenceError) as exc:
        parser.exit(1, f"{exc}\n")
    print(record_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
