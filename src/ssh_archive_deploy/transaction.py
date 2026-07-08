from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

from ssh_archive_deploy.config import DeployConfig
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.execution_plan import ExecutionPlan

TRANSACTION_SCHEMA_VERSION = 1

TransactionMode = Literal["apply", "rollback"]
TransactionStatus = Literal[
    "preparing",
    "prepared",
    "applying",
    "applied",
    "rolling_back",
    "rolled_back",
    "failed",
]


@dataclass(frozen=True)
class TransactionStrategy:
    apply: str
    delete_unknown: bool


@dataclass(frozen=True)
class TransactionCheckpoint:
    before_archive: str
    before_manifest: str
    apply_plan: str


@dataclass(frozen=True)
class TransactionSummary:
    create: int = 0
    replace: int = 0
    skip: int = 0
    conflict: int = 0


@dataclass(frozen=True)
class TransactionError:
    stage: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class TransactionRecord:
    schema_version: int
    transaction_id: str
    mode: TransactionMode
    status: TransactionStatus
    project: str
    release_id: str
    artifact_sha256: str
    created_at: str
    updated_at: str
    remote_root: str
    remote_workdir: str
    strategy: TransactionStrategy
    checkpoint: TransactionCheckpoint
    summary: TransactionSummary
    created_files: list[str] = field(default_factory=list)
    replaced_files: list[str] = field(default_factory=list)
    created_directories: list[str] = field(default_factory=list)
    rollback_checkpoint: str | None = None
    error: TransactionError | None = None


def new_apply_transaction(
    config: DeployConfig,
    plan: ExecutionPlan,
    transaction_id: str,
) -> TransactionRecord:
    now = utc_now()
    return TransactionRecord(
        schema_version=TRANSACTION_SCHEMA_VERSION,
        transaction_id=transaction_id,
        mode="apply",
        status="prepared",
        project=config.project,
        release_id=plan.release_id,
        artifact_sha256=plan.artifact_sha256,
        created_at=now,
        updated_at=now,
        remote_root=config.remote.root,
        remote_workdir=config.remote.workdir,
        strategy=TransactionStrategy(
            apply=config.strategy.apply,
            delete_unknown=config.strategy.delete_unknown,
        ),
        checkpoint=TransactionCheckpoint(
            before_archive="before.tar.gz",
            before_manifest="before-manifest.json",
            apply_plan="apply-plan.json",
        ),
        summary=TransactionSummary(
            create=count_operations(plan, "create"),
            replace=count_operations(plan, "replace"),
            skip=count_operations(plan, "skip"),
            conflict=count_operations(plan, "conflict"),
        ),
        created_files=[operation.path for operation in plan.operations if operation.op == "create"],
        replaced_files=[
            operation.path for operation in plan.operations if operation.op == "replace"
        ],
        created_directories=plan.created_directories,
    )


def new_rollback_transaction(
    target: TransactionRecord,
    transaction_id: str,
    rollback_dir: str,
) -> TransactionRecord:
    now = utc_now()
    return TransactionRecord(
        schema_version=TRANSACTION_SCHEMA_VERSION,
        transaction_id=transaction_id,
        mode="rollback",
        status="rolling_back",
        project=target.project,
        release_id=target.release_id,
        artifact_sha256=target.artifact_sha256,
        created_at=now,
        updated_at=now,
        remote_root=target.remote_root,
        remote_workdir=target.remote_workdir,
        strategy=target.strategy,
        checkpoint=target.checkpoint,
        summary=target.summary,
        created_files=target.created_files,
        replaced_files=target.replaced_files,
        created_directories=target.created_directories,
        rollback_checkpoint=rollback_dir,
    )


def with_status(
    transaction: TransactionRecord,
    status: TransactionStatus,
    *,
    error: TransactionError | None = None,
) -> TransactionRecord:
    return TransactionRecord(
        schema_version=transaction.schema_version,
        transaction_id=transaction.transaction_id,
        mode=transaction.mode,
        status=status,
        project=transaction.project,
        release_id=transaction.release_id,
        artifact_sha256=transaction.artifact_sha256,
        created_at=transaction.created_at,
        updated_at=utc_now(),
        remote_root=transaction.remote_root,
        remote_workdir=transaction.remote_workdir,
        strategy=transaction.strategy,
        checkpoint=transaction.checkpoint,
        summary=transaction.summary,
        created_files=transaction.created_files,
        replaced_files=transaction.replaced_files,
        created_directories=transaction.created_directories,
        rollback_checkpoint=transaction.rollback_checkpoint,
        error=error,
    )


