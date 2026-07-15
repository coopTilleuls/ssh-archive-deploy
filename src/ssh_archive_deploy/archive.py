from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import cast

from ssh_archive_deploy import __version__
from ssh_archive_deploy.config import DeployConfig, GeneratedInputConfig, ScopeConfig
from ssh_archive_deploy.errors import DeployError

MANIFEST_NAME = "DEPLOYMENT_MANIFEST.json"
MANIFEST_VERSION = 2
ASCII_CONTROL_CODES = frozenset(range(0x20)) | {0x7F}
LFS_POINTER_MAX_SIZE = 1024
LFS_POINTER_VERSIONS = {
    "http://git-media.io/v/2",
    "https://git-lfs.github.com/spec/v1",
    "https://hawser.github.com/spec/v1",
}
LFS_POINTER_EXTENSION_PATTERN = re.compile(r"ext-([0-9])-[A-Za-z0-9_].*")
LFS_POINTER_OID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
LFS_POINTER_SIZE_PATTERN = re.compile(r"0|[1-9][0-9]*")


@dataclass(frozen=True)
class ManifestFile:
    path: str
    scope: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ManifestGeneratedInput:
    path: str
    required_paths: list[str]
    files: list[str]


@dataclass(frozen=True)
class ManifestScope:
    name: str
    source: str
    target: str
    files: list[str]
    generated: list[ManifestGeneratedInput]


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
        reject_git_lfs_pointers(package_root, manifest.files)
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
        manifest_records = manifest_file_records(manifest, config)

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


def manifest_file_records(
    manifest: object,
    config: DeployConfig | None = None,
) -> dict[str, tuple[int, str]]:
    if not isinstance(manifest, dict):
        raise DeployError("Archive manifest must be a JSON object.")
    version = manifest.get("version")
    if version != MANIFEST_VERSION:
        raise DeployError(f"Unsupported archive manifest version: {version!r}")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise DeployError("Archive manifest files must be a list.")

    records: dict[str, tuple[int, str]] = {}
    file_scopes: dict[str, str] = {}
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise DeployError("Archive manifest file entry must be an object.")
        path = raw_file.get("path")
        scope = raw_file.get("scope")
        size = raw_file.get("size")
        sha256 = raw_file.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(scope, str)
            or not isinstance(size, int)
            or not isinstance(sha256, str)
        ):
            raise DeployError("Archive manifest file entry is incomplete.")
        if path in records:
            raise DeployError(f"Archive manifest contains duplicate path: {path}")
        records[path] = (size, sha256)
        file_scopes[path] = scope
    validate_manifest_scope_metadata(manifest, file_scopes, config)
    return records


def validate_manifest_scope_metadata(
    manifest: dict[object, object],
    file_scopes: dict[str, str],
    config: DeployConfig | None = None,
) -> None:
    validate_manifest_project(manifest, config)
    raw_scopes = manifest.get("scopes")
    if not isinstance(raw_scopes, list):
        raise DeployError("Archive manifest scopes must be a list.")

    configured_scopes: dict[str, ScopeConfig] = (
        {scope.name: scope for scope in config.scopes} if config else {}
    )
    scope_names: set[str] = set()
    declared_file_scopes: dict[str, str] = {}
    for raw_scope in raw_scopes:
        name, source, target, scope_files, raw_generated = parse_manifest_scope(raw_scope)
        if name in scope_names:
            raise DeployError(f"Archive manifest contains duplicate scope: {name}")
        scope_names.add(name)
        record_manifest_scope_files(name, target, scope_files, declared_file_scopes)
        generated_contract = validate_manifest_generated_inputs(raw_generated, scope_files, target)
        if config is not None:
            validate_manifest_scope_config(
                name,
                source,
                target,
                generated_contract,
                configured_scopes,
            )

    if declared_file_scopes != file_scopes:
        raise DeployError("Archive manifest scope file lists do not match manifest files.")
    if config is not None and scope_names != set(configured_scopes):
        raise DeployError("Archive manifest scopes do not match configuration.")


