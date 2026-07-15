from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import override

import pytest

from ssh_archive_deploy.archive import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    build_archive,
    first_duplicate_name,
    is_git_lfs_pointer,
    manifest_file_records,
    read_manifest_from_archive,
    validate_archive,
)
from ssh_archive_deploy.config import DeployConfig, parse_config
from ssh_archive_deploy.errors import DeployError

LFS_FIXTURE_ROOT = Path(__file__).parent / "fixtures/git-lfs"


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=path, check=True, stdout=subprocess.PIPE)


def generated_config(
    *,
    generated: list[dict[str, object]],
    exclude: list[str] | None = None,
    include: list[str] | None = None,
) -> DeployConfig:
    return parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [
                {
                    "name": "theme",
                    "source": "theme",
                    "target": "wp-content/themes/demo",
                    "include": include or [],
                    "exclude": exclude or [],
                    "generated": generated,
                },
            ],
        },
    )


def manifest_with_generated(
    generated: list[dict[str, object]],
    scope_files: list[str],
) -> dict[str, object]:
    return {
        "version": MANIFEST_VERSION,
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
                "target": "wp-content/themes/demo",
                "files": scope_files,
                "generated": generated,
            },
        ],
        "files": [
            {
                "path": path,
                "scope": "theme",
                "size": 1,
                "sha256": "0" * 64,
            }
            for path in scope_files
        ],
    }


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
            "version": 2,
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


def test_build_rejects_unresolved_lfs_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    shutil.copyfile(LFS_FIXTURE_ROOT / "pointer.psd", tmp_path / "theme/image.psd")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = generated_config(generated=[])
    archive = tmp_path / "site.tar.gz"

    with pytest.raises(
        DeployError,
        match=r"Unresolved Git LFS pointer content: wp-content/themes/demo/image\.psd",
    ):
        build_archive(config, archive, "release-1")

    assert not archive.exists()


def test_build_accepts_resolved_lfs_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    shutil.copyfile(LFS_FIXTURE_ROOT / "resolved.psd", tmp_path / "theme/image.psd")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    manifest = build_archive(
        generated_config(generated=[]),
        tmp_path / "site.tar.gz",
        "release-1",
    )

    assert [item.path for item in manifest.files] == ["wp-content/themes/demo/image.psd"]


def test_git_lfs_pointer_detection_is_structural() -> None:
    pointer = (LFS_FIXTURE_ROOT / "pointer.psd").read_bytes()
    pointer_with_extension = pointer.replace(
        b"oid sha256:",
        b"ext-0-example sha256:4d7a214614ab2935c943f9e0ff69d22e"
        b"adbb8f32b1258daaa5e2ca24d17e2393\noid sha256:",
    )
    pointer_with_uppercase_extension = pointer_with_extension.replace(
        b"ext-0-example", b"ext-0-Example"
    )
    pointer_with_underscore_extension = pointer_with_extension.replace(
        b"ext-0-example", b"ext-0-example_name"
    )
    pointer_with_punctuated_extension = pointer_with_extension.replace(
        b"ext-0-example", b"ext-0-example-name.v1"
    )
    pointer_with_invalid_extension = pointer.replace(
        b"oid sha256:",
        b"ext-0-example synthetic-extension\noid sha256:",
    )
    pointer_with_unknown_key = pointer.replace(
        b"oid sha256:",
        b"aaa value\noid sha256:",
    )
    pointer_with_duplicate_extension_priority = pointer.replace(
        b"oid sha256:",
        b"ext-0-first sha256:4d7a214614ab2935c943f9e0ff69d22e"
        b"adbb8f32b1258daaa5e2ca24d17e2393\n"
        b"ext-0-second sha256:4d7a214614ab2935c943f9e0ff69d22e"
        b"adbb8f32b1258daaa5e2ca24d17e2393\noid sha256:",
    )

    assert is_git_lfs_pointer(pointer)
    assert is_git_lfs_pointer(pointer_with_extension)
    assert is_git_lfs_pointer(pointer_with_uppercase_extension)
    assert is_git_lfs_pointer(pointer_with_underscore_extension)
    assert is_git_lfs_pointer(pointer_with_punctuated_extension)
    assert not is_git_lfs_pointer(pointer_with_invalid_extension)
    assert not is_git_lfs_pointer(pointer_with_unknown_key)
    assert not is_git_lfs_pointer(pointer_with_duplicate_extension_priority)
    assert not is_git_lfs_pointer(b"Documentation: https://git-lfs.github.com/spec/v1\n")
    assert not is_git_lfs_pointer(b"\x00\xffbinary content\n")
    assert not is_git_lfs_pointer(pointer + b"x" * 1024)


