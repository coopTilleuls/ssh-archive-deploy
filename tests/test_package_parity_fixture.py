from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

import pytest

from ssh_archive_deploy.archive import (
    build_archive,
    is_git_lfs_pointer,
    read_manifest_from_archive,
    validate_archive,
)
from ssh_archive_deploy.config import load_config

FIXTURE_ROOT = Path(__file__).parent / "fixtures/package-parity"
VOLATILE_MANIFEST_FIELDS = {"tool_version", "commit_sha", "commit_ref", "build_time"}


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
    )


def stable_manifest_fields(manifest: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in manifest.items() if key not in VOLATILE_MANIFEST_FIELDS}


def test_integrated_package_fixture_matches_expected_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(FIXTURE_ROOT / "project", tmp_path, dirs_exist_ok=True)
    shutil.copyfile(FIXTURE_ROOT / "deploy.yml", tmp_path / "deploy.yml")
    init_repo(tmp_path)
    commit_all(tmp_path)
    shutil.copytree(FIXTURE_ROOT / "generated", tmp_path, dirs_exist_ok=True)
    monkeypatch.chdir(tmp_path)

    resolved_lfs_asset = tmp_path / "themes/boreal/assets/hero.psd"
    assert not is_git_lfs_pointer(resolved_lfs_asset.read_bytes())

    config = load_config(str(tmp_path / "deploy.yml"))
    archive = tmp_path / "dist/package-parity.tar.gz"
    manifest = build_archive(config, archive, "release-parity")
    expected = json.loads((FIXTURE_ROOT / "expected-manifest.json").read_text(encoding="utf-8"))

    returned_manifest = stable_manifest_fields(asdict(manifest))
    embedded_manifest = stable_manifest_fields(read_manifest_from_archive(archive))
    sidecar_manifest = stable_manifest_fields(
        json.loads(
            archive.with_suffix(archive.suffix + ".manifest.json").read_text(encoding="utf-8")
        )
    )

    assert returned_manifest == expected
    assert embedded_manifest == expected
    assert sidecar_manifest == expected
    validate_archive(archive, config)