def validate_manifest_project(
    manifest: dict[object, object],
    config: DeployConfig | None,
) -> None:
    project = manifest.get("project")
    if not isinstance(project, str) or not project:
        raise DeployError("Archive manifest project must be a non-empty string.")
    if config is not None and project != config.project:
        raise DeployError("Archive manifest project does not match configuration.")


def parse_manifest_scope(
    raw_scope: object,
) -> tuple[str, str, str, set[str], list[object]]:
    if not isinstance(raw_scope, dict):
        raise DeployError("Archive manifest scope entry must be an object.")
    name = raw_scope.get("name")
    source = raw_scope.get("source")
    target = raw_scope.get("target")
    raw_scope_files = raw_scope.get("files")
    raw_generated = raw_scope.get("generated")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(source, str)
        or not isinstance(target, str)
        or not isinstance(raw_scope_files, list)
        or not isinstance(raw_generated, list)
    ):
        raise DeployError("Archive manifest scope entry is incomplete.")
    validate_manifest_relative_path(source, "Archive manifest scope source", allow_dot=True)
    validate_manifest_relative_path(target, "Archive manifest scope target", allow_dot=True)
    return (
        name,
        source,
        target,
        string_set(raw_scope_files, "Archive manifest scope files"),
        raw_generated,
    )


def record_manifest_scope_files(
    name: str,
    target: str,
    scope_files: set[str],
    declared_file_scopes: dict[str, str],
) -> None:
    for scope_file in scope_files:
        validate_manifest_relative_path(scope_file, "Archive manifest scope file")
        if not path_in_target(scope_file, target):
            raise DeployError(f"Archive manifest scope file is outside its target: {scope_file}")
        if scope_file in declared_file_scopes:
            raise DeployError("Archive manifest assigns a file to multiple scopes.")
        declared_file_scopes[scope_file] = name


def validate_manifest_scope_config(
    name: str,
    source: str,
    target: str,
    generated_contract: dict[str, frozenset[str]],
    configured_scopes: dict[str, ScopeConfig],
) -> None:
    configured_scope = configured_scopes.get(name)
    if configured_scope is None:
        raise DeployError(f"Archive manifest contains unconfigured scope: {name}")
    if source != configured_scope.source or target != configured_scope.target:
        raise DeployError(f"Archive manifest scope mapping does not match configuration: {name}")
    expected_generated = {
        generated.path: frozenset(generated.required_paths)
        for generated in configured_scope.generated
    }
    if generated_contract != expected_generated:
        raise DeployError(f"Archive manifest generated inputs do not match configuration: {name}")


def validate_manifest_generated_inputs(
    raw_generated: list[object],
    scope_files: set[str],
    scope_target: str,
) -> dict[str, frozenset[str]]:
    generated_paths: set[str] = set()
    generated_files: set[str] = set()
    generated_contract: dict[str, frozenset[str]] = {}
    parsed_inputs = [parse_manifest_generated_input(raw_input) for raw_input in raw_generated]
    for path, _, _ in parsed_inputs:
        if path in generated_paths:
            raise DeployError(f"Archive manifest contains duplicate generated input: {path}")
        for previous_path in generated_paths:
            if paths_overlap(path, previous_path):
                raise DeployError(
                    "Archive manifest generated input paths must not overlap: "
                    f"{previous_path} and {path}"
                )
        generated_paths.add(path)

    for path, required_path_set, files in parsed_inputs:
        if not files.issubset(scope_files):
            raise DeployError("Archive manifest generated files are outside their scope.")
        target_prefix = target_relative_path(scope_target, path)
        if any(not path_in_target(file, target_prefix) for file in files):
            raise DeployError(
                f"Archive manifest generated files are outside their declared path: {path}"
            )
        expected_files = {file for file in scope_files if path_in_target(file, target_prefix)}
        if files != expected_files:
            raise DeployError(
                f"Archive manifest generated file list is incomplete for declared path: {path}"
            )
        for required_path in required_path_set:
            required_prefix = f"{target_prefix}/{required_path}"
            if not any(path_in_target(file, required_prefix) for file in files):
                raise DeployError(
                    "Archive manifest generated required path does not contribute content: "
                    f"{path}/{required_path}"
                )
        if generated_files.intersection(files):
            raise DeployError("Archive manifest assigns a file to multiple generated inputs.")
        generated_files.update(files)
        generated_contract[path] = frozenset(required_path_set)
    return generated_contract


