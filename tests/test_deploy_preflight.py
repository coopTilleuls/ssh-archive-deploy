from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

from ssh_archive_deploy.archive import sha256_file
from ssh_archive_deploy.config import DeployConfig, parse_config
from ssh_archive_deploy.deploy import (
    apply_preflight_script,
    apply_recovery_state_script,
    apply_script,
    recovery_apply_script,
    rollback_preflight_script,
    rollback_script,
)
from ssh_archive_deploy.execution_plan import ExecutionOperation, ExecutionPlan, ExecutionStrategy


def test_apply_preflight_rejects_modified_replace(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    root.mkdir()
    target = root / "index.php"
    target.write_text("old\n", encoding="utf-8")
    old_sha = sha256_file(target)
    target.write_text("manual change\n", encoding="utf-8")
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            4,
            sha256="new-sha",
            remote_sha256_before=old_sha,
            remote_size_before=4,
        ),
    )

    result = run_script(apply_preflight_script(config_for(root), plan))

    assert result.returncode != 0
    assert "remote file changed before apply: index.php" in result.stderr


def test_apply_preflight_rejects_created_path_that_appeared(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    root.mkdir()
    (root / "new.php").write_text("manual\n", encoding="utf-8")
    plan = plan_with(ExecutionOperation("create", "new.php", "root", 4, sha256="new-sha"))

    result = run_script(apply_preflight_script(config_for(root), plan))

    assert result.returncode != 0
    assert "remote path appeared before apply: new.php" in result.stderr


def test_apply_preflight_rejects_symlink_parent(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "theme").symlink_to(outside)
    plan = plan_with(ExecutionOperation("create", "theme/new.php", "root", 4, sha256="new-sha"))

    result = run_script(apply_preflight_script(config_for(root), plan))

    assert result.returncode != 0
    assert "remote parent is a symlink: theme/new.php" in result.stderr


def test_rollback_preflight_rejects_modified_replaced_file(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    root.mkdir()
    target = root / "index.php"
    target.write_text("artifact\n", encoding="utf-8")
    artifact_sha = sha256_file(target)
    target.write_text("manual change\n", encoding="utf-8")
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            9,
            sha256=artifact_sha,
            remote_sha256_before="old-sha",
            remote_size_before=4,
        ),
    )

    result = run_script(rollback_preflight_script(config_for(root), plan))

    assert result.returncode != 0
    assert "replaced file changed before rollback: index.php" in result.stderr


def test_rollback_preflight_rejects_created_symlink(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    outside = tmp_path / "outside.php"
    root.mkdir()
    outside.write_text("artifact\n", encoding="utf-8")
    artifact_sha = sha256_file(outside)
    (root / "new.php").symlink_to(outside)
    plan = plan_with(ExecutionOperation("create", "new.php", "root", 9, sha256=artifact_sha))

    result = run_script(rollback_preflight_script(config_for(root), plan))

    assert result.returncode != 0
    assert "created file changed before rollback: new.php" in result.stderr


def test_recovery_apply_resumes_mixed_before_and_deployed_state(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    transaction_dir = tmp_path / "deploy/transactions/tx-1"
    artifact = tmp_path / "artifact"
    root.mkdir()
    transaction_dir.mkdir(parents=True)
    artifact.mkdir()
    before = root / "index.php"
    before.write_text("old\n", encoding="utf-8")
    before_sha = sha256_file(before)
    (root / "created.php").write_text("new\n", encoding="utf-8")
    (artifact / "index.php").write_text("new\n", encoding="utf-8")
    (artifact / "created.php").write_text("new\n", encoding="utf-8")
    (transaction_dir / "apply-plan.json").write_text("{}\n", encoding="utf-8")
    write_tar(transaction_dir / "artifact.tar.gz", artifact)
    new_sha = sha256_file(artifact / "index.php")
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            4,
            sha256=new_sha,
            remote_sha256_before=before_sha,
            remote_size_before=4,
        ),
        ExecutionOperation("create", "created.php", "root", 4, sha256=new_sha),
    )

    state = run_script(apply_recovery_state_script(config_for(root), str(transaction_dir), plan))
    result = run_script(recovery_apply_script(config_for(root), str(transaction_dir), plan))

    assert state.returncode == 0
    assert state.stdout.strip() == "before=1 deployed=1 total=2"
    assert result.returncode == 0
    assert before.read_text(encoding="utf-8") == "new\n"
    assert (root / "created.php").read_text(encoding="utf-8") == "new\n"


def test_recovery_apply_rejects_unknown_current_state(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    transaction_dir = tmp_path / "deploy/transactions/tx-1"
    extract = transaction_dir / "extract"
    root.mkdir()
    extract.mkdir(parents=True)
    (transaction_dir / "apply-plan.json").write_text("{}\n", encoding="utf-8")
    current = root / "index.php"
    current.write_text("manual\n", encoding="utf-8")
    (extract / "index.php").write_text("new\n", encoding="utf-8")
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            4,
            sha256=sha256_file(extract / "index.php"),
            remote_sha256_before="old-sha",
            remote_size_before=4,
        ),
    )

    result = run_script(apply_recovery_state_script(config_for(root), str(transaction_dir), plan))

    assert result.returncode != 0
    assert "remote path is not recoverable: index.php" in result.stderr


