from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from ssh_archive_deploy.archive import sha256_file, validate_line_safe_path
from ssh_archive_deploy.config import DeployConfig
from ssh_archive_deploy.errors import DeployError

EXECUTION_PLAN_SCHEMA_VERSION = 1

OperationKind = Literal["create", "replace", "skip", "conflict"]


@dataclass(frozen=True)
class ExecutionOperation:
    op: OperationKind
    path: str
    scope: str
    size: int
    sha256: str
    remote_sha256_before: str | None = None
    remote_size_before: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ExecutionStrategy:
    apply: str
    delete_unknown: bool


@dataclass(frozen=True)
class ExecutionPlan:
    schema_version: int
    project: str
    release_id: str
    artifact_sha256: str
    strategy: ExecutionStrategy
    operations: list[ExecutionOperation]
    created_directories: list[str]


@dataclass(frozen=True)
class BeforeManifestFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BeforeManifest:
    schema_version: int
    files: list[BeforeManifestFile]


def build_execution_plan(
    config: DeployConfig,
    manifest: dict[str, object],
    artifact_root: Path,
    remote_root: Path,
    artifact_sha256: str,
) -> ExecutionPlan:
    release_id = require_manifest_str(manifest, "release_id")
    operations: list[ExecutionOperation] = []

    for record in manifest_files(manifest):
        path = record.path
        artifact_file = artifact_root / path
        remote_file = remote_root / path
        if not artifact_file.is_file():
            raise DeployError(f"Archive file missing after extraction: {path}")
        if not remote_file.exists():
            operations.append(
                ExecutionOperation(
                    op="create",
                    path=path,
                    scope=record.scope,
                    size=record.size,
                    sha256=record.sha256,
                ),
            )
            continue
        if not remote_file.is_file():
            operations.append(
                ExecutionOperation(
                    op="conflict",
                    path=path,
                    scope=record.scope,
                    size=record.size,
                    sha256=record.sha256,
                    reason="remote path is not a regular file",
                ),
            )
            continue
        remote_sha256 = sha256_file(remote_file)
        remote_size = remote_file.stat().st_size
        if remote_sha256 == record.sha256:
            operations.append(
                ExecutionOperation(
                    op="skip",
                    path=path,
                    scope=record.scope,
                    size=record.size,
                    sha256=record.sha256,
                    remote_sha256_before=remote_sha256,
                    remote_size_before=remote_size,
                    reason="identical",
                ),
            )
            continue
        operations.append(
            ExecutionOperation(
                op="replace",
                path=path,
                scope=record.scope,
                size=record.size,
                sha256=record.sha256,
                remote_sha256_before=remote_sha256,
                remote_size_before=remote_size,
            ),
        )

    created_directories = created_parent_directories(
        [operation.path for operation in operations if operation.op == "create"],
        remote_root,
    )
    return ExecutionPlan(
        schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
        project=config.project,
        release_id=release_id,
        artifact_sha256=artifact_sha256,
        strategy=ExecutionStrategy(
            apply=config.strategy.apply,
            delete_unknown=config.strategy.delete_unknown,
        ),
        operations=operations,
        created_directories=created_directories,
    )


def before_manifest(plan: ExecutionPlan) -> BeforeManifest:
    return BeforeManifest(
        schema_version=1,
        files=[
            BeforeManifestFile(
                path=operation.path,
                size=require_int(operation.remote_size_before, operation.path),
                sha256=require_str(operation.remote_sha256_before, operation.path),
            )
            for operation in plan.operations
            if operation.op == "replace"
        ],
    )


def ensure_no_conflicts(plan: ExecutionPlan) -> None:
    conflicts = [operation for operation in plan.operations if operation.op == "conflict"]
    if conflicts:
        first = conflicts[0]
        reason = f": {first.reason}" if first.reason else ""
        raise DeployError(f"Cannot apply archive because of conflict at {first.path}{reason}")


def serialize_execution_plan(plan: ExecutionPlan) -> dict[str, object]:
    return cast(dict[str, object], asdict(plan))


def serialize_before_manifest(manifest: BeforeManifest) -> dict[str, object]:
    return cast(dict[str, object], asdict(manifest))


def execution_plan_to_json(plan: ExecutionPlan) -> str:
    return json.dumps(serialize_execution_plan(plan), indent=2) + "\n"


def execution_plan_from_json(raw: str) -> ExecutionPlan:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise DeployError("apply-plan.json must contain an object.")
    return parse_execution_plan(cast(dict[str, object], payload))