def parse_manifest_generated_input(raw_input: object) -> tuple[str, set[str], set[str]]:
    if not isinstance(raw_input, dict):
        raise DeployError("Archive manifest generated input must be an object.")
    path = raw_input.get("path")
    required_paths = raw_input.get("required_paths")
    raw_files = raw_input.get("files")
    if not isinstance(path, str) or not isinstance(required_paths, list):
        raise DeployError("Archive manifest generated input is incomplete.")
    validate_manifest_relative_path(path, "Archive manifest generated input path")
    required_path_set = string_set(
        required_paths,
        "Archive manifest generated required paths",
    )
    for required_path in required_path_set:
        validate_manifest_relative_path(
            required_path,
            "Archive manifest generated required path",
        )
    files = string_set(raw_files, "Archive manifest generated files")
    if not files:
        raise DeployError("Archive manifest generated input must contribute at least one file.")
    return path, required_path_set, files


def string_set(value: object, name: str) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeployError(f"{name} must be a list of strings.")
    if len(value) != len(set(value)):
        raise DeployError(f"{name} must not contain duplicates.")
    return set(value)


def validate_manifest_relative_path(value: str, name: str, *, allow_dot: bool = False) -> None:
    validate_line_safe_path(value, name)
    parts = PurePosixPath(value).parts
    if (
        not value
        or (value == "." and not allow_dot)
        or value.startswith("/")
        or "\\" in value
        or "." in parts
        or ".." in parts
    ):
        raise DeployError(f"{name} must be a safe relative path: {value}")


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
    lfs_pointers: list[str] = []
    for path, member in archive_files.items():
        expected_size, expected_sha256 = manifest_records[path]
        if member.size != expected_size:
            raise DeployError(f"Archive file size does not match manifest: {path}")
        file_handle = tar.extractfile(member)
        if file_handle is None:
            raise DeployError(f"Archive file is not readable: {path}")
        digest = hashlib.sha256()
        pointer_data = bytearray()
        while chunk := file_handle.read(1024 * 1024):
            digest.update(chunk)
            if member.size < LFS_POINTER_MAX_SIZE:
                pointer_data.extend(chunk)
        if digest.hexdigest() != expected_sha256:
            raise DeployError(f"Archive file checksum does not match manifest: {path}")
        if is_git_lfs_pointer(bytes(pointer_data)):
            lfs_pointers.append(path)
    raise_for_git_lfs_pointers(lfs_pointers)


def reject_git_lfs_pointers(package_root: Path, files: list[ManifestFile]) -> None:
    lfs_pointers = [
        item.path for item in files if is_git_lfs_pointer_file(package_root / item.path)
    ]
    raise_for_git_lfs_pointers(lfs_pointers)


def is_git_lfs_pointer_file(path: Path) -> bool:
    if path.stat().st_size >= LFS_POINTER_MAX_SIZE:
        return False
    return is_git_lfs_pointer(path.read_bytes())


def is_git_lfs_pointer(data: bytes) -> bool:
    text = decode_git_lfs_pointer(data)
    if text is None:
        return False
    entries = parse_git_lfs_pointer_entries(text)
    return entries is not None and (
        LFS_POINTER_OID_PATTERN.fullmatch(entries.get("oid", "")) is not None
        and LFS_POINTER_SIZE_PATTERN.fullmatch(entries.get("size", "")) is not None
    )


def decode_git_lfs_pointer(data: bytes) -> str | None:
    if not data or len(data) >= LFS_POINTER_MAX_SIZE:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n") or "\r" in text:
        return None
    return text