def transaction_to_json(transaction: TransactionRecord) -> str:
    payload = asdict(transaction)
    if transaction.error is None:
        payload.pop("error", None)
    return json.dumps(payload, indent=2) + "\n"


def transaction_from_json(raw: str) -> TransactionRecord:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise DeployError("transaction.json must contain an object.")
    return parse_transaction(cast(dict[str, object], payload))


def parse_transaction(payload: dict[str, object]) -> TransactionRecord:
    checkpoint = require_mapping(payload, "checkpoint")
    strategy = require_mapping(payload, "strategy")
    summary = require_mapping(payload, "summary")
    raw_error = payload.get("error")
    return TransactionRecord(
        schema_version=require_int(payload, "schema_version"),
        transaction_id=require_str(payload, "transaction_id"),
        mode=require_mode(payload),
        status=require_status(payload),
        project=require_str(payload, "project"),
        release_id=require_str(payload, "release_id"),
        artifact_sha256=require_str(payload, "artifact_sha256"),
        created_at=require_str(payload, "created_at"),
        updated_at=require_str(payload, "updated_at"),
        remote_root=require_str(payload, "remote_root"),
        remote_workdir=require_str(payload, "remote_workdir"),
        strategy=TransactionStrategy(
            apply=require_str(strategy, "apply"),
            delete_unknown=require_bool(strategy, "delete_unknown"),
        ),
        checkpoint=TransactionCheckpoint(
            before_archive=require_str(checkpoint, "before_archive"),
            before_manifest=require_str(checkpoint, "before_manifest"),
            apply_plan=require_str(checkpoint, "apply_plan"),
        ),
        summary=TransactionSummary(
            create=require_int(summary, "create"),
            replace=require_int(summary, "replace"),
            skip=require_int(summary, "skip"),
            conflict=require_int(summary, "conflict"),
        ),
        created_files=require_str_list(payload, "created_files"),
        replaced_files=require_str_list(payload, "replaced_files"),
        created_directories=require_str_list(payload, "created_directories"),
        rollback_checkpoint=optional_str(payload, "rollback_checkpoint"),
        error=parse_error(raw_error),
    )


def count_operations(plan: ExecutionPlan, operation: str) -> int:
    return sum(1 for item in plan.operations if item.op == operation)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise DeployError(f"{key} must be an object.")
    return cast(dict[str, object], value)


def require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DeployError(f"{key} must be a non-empty string.")
    return value


def optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeployError(f"{key} must be a string.")
    return value


def require_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeployError(f"{key} must be an integer.")
    return value


def require_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise DeployError(f"{key} must be a boolean.")
    return value


def require_str_list(payload: dict[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeployError(f"{key} must be a list of strings.")
    return value


def require_mode(payload: dict[str, object]) -> TransactionMode:
    value = require_str(payload, "mode")
    if value not in ("apply", "rollback"):
        raise DeployError(f"Unsupported transaction mode: {value}")
    return value


def require_status(payload: dict[str, object]) -> TransactionStatus:
    value = require_str(payload, "status")
    allowed = {
        "preparing",
        "prepared",
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
        "failed",
    }
    if value not in allowed:
        raise DeployError(f"Unsupported transaction status: {value}")
    return cast(TransactionStatus, value)


def parse_error(raw_error: object) -> TransactionError | None:
    if raw_error is None:
        return None
    if not isinstance(raw_error, dict):
        raise DeployError("error must be an object.")
    error = cast(dict[str, object], raw_error)
    path = error.get("path")
    if path is not None and not isinstance(path, str):
        raise DeployError("error.path must be a string.")
    return TransactionError(
        stage=require_str(error, "stage"),
        message=require_str(error, "message"),
        path=path,
    )
