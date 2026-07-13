from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssh_archive_deploy.cli import main


def test_summarize_report_command_outputs_github_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "demo",
                "release_id": "release-1",
                "strategy": {"apply": "overlay", "delete_unknown": False},
                "summary": {
                    "changed_files": 0,
                    "artifact_only_files": 0,
                    "remote_only_files": 0,
                    "text_changed_files": 0,
                    "binary_changed_files": 0,
                    "insertions": 0,
                    "deletions": 0,
                },
                "scopes": [
                    {
                        "name": "theme",
                        "target": "wp-content/themes/demo",
                        "summary": {
                            "changed_files": 0,
                            "artifact_only_files": 0,
                            "remote_only_files": 0,
                            "text_changed_files": 0,
                            "binary_changed_files": 0,
                            "insertions": 0,
                            "deletions": 0,
                        },
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    result = main(["summarize-report", "--report", str(report), "--format", "github"])

    assert result == 0
    assert capsys.readouterr().out.startswith("## Deployment Report\n")


def test_summarize_doctor_command_outputs_github_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "doctor.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "demo",
                "target": {"name": "production"},
                "compatibility": "undetermined",
                "tar": {
                    "implementation": "gnu",
                    "version": "1.35",
                    "compatibility": "undetermined",
                },
            }
        ),
        encoding="utf-8",
    )

    result = main(["summarize-doctor", "--report", str(report), "--format", "github"])

    assert result == 0
    assert capsys.readouterr().out.startswith("## Remote Capability Doctor\n")