def parse_git_lfs_pointer_entries(text: str) -> dict[str, str] | None:
    lines = text.removesuffix("\n").split("\n")
    if not lines or not has_supported_lfs_pointer_version(lines[0]):
        return None

    entries: dict[str, str] = {}
    keys: list[str] = []
    extension_priorities: set[str] = set()
    for line in lines[1:]:
        if " " not in line:
            return None
        key, value = line.split(" ", 1)
        if not value or value.startswith(" ") or key in entries:
            return None
        if key not in {"oid", "size"}:
            extension_match = LFS_POINTER_EXTENSION_PATTERN.fullmatch(key)
            if (
                extension_match is None
                or extension_match[1] in extension_priorities
                or LFS_POINTER_OID_PATTERN.fullmatch(value) is None
            ):
                return None
            extension_priorities.add(extension_match[1])
        entries[key] = value
        keys.append(key)
    if keys != sorted(keys):
        return None
    return entries


def has_supported_lfs_pointer_version(line: str) -> bool:
    if not line.startswith("version "):
        return False
    version_key, version = line.split(" ", 1)
    return version_key == "version" and version in LFS_POINTER_VERSIONS


def raise_for_git_lfs_pointers(paths: list[str]) -> None:
    if paths:
        raise DeployError("Unresolved Git LFS pointer content: " + ", ".join(sorted(paths)))


