from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "scripts/validate_release_notes.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release.yml"


def run_validator(path: Path, tag: str = "v0.3.1") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--tag", tag, "--file", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_release_notes_accept_substantive_versioned_content(tmp_path: Path) -> None:
    notes = tmp_path / "v0.3.1.md"
    notes.write_text(
        "## Security\n\n"
        "- Pin every external GitHub Action to an immutable commit and enforce "
        "the policy in CI.\n",
        encoding="utf-8",
    )

    result = run_validator(notes)

    assert result.returncode == 0, result.stderr


def test_release_notes_reject_one_line_placeholder(tmp_path: Path) -> None:
    notes = tmp_path / "v0.3.1.md"
    notes.write_text("Release v0.3.1\n", encoding="utf-8")

    result = run_validator(notes)

    assert result.returncode == 1
    assert "one-line release placeholder" in result.stderr


def test_release_notes_reject_unfinished_content(tmp_path: Path) -> None:
    notes = tmp_path / "v0.3.1.md"
    notes.write_text(
        "## Changes\n\n- TODO: describe the changes included in this future release.\n",
        encoding="utf-8",
    )

    result = run_validator(notes)

    assert result.returncode == 1
    assert "must not contain TODO" in result.stderr


def test_release_workflow_uses_prevalidated_notes_file() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    validation = "uv run python scripts/validate_release_notes.py"
    creation = 'gh release create "${TAG}"'
    assert validation in workflow
    assert workflow.index(validation) < workflow.index(creation)
    assert '--notes-file "${RELEASE_NOTES}"' in workflow
    assert '--notes "Release ${TAG}"' not in workflow
