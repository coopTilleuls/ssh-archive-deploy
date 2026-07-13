from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssh_archive_deploy.archive import sha256_file
from ssh_archive_deploy.config import parse_config
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.execution_plan import (
    before_manifest,
    build_execution_plan,
    execution_plan_from_json,
    execution_plan_to_json,
)


def test_execution_plan_classifies_overlay_operations(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    remote = tmp_path / "remote"
    artifact.mkdir()
    remote.mkdir()
    (artifact / "theme").mkdir()
    (remote / "theme").mkdir()
    (artifact / "theme/new.php").write_text("new\n", encoding="utf-8")
    (artifact / "theme/changed.php").write_text("new\n", encoding="utf-8")
    (artifact / "theme/same.php").write_text("same\n", encoding="utf-8")
    (remote / "theme/changed.php").write_text("old\n", encoding="utf-8")
    (remote / "theme/same.php").write_text("same\n", encoding="utf-8")

    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/srv/deploy"},
            "scope": [{"name": "theme", "source": "theme", "target": "theme"}],
        },
    )
    manifest: dict[str, object] = {
        "release_id": "release-1",
        "files": [
            {
                "path": "theme/new.php",
                "scope": "theme",
                "size": 4,
                "sha256": "2fea4d8bbabb7617ca6f0c0529764a31b5c7fd6bb5015004f7a38bc8c6a76ba1",
            },
            {
                "path": "theme/changed.php",
                "scope": "theme",
                "size": 4,
                "sha256": "2fea4d8bbabb7617ca6f0c0529764a31b5c7fd6bb5015004f7a38bc8c6a76ba1",
            },
            {
                "path": "theme/same.php",
                "scope": "theme",
                "size": 5,
                "sha256": sha256(artifact / "theme/same.php"),
            },
        ],
    }

    plan = build_execution_plan(config, manifest, artifact, remote, "artifact-sha")

    assert [(operation.op, operation.path) for operation in plan.operations] == [
        ("replace", "theme/changed.php"),
        ("create", "theme/new.php"),
        ("skip", "theme/same.php"),
    ]
    assert plan.created_directories == []
    assert before_manifest(plan).files[0].path == "theme/changed.php"


def test_execution_plan_rejects_unknown_operation() -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "project": "demo",
            "release_id": "release-1",
            "artifact_sha256": "abc",
            "strategy": {"apply": "overlay", "delete_unknown": False},
            "operations": [{"op": "delete", "path": "index.php"}],
            "created_directories": [],
        },
    )

    with pytest.raises(DeployError, match="Unsupported apply operation"):
        execution_plan_from_json(raw)


@pytest.mark.parametrize(
    "path",
    [
        "/index.php",
        "../index.php",
        "theme/../index.php",
        "theme\\index.php",
        ".",
        "dir/file\nother.php",
        "dir/file\rother.php",
        "dir/file\x1fother.php",
    ],
)
def test_execution_plan_rejects_unsafe_operation_path(path: str) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "project": "demo",
            "release_id": "release-1",
            "artifact_sha256": "abc",
            "strategy": {"apply": "overlay", "delete_unknown": False},
            "operations": [
                {"op": "create", "path": path, "scope": "root", "size": 1, "sha256": "sha"}
            ],
            "created_directories": [],
        },
    )

    with pytest.raises(DeployError, match="Unsafe path|must not be empty"):
        execution_plan_from_json(raw)


@pytest.mark.parametrize(
    "directory",
    ["/theme", "../theme", "theme/..", "theme\\assets", ".", "theme\nassets"],
)
def test_execution_plan_rejects_unsafe_created_directory(directory: str) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "project": "demo",
            "release_id": "release-1",
            "artifact_sha256": "abc",
            "strategy": {"apply": "overlay", "delete_unknown": False},
            "operations": [],
            "created_directories": [directory],
        },
    )

    with pytest.raises(DeployError, match="Unsafe path|must not be empty"):
        execution_plan_from_json(raw)


def test_execution_plan_json_roundtrip(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    remote = tmp_path / "remote"
    artifact.mkdir()
    remote.mkdir()
    (artifact / "index.html").write_text("new\n", encoding="utf-8")
    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/srv/deploy"},
            "scope": [{"name": "root", "source": ".", "target": "."}],
        },
    )
    plan = build_execution_plan(
        config,
        {
            "release_id": "release-1",
            "files": [
                {
                    "path": "index.html",
                    "scope": "root",
                    "size": 4,
                    "sha256": "2fea4d8bbabb7617ca6f0c0529764a31b5c7fd6bb5015004f7a38bc8c6a76ba1",
                },
            ],
        },
        artifact,
        remote,
        "artifact-sha",
    )

    assert execution_plan_from_json(execution_plan_to_json(plan)) == plan


def sha256(path: Path) -> str:
    return sha256_file(path)
