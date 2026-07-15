import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[2]
GATE = ROOT / "scripts/ruff_diff_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("ruff_diff_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_added_line_map_marks_a_moved_violation_as_new(tmp_path):
    gate = _load_gate()
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    source = tmp_path / "example.py"
    source.write_text(
        "problem = missing_name\nfirst = 1\nsecond = 2\nthird = 3\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "example.py")
    _git(tmp_path, "commit", "-m", "baseline")
    base = _git(tmp_path, "rev-parse", "HEAD")

    source.write_text(
        "fixed = 0\nfirst = 1\nsecond = 2\nthird = 3\n"
        "problem = missing_name\n",
        encoding="utf-8",
    )

    assert gate._added_python_lines(
        tmp_path,
        base,
        "WORKTREE",
        ["example.py"],
    ) == {"example.py": {1, 5}}
