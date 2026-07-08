from __future__ import annotations

import pytest

from ssh_archive_deploy.config import DeployConfig, parse_config
from ssh_archive_deploy.deploy import (
    validate_rollback_plan,
    validate_rollback_target,
    validate_transaction_id,
)
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.execution_plan import ExecutionOperation, ExecutionPlan, ExecutionStrategy
from ssh_archive_deploy.transaction import (
    TransactionCheckpoint,
    TransactionMode,
    TransactionRecord,
    TransactionStrategy,
    TransactionSummary,
)


def test_rejects_unsafe_transaction_id() -> None:
    with pytest.raises(DeployError, match="Unsafe transaction id"):
        validate_transaction_id("../../transaction")


def test_accepts_apply_transaction_id() -> None:
    validate_transaction_id("20260708T120000Z-release-abcdef123456-apply")


def test_rollback_target_must_match_config() -> None:
    config = config_for()
    target = transaction_for(remote_root="/other")

    with pytest.raises(DeployError, match="remote root"):
        validate_rollback_target(config, target)


def test_rollback_target_must_be_apply_transaction() -> None:
    config = config_for()
    target = transaction_for(mode="rollback")

    with pytest.raises(DeployError, match="not an apply transaction"):
        validate_rollback_target(config, target)


def test_rollback_plan_must_match_transaction() -> None:
    config = config_for()
    target = transaction_for()
    plan = plan_for(release_id="other-release")

    with pytest.raises(DeployError, match="release id"):
        validate_rollback_plan(config, target, plan)


def test_rollback_plan_operations_must_match_transaction() -> None:
    config = config_for()
    target = transaction_for()
    plan = plan_for(
        operations=[
            ExecutionOperation(
                "create",
                "extra.php",
                "root",
                4,
                sha256="new-sha",
            ),
        ],
    )

    with pytest.raises(DeployError, match="created files"):
        validate_rollback_plan(config, target, plan)


def config_for() -> DeployConfig:
    return parse_config(
        {
            "version": 1,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/srv/deploy"},
            "scope": [{"name": "root", "source": ".", "target": "."}],
        },
    )


def transaction_for(
    *,
    mode: TransactionMode = "apply",
    project: str = "demo",
    release_id: str = "release-1",
    artifact_sha256: str = "artifact-sha",
    remote_root: str = "/var/www/html",
    remote_workdir: str = "/srv/deploy",
) -> TransactionRecord:
    return TransactionRecord(
        schema_version=1,
        transaction_id="20260708T120000Z-release-abcdef123456-apply",
        mode=mode,
        status="applied",
        project=project,
        release_id=release_id,
        artifact_sha256=artifact_sha256,
        created_at="2026-07-08T12:00:00Z",
        updated_at="2026-07-08T12:00:00Z",
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        strategy=TransactionStrategy(apply="overlay", delete_unknown=False),
        checkpoint=TransactionCheckpoint(
            before_archive="before.tar.gz",
            before_manifest="before-manifest.json",
            apply_plan="apply-plan.json",
        ),
        summary=TransactionSummary(),
    )


def plan_for(
    *,
    project: str = "demo",
    release_id: str = "release-1",
    artifact_sha256: str = "artifact-sha",
    operations: list[ExecutionOperation] | None = None,
) -> ExecutionPlan:
    return ExecutionPlan(
        schema_version=1,
        project=project,
        release_id=release_id,
        artifact_sha256=artifact_sha256,
        strategy=ExecutionStrategy(apply="overlay", delete_unknown=False),
        operations=operations or [],
        created_directories=[],
    )
