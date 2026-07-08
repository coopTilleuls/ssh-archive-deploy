from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import cast

from ssh_archive_deploy import __version__
from ssh_archive_deploy.config import DeployConfig, ScopeConfig
from ssh_archive_deploy.errors import DeployError

MANIFEST_NAME = "DEPLOYMENT_MANIFEST.json"
ASCII_CONTROL_CODES = frozenset(range(0x20)) | {0x7F}


@dataclass(frozen=True)
class ManifestFile:
    path: str
    scope: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ManifestScope:
    name: str
    source: str
    target: str
    files: list[str]


@dataclass(frozen=True)
class Manifest:
    version: int
    tool_version: str
    project: str
    release_id: str
    commit_sha: str
    commit_ref: str
    build_time: str
    scopes: list[ManifestScope]
    files: list[ManifestFile]


def build_archive(config: DeployConfig, output: Path, release_id: str | None = None) -> Manifest:
    repo_root = git_root()
    release = release_id or default_release_id(repo_root)
    validate_release_id(release)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ssh-archive-deploy-") as tmp:
        package_root = Path(tmp) / "package"
        package_root.mkdir()
        manifest = collect_files(config, repo_root, package_root, release)
        manifest_path = package_root / MANIFEST_NAME
        manifest_path.write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")
        write_archive(package_root, output)
        validate_archive(output, config)
        output.with_suffix(output.suffix + ".manifest.json").write_text(
            json.dumps(asdict(manifest), indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest


def validate_archive(archive: Path, config: DeployConfig | None = None) -> None:
    if not archive.is_file():
        raise DeployError(f"Archive not found: {archive}")

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        duplicate_name = first_duplicate_name(names)
        if duplicate_name is not None:
            raise DeployError(f"Archive contains duplicate path: {duplicate_name}")
        if MANIFEST_NAME not in names:
            raise DeployError(f"Archive does not contain {MANIFEST_NAME}.")
        for member in members:
            validate_archive_member(member)

        manifest_member = tar.extractfile(MANIFEST_NAME)
        if manifest_member is None:
            raise DeployError("Archive manifest is not readable.")
        manifest = json.loads(manifest_member.read().decode("utf-8"))
        manifest_records = manifest_file_records(manifest)

        archive_files = archive_file_members(members)
        manifest_paths = set(manifest_records)
        archive_file_paths = set(archive_files)
        if manifest_paths != archive_file_paths:
            raise DeployError("Archive file list does not match manifest.")

        validate_archive_file_contents(tar, archive_files, manifest_records)

    if config is not None:
        allowed_targets = [scope.target for scope in config.scopes]
        for path in archive_file_paths:
            if not any(path_in_target(path, target) for target in allowed_targets):
                raise DeployError(f"Archive contains out-of-scope path: {path}")


def first_duplicate_name(names: list[str]) -> str | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return name
        seen.add(name)
    return None


def manifest_file_records(manifest: object) -> dict[str, tuple[int, str]]:
    if not isinstance(manifest, dict):
        raise DeployError("Archive manifest must be a JSON object.")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise DeployError("Archive manifest files must be a list.")

    records: dict[str, tuple[int, str]] = {}
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise DeployError("Archive manifest file entry must be an object.")
        path = raw_file.get("path")
        size = raw_file.get("size")
        sha256 = raw_file.get("sha256")
        if not isinstance(path, str) or not isinstance(size, int) or not isinstance(sha256, str):
            raise DeployError("Archive manifest file entry is incomplete.")
        if path in records:
            raise DeployError(f"Archive manifest contains duplicate path: {path}")
        records[path] = (size, sha256)
    return records


def archive_file_members(members: list[tarfile.TarInfo]) -> dict[str, tarfile.TarInfo]:
    return {
        member.name: member
        for member in members
        if member.name != MANIFEST_NAME
        and not member.name.endswith("/")
        and not is_directory_name(member.name)
        and member.isfile()
    }


def validate_archive_file_contents(
    tar: tarfile.TarFile,
    archive_files: dict[str, tarfile.TarInfo],
    manifest_records: dict[str, tuple[int, str]],
) -> None:
    for path, member in archive_files.items():
        expected_size, expected_sha256 = manifest_records[path]
        if member.size != expected_size:
            raise DeployError(f"Archive file size does not match manifest: {path}")
        file_handle = tar.extractfile(member)
        if file_handle is None:
            raise DeployError(f"Archive file is not readable: {path}")
        digest = hashlib.sha256()
        while chunk := file_handle.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise DeployError(f"Archive file checksum does not match manifest: {path}")


def collect_files(
    config: DeployConfig,
    repo_root: Path,
    package_root: Path,
    release_id: str,
) -> Manifest:
    files: list[ManifestFile] = []
    manifest_scopes: list[ManifestScope] = []
    seen_targets: set[str] = set()

    for scope in config.scopes:
        tracked = git_tracked_files(repo_root, scope.source)
        selected = [path for path in tracked if include_source_file(config, scope, path)]
        if not selected:
            raise DeployError(f"No tracked files selected for scope: {scope.name}")

        scope_files: list[str] = []
        for source_path in selected:
            validate_line_safe_path(source_path, "git path")
            full_source = repo_root / source_path
            if full_source.is_symlink():
                raise DeployError(f"Refusing to package symlink: {source_path}")
            if not full_source.is_file():
                continue

            target_path = target_for_source(scope, source_path)
            validate_line_safe_path(target_path, "archive path")
            if target_path in seen_targets:
                raise DeployError(f"Multiple scopes write the same target path: {target_path}")
            seen_targets.add(target_path)

            output_path = package_root / target_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(full_source.read_bytes())
            digest = sha256_file(output_path)
            size = output_path.stat().st_size
            files.append(ManifestFile(target_path, scope.name, size, digest))
            scope_files.append(target_path)

        manifest_scopes.append(
            ManifestScope(scope.name, scope.source, scope.target, sorted(scope_files)),
        )

    return Manifest(
        version=1,
        tool_version=__version__,
        project=config.project,
        release_id=release_id,
        commit_sha=git_output(repo_root, "rev-parse", "HEAD"),
        commit_ref=commit_ref(repo_root),
        build_time=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scopes=manifest_scopes,
        files=sorted(files, key=lambda item: item.path),
    )


def include_source_file(config: DeployConfig, scope: ScopeConfig, source_path: str) -> bool:
    rel_to_source = relative_to_source(scope, source_path)
    target_path = target_for_source(scope, source_path)

    if scope.include and not any(
        match_pattern(rel_to_source, pattern) for pattern in scope.include
    ):
        return False
    if any(match_pattern(rel_to_source, pattern) for pattern in scope.exclude):
        return False
    if any(match_pattern(target_path, pattern) for pattern in scope.exclude):
        return False
    return not any(match_pattern(target_path, pattern) for pattern in config.exclude)


def target_for_source(scope: ScopeConfig, source_path: str) -> str:
    rel = relative_to_source(scope, source_path)
    if scope.target == ".":
        return rel
    return f"{scope.target}/{rel}"


def relative_to_source(scope: ScopeConfig, source_path: str) -> str:
    if scope.source == ".":
        return source_path
    prefix = f"{scope.source}/"
    if not source_path.startswith(prefix):
        raise DeployError(f"Git returned file outside scope {scope.name}: {source_path}")
    return source_path.removeprefix(prefix)


def write_archive(package_root: Path, output: Path) -> None:
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.add(package_root / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
        for path in sorted(package_root.rglob("*")):
            if path.name == MANIFEST_NAME or not path.is_file():
                continue
            arcname = path.relative_to(package_root).as_posix()
            tar.add(path, arcname=arcname, recursive=False)


def validate_archive_member(member: tarfile.TarInfo) -> None:
    name = member.name
    validate_line_safe_path(name, "archive path")
    if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
        raise DeployError(f"Unsafe path in archive: {name}")
    if name.startswith("./") or "/./" in name or name.endswith("/."):
        raise DeployError(f"Unsafe path in archive: {name}")
    if "\\" in name:
        raise DeployError(f"Backslash paths are not supported in archive: {name}")
    parts = PurePosixPath(name).parts
    if "." in parts or ".." in parts:
        raise DeployError(f"Unsafe path in archive: {name}")
    if not (member.isfile() or member.isdir()):
        raise DeployError(f"Unsupported archive entry type: {name}")


def validate_line_safe_path(path: str, name: str) -> None:
    if has_ascii_control_character(path):
        raise DeployError(f"{name} contains control characters: {path!r}")


def has_ascii_control_character(value: str) -> bool:
    return any(ord(character) in ASCII_CONTROL_CODES for character in value)


def git_root() -> Path:
    return Path(git_output(Path.cwd(), "rev-parse", "--show-toplevel"))


def git_tracked_files(repo_root: Path, source: str) -> list[str]:
    command = ["git", "ls-files", "-z", "--", source]
    result = subprocess.run(command, cwd=repo_root, check=True, stdout=subprocess.PIPE)
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit_ref(repo_root: Path) -> str:
    commands = [
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        ("describe", "--exact-match", "--tags", "HEAD"),
        ("rev-parse", "--short=12", "HEAD"),
    ]
    for command in commands:
        try:
            return git_output(repo_root, *command)
        except subprocess.CalledProcessError:
            continue
    return "unknown"


def default_release_id(repo_root: Path) -> str:
    short_sha = git_output(repo_root, "rev-parse", "--short=12", "HEAD")
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{short_sha}"


def validate_release_id(value: str) -> None:
    if not value:
        raise DeployError("Release id is empty.")
    allowed_characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(character not in allowed_characters for character in value):
        raise DeployError(f"Unsafe release id: {value}")


def match_pattern(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    normalized_pattern = pattern.strip("/")
    return fnmatch(normalized, normalized_pattern) or (
        normalized_pattern.endswith("/**")
        and normalized.startswith(normalized_pattern.removesuffix("/**") + "/")
    )


def path_in_target(path: str, target: str) -> bool:
    if target == ".":
        return True
    return path == target or path.startswith(f"{target}/")


def is_directory_name(name: str) -> bool:
    return name.endswith("/")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest_from_archive(archive: Path) -> dict[str, object]:
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        if member is None:
            raise DeployError(f"Archive does not contain {MANIFEST_NAME}.")
        return cast(dict[str, object], json.loads(member.read().decode("utf-8")))


def extract_archive(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            validate_archive_member(member)
        tar.extractall(destination, filter="data")


def common_path_roots(paths: list[str]) -> list[str]:
    roots: set[str] = set()
    for path in paths:
        first = path.split("/", 1)[0]
        roots.add(first)
    return sorted(roots)


def remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def chmod_private(path: Path) -> None:
    os.chmod(path, 0o600)