def collect_files(
    config: DeployConfig,
    repo_root: Path,
    package_root: Path,
    release_id: str,
) -> Manifest:
    files: list[ManifestFile] = []
    manifest_scopes: list[ManifestScope] = []
    seen_targets: set[str] = set()
    repository_tracked = set(git_tracked_files(repo_root, "."))

    for scope in config.scopes:
        for generated in scope.generated:
            validate_generated_input_not_tracked(scope, generated, repository_tracked)
        tracked = git_tracked_files(repo_root, scope.source)
        selected = [path for path in tracked if include_source_file(config, scope, path)]

        scope_files: list[str] = []
        for source_path in selected:
            target_path = package_source_file(
                repo_root,
                package_root,
                scope,
                source_path,
                seen_targets,
                files,
            )
            if target_path is not None:
                scope_files.append(target_path)

        manifest_generated: list[ManifestGeneratedInput] = []
        for generated in scope.generated:
            generated_files = collect_generated_input_files(repo_root, scope, generated)
            selected_generated = [
                path for path in generated_files if not is_excluded_source_file(config, scope, path)
            ]
            if not selected_generated:
                raise DeployError(
                    f"Generated input must contribute at least one file after exclusions: "
                    f"{scope.name}/{generated.path}"
                )
            validate_required_generated_paths(
                repo_root,
                scope,
                generated,
                selected_generated,
            )

            generated_targets: list[str] = []
            for source_path in selected_generated:
                target_path = package_source_file(
                    repo_root,
                    package_root,
                    scope,
                    source_path,
                    seen_targets,
                    files,
                )
                if target_path is not None:
                    scope_files.append(target_path)
                    generated_targets.append(target_path)

            manifest_generated.append(
                ManifestGeneratedInput(
                    path=generated.path,
                    required_paths=generated.required_paths,
                    files=sorted(generated_targets),
                )
            )

        if not scope_files:
            raise DeployError(f"No files selected for scope: {scope.name}")

        manifest_scopes.append(
            ManifestScope(
                name=scope.name,
                source=scope.source,
                target=scope.target,
                files=sorted(scope_files),
                generated=manifest_generated,
            ),
        )

    return Manifest(
        version=MANIFEST_VERSION,
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

    if scope.include and not any(
        match_pattern(rel_to_source, pattern) for pattern in scope.include
    ):
        return False
    return not is_excluded_source_file(config, scope, source_path)


def is_excluded_source_file(
    config: DeployConfig,
    scope: ScopeConfig,
    source_path: str,
) -> bool:
    rel_to_source = relative_to_source(scope, source_path)
    target_path = target_for_source(scope, source_path)
    if any(match_pattern(rel_to_source, pattern) for pattern in scope.exclude):
        return True
    if any(match_pattern(target_path, pattern) for pattern in scope.exclude):
        return True
    return any(match_pattern(target_path, pattern) for pattern in config.exclude)


def package_source_file(
    repo_root: Path,
    package_root: Path,
    scope: ScopeConfig,
    source_path: str,
    seen_targets: set[str],
    files: list[ManifestFile],
) -> str | None:
    validate_line_safe_path(source_path, "source path")
    validate_no_symlink_components(repo_root, source_path)
    full_source = repo_root / source_path
    if not full_source.is_file():
        return None

    target_path = target_for_source(scope, source_path)
    validate_line_safe_path(target_path, "archive path")
    if target_path in seen_targets:
        raise DeployError(f"Multiple inputs write the same target path: {target_path}")
    seen_targets.add(target_path)

    output_path = package_root / target_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(full_source.read_bytes())
    digest = sha256_file(output_path)
    size = output_path.stat().st_size
    files.append(ManifestFile(target_path, scope.name, size, digest))
    return target_path


def collect_generated_input_files(
    repo_root: Path,
    scope: ScopeConfig,
    generated: GeneratedInputConfig,
) -> list[str]:
    source_path = source_relative_path(scope, generated.path)
    validate_no_symlink_components(repo_root, source_path)
    generated_root = repo_root / source_path
    if not generated_root.exists():
        raise DeployError(f"Generated input is missing: {scope.name}/{generated.path}")
    return [path.relative_to(repo_root).as_posix() for path in regular_files_under(generated_root)]


def validate_generated_input_not_tracked(
    scope: ScopeConfig,
    generated: GeneratedInputConfig,
    tracked_files: set[str],
) -> None:
    generated_source = source_relative_path(scope, generated.path)
    for tracked_file in sorted(tracked_files):
        if paths_overlap(generated_source, tracked_file):
            raise DeployError(
                "Generated input overlaps a Git-tracked path: "
                f"{scope.name}/{generated.path} ({tracked_file})"
            )


def regular_files_under(path: Path) -> list[Path]:
    if path.is_symlink():
        raise DeployError(f"Refusing to package generated symlink: {path}")
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise DeployError(f"Unsupported generated input type: {path}")

    files: list[Path] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        files.extend(regular_files_under(child))
    return files


def validate_required_generated_paths(
    repo_root: Path,
    scope: ScopeConfig,
    generated: GeneratedInputConfig,
    selected_files: list[str],
) -> None:
    generated_source = source_relative_path(scope, generated.path)
    selected = set(selected_files)
    for required_path in generated.required_paths:
        required_source = f"{generated_source}/{required_path}"
        validate_no_symlink_components(repo_root, required_source)
        full_required = repo_root / required_source
        if not full_required.exists():
            raise DeployError(
                f"Generated required path is missing: {scope.name}/{generated.path}/{required_path}"
            )
        if full_required.is_file():
            contributes = required_source in selected
        elif full_required.is_dir():
            contributes = any(path.startswith(f"{required_source}/") for path in selected)
        else:
            raise DeployError(f"Unsupported generated required path type: {required_source}")
        if not contributes:
            raise DeployError(
                f"Generated required path is excluded or empty: "
                f"{scope.name}/{generated.path}/{required_path}"
            )


def source_relative_path(scope: ScopeConfig, path: str) -> str:
    if scope.source == ".":
        return path
    return f"{scope.source}/{path}"


def validate_no_symlink_components(repo_root: Path, source_path: str) -> None:
    current = repo_root
    for part in PurePosixPath(source_path).parts:
        if part == ".":
            continue
        current /= part
        if current.is_symlink():
            raise DeployError(f"Refusing to package symlink: {source_path}")


def target_for_source(scope: ScopeConfig, source_path: str) -> str:
    rel = relative_to_source(scope, source_path)
    if scope.target == ".":
        return rel
    return f"{scope.target}/{rel}"


def target_relative_path(target: str, path: str) -> str:
    if target == ".":
        return path
    return f"{target}/{path}"


def paths_overlap(first: str, second: str) -> bool:
    return path_in_target(first, second) or path_in_target(second, first)


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