def test_build_rejects_generated_lfs_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/vendor").mkdir()
    shutil.copyfile(LFS_FIXTURE_ROOT / "pointer.psd", tmp_path / "theme/vendor/image.psd")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        DeployError,
        match=r"Unresolved Git LFS pointer content: wp-content/themes/demo/vendor/image\.psd",
    ):
        build_archive(
            generated_config(generated=[{"path": "vendor"}]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_reports_lfs_pointer_paths_in_sorted_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    for name in ["b.psd", "a.psd"]:
        shutil.copyfile(LFS_FIXTURE_ROOT / "pointer.psd", tmp_path / f"theme/{name}")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError) as error:
        build_archive(
            generated_config(generated=[]),
            tmp_path / "site.tar.gz",
            "release-1",
        )

    assert str(error.value) == (
        "Unresolved Git LFS pointer content: "
        "wp-content/themes/demo/a.psd, wp-content/themes/demo/b.psd"
    )


def test_build_rejects_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / "theme/link.php").symlink_to("index.php")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [{"name": "theme", "source": "theme", "target": "theme"}],
        },
    )

    with pytest.raises(DeployError, match="symlink"):
        build_archive(config, tmp_path / "site.tar.gz", "release-1")


def test_build_includes_declared_generated_composer_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    fixture = Path(__file__).parent / "fixtures/generated-composer"
    shutil.copytree(fixture / "project/theme", tmp_path / "theme")
    (tmp_path / "theme/index.php").write_text("<?php\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    shutil.copytree(fixture / "generated/vendor", tmp_path / "theme/vendor")
    monkeypatch.chdir(tmp_path)

    config = generated_config(
        generated=[{"path": "vendor", "required_paths": ["autoload.php"]}],
        include=["index.php"],
    )
    archive = tmp_path / "site.tar.gz"

    manifest = build_archive(config, archive, "release-1")

    assert manifest.version == MANIFEST_VERSION
    assert [item.path for item in manifest.files] == [
        "wp-content/themes/demo/index.php",
        "wp-content/themes/demo/vendor/autoload.php",
        "wp-content/themes/demo/vendor/composer/installed.php",
    ]
    assert manifest.scopes[0].generated[0].path == "vendor"
    assert manifest.scopes[0].generated[0].required_paths == ["autoload.php"]
    assert manifest.scopes[0].generated[0].files == [
        "wp-content/themes/demo/vendor/autoload.php",
        "wp-content/themes/demo/vendor/composer/installed.php",
    ]
    validate_archive(archive, config)


def test_build_accepts_generated_only_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / ".gitignore").write_text("theme/build/\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/build").mkdir()
    (tmp_path / "theme/build/app.js").write_text("built\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    manifest = build_archive(
        generated_config(generated=[{"path": "build"}]),
        tmp_path / "site.tar.gz",
        "release-1",
    )

    assert [item.path for item in manifest.files] == ["wp-content/themes/demo/build/app.js"]


def test_build_accepts_generated_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / ".gitignore").write_text("theme/build.css\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/build.css").write_text("built\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    manifest = build_archive(
        generated_config(generated=[{"path": "build.css"}]),
        tmp_path / "site.tar.gz",
        "release-1",
    )

    assert [item.path for item in manifest.files] == ["wp-content/themes/demo/build.css"]


