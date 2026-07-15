#!/usr/bin/env python3
"""Fail a release when it introduces Ruff violations above the Git baseline."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _changed_python_files(root: Path, base: str, target: str) -> list[str]:
    if target == "WORKTREE":
        diff = _run(
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            base,
            "--",
            "*.py",
            cwd=root,
        )
        untracked = _run(
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
            cwd=root,
        )
        processes = (diff, untracked)
    else:
        processes = (
            _run(
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                f"{base}..{target}",
                "--",
                "*.py",
                cwd=root,
            ),
        )
    for process in processes:
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or "git diff failed")
    return sorted(
        {
            line.strip()
            for process in processes
            for line in process.stdout.splitlines()
            if line.strip() and (root / line.strip()).is_file()
        }
    )


def _added_python_lines(
    root: Path,
    base: str,
    target: str,
    relative_files: list[str],
) -> dict[str, set[int]]:
    """Return added target line numbers so moved violations cannot hide in baseline counts."""

    added: dict[str, set[int]] = {}
    hunk_header = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for relative in relative_files:
        baseline = _run("git", "show", f"{base}:{relative}", cwd=root)
        if baseline.returncode != 0:
            line_count = len((root / relative).read_text(encoding="utf-8").splitlines())
            added[relative] = set(range(1, line_count + 1))
            continue
        comparison = base if target == "WORKTREE" else f"{base}..{target}"
        diff = _run(
            "git",
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            comparison,
            "--",
            relative,
            cwd=root,
        )
        if diff.returncode != 0:
            raise RuntimeError(diff.stderr.strip() or "git diff failed")
        lines: set[int] = set()
        for output_line in diff.stdout.splitlines():
            match = hunk_header.match(output_line)
            if match is None:
                continue
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            lines.update(range(start, start + count))
        added[relative] = lines
    return added


def _ruff_violations(
    *,
    cwd: Path,
    config: Path,
    relative_files: list[str],
) -> list[dict]:
    if not relative_files:
        return []
    ruff = shutil.which("ruff")
    if not ruff:
        raise RuntimeError("ruff is not available in PATH")
    process = _run(
        ruff,
        "check",
        "--config",
        str(config),
        "--output-format=json",
        *relative_files,
        cwd=cwd,
    )
    if process.returncode not in {0, 1}:
        raise RuntimeError(process.stderr.strip() or "ruff invocation failed")
    try:
        parsed = json.loads(process.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ruff returned invalid JSON") from exc
    if not isinstance(parsed, list):
        raise RuntimeError("ruff JSON output is not a list")
    return parsed


def _fingerprint(issue: dict, *, cwd: Path, path_map: dict[Path, str]) -> tuple[str, str, str, str]:
    filename = Path(str(issue["filename"]))
    resolved = (filename if filename.is_absolute() else cwd / filename).resolve()
    relative = path_map.get(resolved)
    if relative is None:
        raise RuntimeError(f"ruff reported an unexpected file: {filename}")
    row = int(issue["location"]["row"])
    lines = resolved.read_text(encoding="utf-8").splitlines()
    source = lines[row - 1].strip() if 0 < row <= len(lines) else ""
    return (
        relative,
        str(issue.get("code") or "unknown"),
        str(issue.get("message") or ""),
        source,
    )


def _copy_baseline(root: Path, baseline_root: Path, base: str, relative_files: list[str]) -> list[str]:
    copied: list[str] = []
    for relative in relative_files:
        show = _run("git", "show", f"{base}:{relative}", cwd=root)
        if show.returncode != 0:
            continue
        destination = baseline_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(show.stdout, encoding="utf-8")
        copied.append(relative)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="HEAD^")
    parser.add_argument("--target", default="HEAD")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    config = root / "backend" / "pyproject.toml"
    relative_files = _changed_python_files(root, args.base, args.target)
    if not relative_files:
        print("Ruff diff gate: no changed Python files")
        return 0

    current_map = {(root / relative).resolve(): relative for relative in relative_files}
    current_issues = _ruff_violations(
        cwd=root,
        config=config,
        relative_files=relative_files,
    )
    current_fingerprints = [
        _fingerprint(issue, cwd=root, path_map=current_map)
        for issue in current_issues
    ]
    added_lines = _added_python_lines(root, args.base, args.target, relative_files)

    with tempfile.TemporaryDirectory(prefix="astra-ruff-baseline-") as temporary:
        baseline_root = Path(temporary)
        baseline_files = _copy_baseline(
            root,
            baseline_root,
            args.base,
            relative_files,
        )
        baseline_map = {
            (baseline_root / relative).resolve(): relative
            for relative in baseline_files
        }
        baseline_issues = _ruff_violations(
            cwd=baseline_root,
            config=config,
            relative_files=baseline_files,
        )
        baseline_fingerprints = Counter(
            _fingerprint(issue, cwd=baseline_root, path_map=baseline_map)
            for issue in baseline_issues
        )

    new_issue_indexes: list[int] = []
    for index, (issue, fingerprint) in enumerate(
        zip(current_issues, current_fingerprints, strict=True)
    ):
        relative = fingerprint[0]
        row = int(issue["location"]["row"])
        if row in added_lines.get(relative, set()):
            new_issue_indexes.append(index)
            continue
        if baseline_fingerprints[fingerprint] > 0:
            baseline_fingerprints[fingerprint] -= 1
            continue
        new_issue_indexes.append(index)

    if not new_issue_indexes:
        print(
            "Ruff diff gate passed: no new violations "
            f"across {len(relative_files)} changed Python file(s)"
        )
        return 0

    print("Ruff diff gate failed: new violations introduced:")
    for index in new_issue_indexes:
        issue = current_issues[index]
        fingerprint = current_fingerprints[index]
        relative, code, message, _source = fingerprint
        location = issue["location"]
        print(
            f"{relative}:{location['row']}:{location['column']}: "
            f"{code} {message}"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