def parse_execution_plan(payload: dict[str, object]) -> ExecutionPlan:
    strategy = require_mapping(payload, "strategy")
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list):
        raise DeployError("apply-plan.json operations must be a list.")
    operations = [parse_operation(item) for item in raw_operations]
    created_directories = require_payload_str_list(payload, "created_directories")
    for operation in operations:
        validate_plan_path(operation.path, "operation path")
    for directory in created_directories:
        validate_plan_path(directory, "created directory")

    return ExecutionPlan(
        schema_version=require_payload_int(payload, "schema_version"),
        project=require_payload_str(payload, "project"),
        release_id=require_payload_str(payload, "release_id"),
        artifact_sha256=require_payload_str(payload, "artifact_sha256"),
        strategy=ExecutionStrategy(
            apply=require_payload_str(strategy, "apply"),
            delete_unknown=require_payload_bool(strategy, "delete_unknown"),
        ),
        operations=operations,
        created_directories=created_directories,
    )


def before_manifest_to_json(manifest: BeforeManifest) -> str:
    return json.dumps(serialize_before_manifest(manifest), indent=2) + "\n"


@dataclass(frozen=True)
class ManifestRecord:
    path: str
    scope: str
    size: int
    sha256: str


def manifest_files(manifest: dict[str, object]) -> list[ManifestRecord]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise DeployError("Archive manifest files must be a list.")
    records: list[ManifestRecord] = []
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
        records.append(ManifestRecord(path, scope, size, sha256))
    return sorted(records, key=lambda record: record.path)


def require_manifest_str(manifest: dict[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise DeployError(f"Archive manifest {key} must be a non-empty string.")
    return value


def parse_operation(raw: object) -> ExecutionOperation:
    if not isinstance(raw, dict):
        raise DeployError("apply-plan.json operation must be an object.")
    payload = cast(dict[str, object], raw)
    operation = require_payload_str(payload, "op")
    if operation not in ("create", "replace", "skip", "conflict"):
        raise DeployError(f"Unsupported apply operation: {operation}")
    remote_sha256_before = optional_payload_str(payload, "remote_sha256_before")
    remote_size_before = optional_payload_int(payload, "remote_size_before")
    reason = optional_payload_str(payload, "reason")
    return ExecutionOperation(
        op=cast(OperationKind, operation),
        path=require_payload_str(payload, "path"),
        scope=require_payload_str(payload, "scope"),
        size=require_payload_int(payload, "size"),
        sha256=require_payload_str(payload, "sha256"),
        remote_sha256_before=remote_sha256_before,
        remote_size_before=remote_size_before,
        reason=reason,
    )


def created_parent_directories(paths: list[str], remote_root: Path) -> list[str]:
    directories: set[str] = set()
    for path in paths:
        parent = Path(path).parent
        parts: list[str] = []
        for part in parent.parts:
            if part == ".":
                continue
            parts.append(part)
            directory = "/".join(parts)
            if not (remote_root / directory).exists():
                directories.add(directory)
    return sorted(directories, key=lambda item: (item.count("/"), item))


def validate_plan_path(path: str, name: str) -> None:
    if not path:
        raise DeployError(f"apply-plan.json {name} must not be empty.")
    try:
        validate_line_safe_path(path, f"apply-plan.json {name}")
    except DeployError as error:
        raise DeployError(f"Unsafe path in apply-plan.json {name}: {path!r}") from error
    if path == ".":
        raise DeployError(f"Unsafe path in apply-plan.json {name}: {path}")
    if path.startswith("/") or "\\" in path:
        raise DeployError(f"Unsafe path in apply-plan.json {name}: {path}")
    parts = PurePosixPath(path).parts
    if "." in parts or ".." in parts:
        raise DeployError(f"Unsafe path in apply-plan.json {name}: {path}")


def require_int(value: int | None, path: str) -> int:
    if value is None:
        raise DeployError(f"Missing remote size before replace operation: {path}")
    return value


def require_str(value: str | None, path: str) -> str:
    if value is None:
        raise DeployError(f"Missing remote checksum before replace operation: {path}")
    return value


def require_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise DeployError(f"{key} must be an object.")
    return cast(dict[str, object], value)


def require_payload_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DeployError(f"{key} must be a non-empty string.")
    return value


def optional_payload_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeployError(f"{key} must be a string.")
    return value


def require_payload_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeployError(f"{key} must be an integer.")
    return value


def optional_payload_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeployError(f"{key} must be an integer.")
    return value


def require_payload_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise DeployError(f"{key} must be a boolean.")
    return value


def require_payload_str_list(payload: dict[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeployError(f"{key} must be a list of strings.")
    return value