def test_recovery_apply_validates_artifact_before_copy(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    transaction_dir = tmp_path / "deploy/transactions/tx-1"
    artifact = tmp_path / "artifact"
    expected = tmp_path / "expected.php"
    root.mkdir()
    transaction_dir.mkdir(parents=True)
    artifact.mkdir()
    current = root / "index.php"
    current.write_text("old\n", encoding="utf-8")
    expected.write_text("new\n", encoding="utf-8")
    (artifact / "index.php").write_text("wrong\n", encoding="utf-8")
    write_tar(transaction_dir / "artifact.tar.gz", artifact)
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            expected.stat().st_size,
            sha256=sha256_file(expected),
            remote_sha256_before=sha256_file(current),
            remote_size_before=current.stat().st_size,
        ),
    )

    result = run_script(recovery_apply_script(config_for(root), str(transaction_dir), plan))

    assert result.returncode != 0
    assert "artifact file size mismatch: index.php" in result.stderr
    assert current.read_text(encoding="utf-8") == "old\n"


def test_rollback_validates_checkpoint_before_copy(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    transaction_dir = tmp_path / "deploy/transactions/tx-1"
    checkpoint = tmp_path / "checkpoint"
    before = tmp_path / "before.php"
    root.mkdir()
    transaction_dir.mkdir(parents=True)
    checkpoint.mkdir()
    current = root / "index.php"
    current.write_text("artifact\n", encoding="utf-8")
    before.write_text("old\n", encoding="utf-8")
    (checkpoint / "index.php").write_text("wrong\n", encoding="utf-8")
    write_tar(transaction_dir / "before.tar.gz", checkpoint)
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            current.stat().st_size,
            sha256=sha256_file(current),
            remote_sha256_before=sha256_file(before),
            remote_size_before=before.stat().st_size,
        ),
    )

    result = run_script(rollback_script(config_for(root), str(transaction_dir), plan))

    assert result.returncode != 0
    assert "before checkpoint file size mismatch: index.php" in result.stderr
    assert current.read_text(encoding="utf-8") == "artifact\n"


def test_apply_script_rejects_raced_destination_symlink(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    transaction_dir = tmp_path / "deploy/transactions/tx-1"
    artifact = tmp_path / "artifact"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    transaction_dir.mkdir(parents=True)
    artifact.mkdir()
    outside.write_text("old outside\n", encoding="utf-8")
    (artifact / "index.php").write_text("new\n", encoding="utf-8")
    write_tar(transaction_dir / "artifact.tar.gz", artifact)
    (root / "index.php").symlink_to(outside)
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "index.php",
            "root",
            4,
            sha256=sha256_file(artifact / "index.php"),
            remote_sha256_before="old-sha",
            remote_size_before=4,
        ),
    )

    result = run_script(apply_script(config_for(root), str(transaction_dir), plan))

    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "old outside\n"


def test_rollback_script_does_not_extract_through_symlink_parent(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    target_dir = tmp_path / "deploy/transactions/tx-1"
    before = tmp_path / "before"
    outside = tmp_path / "outside"
    root.mkdir()
    target_dir.mkdir(parents=True)
    before.mkdir()
    outside.mkdir()
    (before / "uploads").mkdir()
    restored = before / "uploads/victim.txt"
    restored.write_text("old\n", encoding="utf-8")
    write_tar(target_dir / "before.tar.gz", before)
    (root / "uploads").mkdir()
    current = root / "uploads/victim.txt"
    current.write_text("new\n", encoding="utf-8")
    new_sha = sha256_file(current)
    current.unlink()
    (root / "uploads").rmdir()
    (root / "uploads").symlink_to(outside)
    plan = plan_with(
        ExecutionOperation(
            "replace",
            "uploads/victim.txt",
            "root",
            4,
            sha256=new_sha,
            remote_sha256_before=sha256_file(restored),
            remote_size_before=4,
        ),
    )

    result = run_script(rollback_script(config_for(root), str(target_dir), plan))

    assert result.returncode != 0
    assert not (outside / "victim.txt").exists()


def config_for(root: Path) -> DeployConfig:
    return parse_config(
        {
            "version": 1,
            "project": "demo",
            "remote": {"root": str(root), "workdir": str(root.parent / "deploy")},
            "scope": [{"name": "root", "source": ".", "target": "."}],
        },
    )


def plan_with(*operations: ExecutionOperation) -> ExecutionPlan:
    return ExecutionPlan(
        schema_version=1,
        project="demo",
        release_id="release-1",
        artifact_sha256="artifact-sha",
        strategy=ExecutionStrategy(apply="overlay", delete_unknown=False),
        operations=list(operations),
        created_directories=[],
    )


def run_script(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-s"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )


def write_tar(path: Path, root: Path) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for item in sorted(root.iterdir()):
            archive.add(item, arcname=item.name)
