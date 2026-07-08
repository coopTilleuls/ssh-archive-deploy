from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path
from typing import override

import pytest

from ssh_archive_deploy.archive import (
    MANIFEST_NAME,
    build_archive,
    first_duplicate_name,
    read_manifest_from_archive,
    validate_archive,
)
from ssh_archive_deploy.config import parse_config
from ssh_archive_deploy.errors import DeployError


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=path, check=True, stdout=subprocess.PIPE)


def test_build_uses_git_tracked_files_and_excludes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / "theme/secret.php").write_text("secret\n", encoding="utf-8")
    (tmp_path / "theme/ignored.log").write_text("ignored\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    config = parse_config(
        {
            "version": 1,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [
                {
                    "name": "theme",
                    "source": "theme",
                    "target": "wp-content/themes/demo",
                    "exclude": ["secret.php"],
                },
            ],
        },
    )

    archive = tmp_path / "dist/site.tar.gz"
    manifest = build_archive(config, archive, "release-1")

    assert archive.exists()
    assert [item.path for item in manifest.files] == ["wp-content/themes/demo/index.php"]
    validate_archive(archive, config)
    archived_manifest = read_manifest_from_archive(archive)
    assert json.dumps(archived_manifest)


def test_build_rejects_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / "theme/link.php").symlink_to("index.php")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    config = parse_config(
        {
            "version": 1,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [{"name": "theme", "source": "theme", "target": "theme"}],
        },
    )

    with pytest.raises(DeployError, match="symlink"):
        build_archive(config, tmp_path / "site.tar.gz", "release-1")


def test_validate_archive_rejects_checksum_mismatch(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "theme").mkdir()
    (package / "theme/index.php").write_text("actual\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "tool_version": "test",
        "project": "demo",
        "release_id": "release-1",
        "commit_sha": "abc",
        "commit_ref": "main",
        "build_time": "2026-01-01T00:00:00Z",
        "scopes": [
            {
                "name": "theme",
                "source": "theme",
                "target": "theme",
                "files": ["theme/index.php"],
            },
        ],
        "files": [
            {
                "path": "theme/index.php",
                "scope": "theme",
                "size": 7,
                "sha256": "0" * 64,
            },
        ],
    }
    (package / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "site.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(package / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
        tar.add(package / "theme/index.php", arcname="theme/index.php", recursive=False)

    with pytest.raises(DeployError, match="checksum"):
        validate_archive(archive)


def test_validate_archive_rejects_non_canonical_member_path(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "index.php").write_text("actual\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "tool_version": "test",
        "project": "demo",
        "release_id": "release-1",
        "commit_sha": "abc",
        "commit_ref": "main",
        "build_time": "2026-01-01T00:00:00Z",
        "scopes": [
            {
                "name": "root",
                "source": ".",
                "target": ".",
                "files": ["./index.php"],
            },
        ],
        "files": [
            {
                "path": "./index.php",
                "scope": "root",
                "size": 7,
                "sha256": "0" * 64,
            },
        ],
    }
    (package / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "site.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(package / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
        tar.add(package / "index.php", arcname="./index.php", recursive=False)

    with pytest.raises(DeployError, match="Unsafe path"):
        validate_archive(archive)


def test_validate_archive_rejects_control_character_member_path(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "index.php").write_text("actual\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "tool_version": "test",
        "project": "demo",
        "release_id": "release-1",
        "commit_sha": "abc",
        "commit_ref": "main",
        "build_time": "2026-01-01T00:00:00Z",
        "scopes": [
            {
                "name": "root",
                "source": ".",
                "target": ".",
                "files": ["dir/file\nother.php"],
            },
        ],
        "files": [
            {
                "path": "dir/file\nother.php",
                "scope": "root",
                "size": 7,
                "sha256": "0" * 64,
            },
        ],
    }
    (package / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "site.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(package / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
        tar.add(package / "index.php", arcname="dir/file\nother.php", recursive=False)

    with pytest.raises(DeployError, match="control characters"):
        validate_archive(archive)


def test_validate_archive_rejects_duplicate_member_path(tmp_path: Path) -> None:
    archive = tmp_path / "site.tar.gz"
    first = tmp_path / "first.php"
    second = tmp_path / "second.php"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(first, arcname="index.php", recursive=False)
        tar.add(second, arcname="index.php", recursive=False)

    with pytest.raises(DeployError, match="duplicate path: index.php"):
        validate_archive(archive)


def test_duplicate_member_detection_does_not_use_quadratic_count() -> None:
    class CountGuardList(list[str]):
        @override
        def count(self, value: object) -> int:
            raise AssertionError("duplicate detection must not use list.count")

    names = CountGuardList(f"file-{index}.txt" for index in range(10_000))
    names.append("file-9999.txt")

    assert first_duplicate_name(names) == "file-9999.txt"
