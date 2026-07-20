from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ssh_archive_deploy.archive import MANIFEST_NAME, build_archive
from ssh_archive_deploy.config import parse_config
from ssh_archive_deploy.planner import ArtifactSnapshot, RemoteSnapshot, compute_plan
from ssh_archive_deploy.report import remote_tar_script, write_report
from ssh_archive_deploy.summary import render_github_summary


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=path, check=True, stdout=subprocess.PIPE)


def test_report_is_grouped_by_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "theme").mkdir()
    (repo / "theme/index.php").write_text("new\n", encoding="utf-8")
    (repo / "theme/added.php").write_text("added\n", encoding="utf-8")
    commit_all(repo)
    monkeypatch.chdir(repo)

    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [{"name": "theme", "source": "theme", "target": "theme"}],
        },
    )
    archive = repo / "site.tar.gz"
    build_archive(config, archive, "release-1")

    current_root = tmp_path / "current"
    new_root = tmp_path / "new"
    current_root.mkdir()
    new_root.mkdir()
    (current_root / "theme").mkdir()
    (new_root / "theme").mkdir()
    (current_root / "theme/index.php").write_text("old\n", encoding="utf-8")
    (current_root / "theme/remote-only.php").write_text("drift\n", encoding="utf-8")
    (new_root / "theme/index.php").write_text("new\n", encoding="utf-8")
    (new_root / "theme/added.php").write_text("added\n", encoding="utf-8")

    summary = write_report(config, new_root, current_root, tmp_path / "report", "release-1")

    assert summary.scopes[0].changed_files == 1
    assert summary.scopes[0].artifact_only_files == 1
    assert summary.scopes[0].remote_only_files == 1
    report = json.loads((tmp_path / "report/report.json").read_text(encoding="utf-8"))
    assert report == {
        "schema_version": 1,
        "project": "demo",
        "release_id": "release-1",
        "strategy": {
            "apply": "overlay",
            "delete_unknown": False,
        },
        "summary": {
            "changed_files": 1,
            "artifact_only_files": 1,
            "remote_only_files": 1,
            "text_changed_files": 1,
            "binary_changed_files": 0,
            "insertions": 1,
            "deletions": 1,
        },
        "scopes": [
            {
                "name": "theme",
                "target": "theme",
                "summary": {
                    "changed_files": 1,
                    "artifact_only_files": 1,
                    "remote_only_files": 1,
                    "text_changed_files": 1,
                    "binary_changed_files": 0,
                    "insertions": 1,
                    "deletions": 1,
                },
                "changed_files": ["theme/index.php"],
                "changed_text_files": ["theme/index.php"],
                "changed_binary_files": [],
                "artifact_only_files": ["theme/added.php"],
                "remote_only_files": ["theme/remote-only.php"],
                "text_line_changes": [
                    {
                        "path": "theme/index.php",
                        "insertions": 1,
                        "deletions": 1,
                    },
                ],
            },
        ],
    }


def test_remote_report_stream_does_not_require_mktemp() -> None:
    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [
                {"name": "theme", "source": "theme", "target": "theme"},
                {"name": "optional", "source": "optional", "target": "optional"},
            ],
        },
    )

    script = remote_tar_script(config).decode("utf-8")

    assert "mktemp" not in script
    assert "set --" in script
    assert '[ ! -e optional ] || set -- "$@" optional' in script
    assert '[ ! -e theme ] || set -- "$@" theme' in script
    assert 'if [ "$#" -eq 0 ]; then' in script
    assert "tar -czf - --files-from /dev/null" in script
    assert 'tar -czf - -- "$@"' in script


def test_planner_classifies_overlay_operations(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    remote_root = tmp_path / "remote"
    artifact_root.mkdir()
    remote_root.mkdir()
    (artifact_root / "theme").mkdir()
    (remote_root / "theme").mkdir()
    (artifact_root / "theme/index.php").write_text("new\n", encoding="utf-8")
    (artifact_root / "theme/added.php").write_text("added\n", encoding="utf-8")
    (remote_root / "theme/index.php").write_text("old\n", encoding="utf-8")
    (remote_root / "theme/manual.php").write_text("manual\n", encoding="utf-8")

    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "strategy": {"apply": "overlay", "delete_unknown": False},
            "scope": [{"name": "theme", "source": "theme", "target": "theme"}],
        },
    )

    plan = compute_plan(
        config,
        ArtifactSnapshot(artifact_root),
        RemoteSnapshot(remote_root),
        "release-1",
    )

    assert plan.project == "demo"
    assert plan.release_id == "release-1"
    assert plan.scopes[0].changed_files == ["index.php"]
    assert plan.scopes[0].artifact_only_files == ["added.php"]
    assert plan.scopes[0].remote_only_files == ["manual.php"]


def test_planner_ignores_root_manifest_for_dot_target(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    remote_root = tmp_path / "remote"
    artifact_root.mkdir()
    remote_root.mkdir()
    (artifact_root / MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
    (artifact_root / "index.html").write_text("new\n", encoding="utf-8")

    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "strategy": {"apply": "overlay", "delete_unknown": False},
            "scope": [{"name": "root", "source": ".", "target": "."}],
        },
    )

    plan = compute_plan(
        config,
        ArtifactSnapshot(artifact_root),
        RemoteSnapshot(remote_root),
        "release-1",
    )

    assert plan.scopes[0].artifact_only_files == ["index.html"]


def test_github_summary_renders_clean_report(tmp_path: Path) -> None:
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
        )
        + "\n",
        encoding="utf-8",
    )

    summary = render_github_summary(report)

    assert "## Deployment Report" in summary
    assert "Project: `demo`" in summary
    assert "Release id: `release-1`" in summary
    assert "| theme | wp-content/themes/demo | 0 | 0 | 0 | 0 | 0 | 0 | 0 |" in summary
    assert "Result: no drift detected." in summary
    assert "index.php" not in summary


def test_github_summary_renders_drift_without_file_lists(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "demo`project",
                "release_id": "release-2",
                "strategy": {"apply": "overlay", "delete_unknown": False},
                "summary": {
                    "changed_files": 2,
                    "artifact_only_files": 1,
                    "remote_only_files": 3,
                    "text_changed_files": 1,
                    "binary_changed_files": 1,
                    "insertions": 4,
                    "deletions": 5,
                },
                "scopes": [
                    {
                        "name": "theme|main",
                        "target": "wp-content/themes/demo",
                        "summary": {
                            "changed_files": 2,
                            "artifact_only_files": 1,
                            "remote_only_files": 3,
                            "text_changed_files": 1,
                            "binary_changed_files": 1,
                            "insertions": 4,
                            "deletions": 5,
                        },
                        "changed_files": ["wp-config.php"],
                        "remote_only_files": ["secret.txt"],
                    },
                ],
            },
        )
        + "\n",
        encoding="utf-8",
    )

    summary = render_github_summary(report)

    assert "Project: ``demo`project``" in summary
    assert "| theme\\|main | wp-content/themes/demo | 2 | 1 | 3 | 1 | 1 | 4 | 5 |" in summary
    assert (
        "Result: drift detected. Review the uploaded report artifacts before deploying." in summary
    )
    assert "Note: the configured overlay strategy keeps remote-only files in place." in summary
    assert "Detailed file lists are available in the uploaded report artifacts." in summary
    assert "wp-config.php" not in summary
    assert "secret.txt" not in summary
