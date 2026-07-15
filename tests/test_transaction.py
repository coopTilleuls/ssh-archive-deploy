from __future__ import annotations

from ssh_archive_deploy.config import parse_config
from ssh_archive_deploy.execution_plan import ExecutionOperation, ExecutionPlan, ExecutionStrategy
from ssh_archive_deploy.transaction import (
    new_apply_transaction,
    transaction_from_json,
    transaction_to_json,
    with_status,
)


def test_transaction_json_roundtrip() -> None:
    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/srv/deploy"},
            "scope": [{"name": "root", "source": ".", "target": "."}],
        },
    )
    plan = ExecutionPlan(
        schema_version=1,
        project="demo",
        release_id="release-1",
        artifact_sha256="abc123",
        strategy=ExecutionStrategy(apply="overlay", delete_unknown=False),
        operations=[
            ExecutionOperation("create", "new.php", "root", 3, "sha-new"),
            ExecutionOperation(
                "replace",
                "index.php",
                "root",
                5,
                "sha-index",
                remote_sha256_before="sha-old",
                remote_size_before=4,
            ),
            ExecutionOperation("skip", "same.php", "root", 4, "sha-same", reason="identical"),
        ],
        created_directories=[],
    )

    transaction = new_apply_transaction(config, plan, "tx-1")
    parsed = transaction_from_json(transaction_to_json(transaction))

    assert parsed.transaction_id == "tx-1"
    assert parsed.status == "prepared"
    assert parsed.summary.create == 1
    assert parsed.summary.replace == 1
    assert parsed.summary.skip == 1
    assert parsed.created_files == ["new.php"]
    assert parsed.replaced_files == ["index.php"]


def test_transaction_status_update_preserves_identity() -> None:
    config = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/var/www/html", "workdir": "/srv/deploy"},
            "scope": [{"name": "root", "source": ".", "target": "."}],
        },
    )
    plan = ExecutionPlan(
        schema_version=1,
        project="demo",
        release_id="release-1",
        artifact_sha256="abc123",
        strategy=ExecutionStrategy(apply="overlay", delete_unknown=False),
        operations=[],
        created_directories=[],
    )
    transaction = new_apply_transaction(config, plan, "tx-1")

    updated = with_status(transaction, "applied")

    assert updated.transaction_id == transaction.transaction_id
    assert updated.created_at == transaction.created_at
    assert updated.status == "applied"
