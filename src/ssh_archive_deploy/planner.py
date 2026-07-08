from __future__ import annotations

import difflib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from ssh_archive_deploy.archive import MANIFEST_NAME
from ssh_archive_deploy.config import DeployConfig, ScopeConfig


@dataclass(frozen=True)
class ArtifactSnapshot:
    root: Path


@dataclass(frozen=True)
class RemoteSnapshot:
    root: Path


@dataclass(frozen=True)
class TextChange:
    path: str
    insertions: int
    deletions: int


@dataclass(frozen=True)
class ScopePlan:
    name: str
    changed_files: list[str]
    text_changes: list[TextChange]
    binary_changed_files: list[str]
    artifact_only_files: list[str]
    remote_only_files: list[str]


@dataclass(frozen=True)
class ApplyPlan:
    project: str
    release_id: str
    scopes: list[ScopePlan]


def compute_plan(
    config: DeployConfig,
    artifact: ArtifactSnapshot,
    remote: RemoteSnapshot,
    release_id: str,
) -> ApplyPlan:
    return ApplyPlan(
        project=config.project,
        release_id=release_id,
        scopes=[
            compute_scope_plan(config, scope, artifact.root, remote.root) for scope in config.scopes
        ],
    )


def compute_scope_plan(
    config: DeployConfig,
    scope: ScopeConfig,
    artifact_root: Path,
    remote_root: Path,
) -> ScopePlan:
    target = "" if scope.target == "." else scope.target
    artifact_target = artifact_root / target
    remote_target = remote_root / target

    artifact_files = sorted(
        path
        for path in list_files(artifact_target)
        if path_matches_scope(config, scope, target, path)
    )
    remote_files = sorted(
        path
        for path in list_files(remote_target)
        if path_matches_scope(config, scope, target, path)
    )

    changed: list[str] = []
    text_changes: list[TextChange] = []
    binary_changed: list[str] = []

    remote_file_set = set(remote_files)
    for file_path in artifact_files:
        if file_path not in remote_file_set:
            continue
        artifact_file = artifact_target / file_path
        remote_file = remote_target / file_path
        if artifact_file.read_bytes() == remote_file.read_bytes():
            continue
        changed.append(file_path)
        if is_text_file(artifact_file) and is_text_file(remote_file):
            added, deleted = count_text_diff(remote_file, artifact_file)
            text_changes.append(TextChange(file_path, added, deleted))
        else:
            binary_changed.append(file_path)

    return ScopePlan(
        name=scope.name,
        changed_files=changed,
        text_changes=text_changes,
        binary_changed_files=binary_changed,
        artifact_only_files=sorted(set(artifact_files) - set(remote_files)),
        remote_only_files=sorted(set(remote_files) - set(artifact_files)),
    )


def path_matches_scope(config: DeployConfig, scope: ScopeConfig, target: str, path: str) -> bool:
    full_path = path if target == "" else f"{target}/{path}"
    if scope.include and not any(match(path, pattern) for pattern in scope.include):
        return False
    if any(match(path, pattern) or match(full_path, pattern) for pattern in scope.exclude):
        return False
    return not any(match(full_path, pattern) for pattern in config.exclude)


def match(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    normalized_pattern = pattern.strip("/")
    return fnmatch(normalized, normalized_pattern) or (
        normalized_pattern.endswith("/**")
        and normalized.startswith(normalized_pattern.removesuffix("/**") + "/")
    )


def list_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        relative_path(root, path)
        for path in root.rglob("*")
        if path.is_file() and is_planned(root, path)
    )


def is_planned(root: Path, path: Path) -> bool:
    return relative_path(root, path) != MANIFEST_NAME


def relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def is_text_file(path: Path) -> bool:
    chunk = path.read_bytes()[:4096]
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def count_text_diff(old: Path, new: Path) -> tuple[int, int]:
    old_lines = old.read_text(encoding="utf-8", errors="replace").splitlines()
    new_lines = new.read_text(encoding="utf-8", errors="replace").splitlines()
    added = 0
    deleted = 0
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted
