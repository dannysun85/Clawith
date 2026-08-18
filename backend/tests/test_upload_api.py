"""Chat upload text extraction must treat filenames as data, never as code.

Regression coverage for the upstream fix a09a2323 ("Fix chat upload filename
code injection").  The local implementation never spawned a subprocess with
the upload path interpolated into source; these tests pin that property so
the vulnerable pattern cannot be reintroduced.
"""

import subprocess
from pathlib import Path

import pytest

from app.api import upload


@pytest.mark.parametrize("extension", [".pdf", ".docx", ".xlsx"])
def test_office_extraction_never_invokes_subprocess_for_adversarial_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extension: str,
) -> None:
    file_path = tmp_path / f"report');__import__('os').system('id');#{extension}"
    file_path.write_bytes(b"not-a-real-office-document")

    def forbidden_subprocess_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("extract_text must not shell out for office files")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess_run)

    result = upload.extract_text(file_path, extension)

    # Invalid bytes must degrade to a bracketed failure marker, not an exploit.
    assert result.startswith("[")
    assert result.endswith("]")
    assert "uid=" not in result  # no `os.system('id')` output leaked


def test_text_extraction_reads_adversarial_filename_as_plain_text(tmp_path: Path) -> None:
    file_path = tmp_path / "notes');__import__('os').system('id');#.txt"
    file_path.write_text("plain content", encoding="utf-8")

    assert upload.extract_text(file_path, ".txt") == "plain content"