def test_build_rejects_missing_generated_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="Generated input is missing"):
        build_archive(
            generated_config(generated=[{"path": "vendor"}]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_empty_generated_input_after_exclusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/vendor/cache.php").write_text("cache\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="must contribute at least one file"):
        build_archive(
            generated_config(generated=[{"path": "vendor"}], exclude=["vendor/**"]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_missing_generated_required_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/vendor/package.php").write_text("package\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="required path is missing"):
        build_archive(
            generated_config(
                generated=[{"path": "vendor", "required_paths": ["autoload.php"]}],
            ),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_excluded_generated_required_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/vendor/autoload.php").write_text("autoload\n", encoding="utf-8")
    (tmp_path / "theme/vendor/package.php").write_text("package\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="required path is excluded"):
        build_archive(
            generated_config(
                generated=[{"path": "vendor", "required_paths": ["autoload.php"]}],
                exclude=["vendor/autoload.php"],
            ),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_generated_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/vendor/autoload.php").symlink_to("../index.php")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="generated symlink"):
        build_archive(
            generated_config(generated=[{"path": "vendor"}]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_generated_special_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("theme/vendor/\n", encoding="utf-8")
    commit_all(tmp_path)
    os.mkfifo(tmp_path / "theme/vendor/build.pipe")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="Unsupported generated input type"):
        build_archive(
            generated_config(generated=[{"path": "vendor"}]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_generated_overlap_with_tracked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / "theme/vendor/autoload.php").write_text("autoload\n", encoding="utf-8")
    commit_all(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="overlaps a Git-tracked path"):
        build_archive(
            generated_config(generated=[{"path": "vendor"}], include=["index.php"]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_excluded_tracked_file_under_generated_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme/vendor").mkdir(parents=True)
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    (tmp_path / "theme/vendor/tracked.php").write_text("tracked\n", encoding="utf-8")
    commit_all(tmp_path)
    (tmp_path / "theme/vendor/generated.php").write_text("generated\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="overlaps a Git-tracked path"):
        build_archive(
            generated_config(
                generated=[{"path": "vendor"}],
                exclude=["vendor/tracked.php"],
            ),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_generated_input_at_gitlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    commit_all(tmp_path)
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "theme/vendor").mkdir()
    (tmp_path / "theme/vendor/autoload.php").write_text("autoload\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit_sha},theme/vendor",
        ],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(DeployError, match="overlaps a Git-tracked path"):
        build_archive(
            generated_config(generated=[{"path": "vendor"}]),
            tmp_path / "site.tar.gz",
            "release-1",
        )


def test_build_rejects_generated_input_below_gitlink_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(tmp_path)
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme/index.php").write_text("ok\n", encoding="utf-8")
    commit_all(tmp_path)
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "theme/vendor/dist/build").mkdir(parents=True)
    (tmp_path / "theme/vendor/dist/build/app.php").write_text("generated\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit_sha},theme/vendor",
        ],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
            "scope": [
                {
                    "name": "vendor",
                    "source": "theme/vendor/dist",
                    "target": "vendor/dist",
                    "generated": [{"path": "build"}],
                },
            ],
        },
    )

    with pytest.raises(DeployError, match="overlaps a Git-tracked path"):
        build_archive(config, tmp_path / "site.tar.gz", "release-1")


def test_manifest_rejects_empty_generated_input() -> None:
    manifest = manifest_with_generated(
        [{"path": "vendor", "required_paths": ["autoload.php"], "files": []}],
        ["wp-content/themes/demo/index.php"],
    )

    with pytest.raises(DeployError, match="must contribute at least one file"):
        manifest_file_records(manifest)


def test_manifest_rejects_generated_file_outside_declared_path() -> None:
    manifest = manifest_with_generated(
        [
            {
                "path": "vendor",
                "required_paths": [],
                "files": ["wp-content/themes/demo/index.php"],
            },
        ],
        ["wp-content/themes/demo/index.php"],
    )

    with pytest.raises(DeployError, match="outside their declared path"):
        manifest_file_records(manifest)


def test_manifest_rejects_incomplete_generated_file_list() -> None:
    autoload_file = "wp-content/themes/demo/vendor/autoload.php"
    package_file = "wp-content/themes/demo/vendor/package.php"
    manifest = manifest_with_generated(
        [
            {
                "path": "vendor",
                "required_paths": ["autoload.php"],
                "files": [autoload_file],
            },
        ],
        [autoload_file, package_file],
    )

    with pytest.raises(DeployError, match="file list is incomplete"):
        manifest_file_records(manifest)


def test_manifest_rejects_required_path_without_contributed_content() -> None:
    generated_file = "wp-content/themes/demo/vendor/package.php"
    manifest = manifest_with_generated(
        [
            {
                "path": "vendor",
                "required_paths": ["autoload.php"],
                "files": [generated_file],
            },
        ],
        [generated_file],
    )

    with pytest.raises(DeployError, match="required path does not contribute content"):
        manifest_file_records(manifest)


def test_manifest_rejects_overlapping_generated_inputs() -> None:
    vendor_file = "wp-content/themes/demo/vendor/autoload.php"
    composer_file = "wp-content/themes/demo/vendor/composer/installed.php"
    manifest = manifest_with_generated(
        [
            {"path": "vendor", "required_paths": [], "files": [vendor_file]},
            {
                "path": "vendor/composer",
                "required_paths": [],
                "files": [composer_file],
            },
        ],
        [vendor_file, composer_file],
    )

    with pytest.raises(DeployError, match="paths must not overlap"):
        manifest_file_records(manifest)


def test_manifest_rejects_generated_contract_different_from_config() -> None:
    generated_file = "wp-content/themes/demo/vendor/autoload.php"
    manifest = manifest_with_generated(
        [
            {
                "path": "vendor",
                "required_paths": ["autoload.php"],
                "files": [generated_file],
            },
        ],
        [generated_file],
    )
    config = generated_config(generated=[{"path": "build"}])

    with pytest.raises(DeployError, match="generated inputs do not match configuration"):
        manifest_file_records(manifest, config)


def test_validate_archive_rejects_manifest_version_one(tmp_path: Path) -> None:
    manifest_path = tmp_path / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps({"version": 1, "scopes": [], "files": []}),
        encoding="utf-8",
    )
    archive = tmp_path / "site.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(manifest_path, arcname=MANIFEST_NAME, recursive=False)

    with pytest.raises(DeployError, match="Unsupported archive manifest version"):
        validate_archive(archive)


@pytest.mark.parametrize(
    "version",
    [
        "https://git-lfs.github.com/spec/v1",
        "https://hawser.github.com/spec/v1",
        "http://git-media.io/v/2",
    ],
)
def test_validate_archive_rejects_unresolved_lfs_pointer(
    tmp_path: Path,
    version: str,
) -> None:
    package = tmp_path / "package"
    deployed_path = "wp-content/themes/demo/image.psd"
    deployed_file = package / deployed_path
    deployed_file.parent.mkdir(parents=True)
    pointer = (
        (LFS_FIXTURE_ROOT / "pointer.psd")
        .read_bytes()
        .replace(
            b"https://git-lfs.github.com/spec/v1",
            version.encode(),
        )
    )
    deployed_file.write_bytes(pointer)
    manifest = manifest_with_generated([], [deployed_path])
    manifest["files"] = [
        {
            "path": deployed_path,
            "scope": "theme",
            "size": len(pointer),
            "sha256": hashlib.sha256(pointer).hexdigest(),
        },
    ]
    (package / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "site.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(package / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
        tar.add(deployed_file, arcname=deployed_path, recursive=False)

    with pytest.raises(DeployError, match="Unresolved Git LFS pointer content"):
        validate_archive(archive)


def test_validate_archive_rejects_checksum_mismatch(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "theme").mkdir()
    (package / "theme/index.php").write_text("actual\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "version": MANIFEST_VERSION,
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
                "generated": [],
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
    manifest: dict[str, object] = {
        "version": MANIFEST_VERSION,
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
                "generated": [],
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
    manifest: dict[str, object] = {
        "version": MANIFEST_VERSION,
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
                "generated": [],
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
