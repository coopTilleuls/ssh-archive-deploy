from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shlex import quote

from ssh_archive_deploy.archive import (
    extract_archive,
    read_manifest_from_archive,
    sha256_file,
    validate_archive,
)
from ssh_archive_deploy.config import DeployConfig
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.execution_plan import (
    ExecutionOperation,
    ExecutionPlan,
    before_manifest,
    before_manifest_to_json,
    build_execution_plan,
    ensure_no_conflicts,
    execution_plan_from_json,
    execution_plan_to_json,
)
from ssh_archive_deploy.report import (
    SshConfig,
    fetch_remote_archive,
    remote_exclude_patterns,
    remote_include_paths,
    ssh_command,
)
from ssh_archive_deploy.transaction import (
    TransactionError,
    TransactionRecord,
    new_apply_transaction,
    new_rollback_transaction,
    transaction_from_json,
    transaction_to_json,
    with_status,
)


@dataclass(frozen=True)
class ApplyResult:
    transaction_id: str
    release_id: str
    created_files: int
    replaced_files: int
    skipped_files: int
    checkpoint_path: str
    noop: bool = False


@dataclass(frozen=True)
class RollbackResult:
    transaction_id: str
    target_transaction_id: str
    release_id: str
    restored_files: int
    deleted_files: int
    checkpoint_path: str


def apply_deployment(config: DeployConfig, archive: Path, ssh: SshConfig) -> ApplyResult:
    validate_mutating_config(config)
    validate_mutating_ssh(ssh)
    validate_archive(archive, config)
    manifest = read_manifest_from_archive(archive)
    artifact_sha256 = sha256_file(archive)
    release_id = require_manifest_str(manifest, "release_id")
    transaction_id = make_transaction_id(release_id, artifact_sha256, "apply")
    lock = RemoteLock(config, ssh, transaction_id, "apply", release_id)
    lock.acquire()
    transaction: TransactionRecord | None = None
    try:
        ensure_remote_state(config, ssh)
        idempotent = existing_release_result(
            config,
            ssh,
            release_id,
            artifact_sha256,
            archive,
        )
        if idempotent is not None:
            return idempotent

        with tempfile.TemporaryDirectory(prefix="ssh-archive-deploy-apply-") as tmp:
            tmp_path = Path(tmp)
            current_root = tmp_path / "current"
            artifact_root = tmp_path / "artifact"
            current_root.mkdir()
            artifact_root.mkdir()

            fetch_remote_archive(config, current_root, ssh)
            extract_archive(archive, artifact_root)
            plan = build_execution_plan(
                config,
                manifest,
                artifact_root,
                current_root,
                artifact_sha256,
            )
            ensure_no_conflicts(plan)
            transaction = new_apply_transaction(config, plan, transaction_id)
            transaction_dir = transaction_path(config, transaction_id)

            ensure_baseline(config, ssh)
            run_ssh_script(ssh, create_transaction_dir_script(transaction_dir))
            upload_file(ssh, archive, f"{transaction_dir}/artifact.tar.gz")
            upload_text(ssh, execution_plan_to_json(plan), f"{transaction_dir}/apply-plan.json")
            upload_text(
                ssh,
                before_manifest_to_json(before_manifest(plan)),
                f"{transaction_dir}/before-manifest.json",
            )
            upload_text(
                ssh,
                transaction_to_json(transaction),
                f"{transaction_dir}/transaction.json",
            )
            run_ssh_script(ssh, apply_preflight_script(config, plan))
            run_ssh_script(ssh, checkpoint_script(config, transaction_dir, plan))
            transaction = with_status(transaction, "applying")
            upload_text(
                ssh,
                transaction_to_json(transaction),
                f"{transaction_dir}/transaction.json",
            )
            run_ssh_script(ssh, apply_script(config, transaction_dir, plan))
            transaction = with_status(transaction, "applied")
            upload_text(
                ssh,
                transaction_to_json(transaction),
                f"{transaction_dir}/transaction.json",
            )
            update_pointer(ssh, config, "latest-successful-apply", transaction_id)
            update_pointer(ssh, config, "latest-transaction", transaction_id)
            apply_retention(config, ssh)

            return ApplyResult(
                transaction_id=transaction_id,
                release_id=release_id,
                created_files=transaction.summary.create,
                replaced_files=transaction.summary.replace,
                skipped_files=transaction.summary.skip,
                checkpoint_path=transaction_dir,
            )
    except Exception as error:
        if transaction is not None:
            failed = with_status(
                transaction,
                "failed",
                error=TransactionError(stage="apply", message=str(error)),
            )
            upload_text(
                ssh,
                transaction_to_json(failed),
                f"{transaction_path(config, transaction.transaction_id)}/transaction.json",
            )
        raise
    finally:
        lock.release()


def rollback_deployment(config: DeployConfig, release: str, ssh: SshConfig) -> RollbackResult:
    validate_mutating_config(config)
    validate_mutating_ssh(ssh)
    if release != "latest":
        raise DeployError("Only rollback --release latest is supported.")

    transaction_id = make_transaction_id(
        "rollback", datetime.now(UTC).strftime("%H%M%S"), "rollback"
    )
    lock = RemoteLock(config, ssh, transaction_id, "rollback", release)
    lock.acquire()
    rollback_record: TransactionRecord | None = None
    try:
        target_transaction_id = read_remote_file(
            ssh,
            f"{config.remote.workdir}/pointers/latest-successful-apply",
        ).strip()
        if not target_transaction_id:
            raise DeployError("No latest successful apply transaction found.")
        validate_transaction_id(target_transaction_id)

        target_dir = transaction_path(config, target_transaction_id)
        target = transaction_from_json(read_remote_file(ssh, f"{target_dir}/transaction.json"))
        validate_rollback_target(config, target)
        if target.status != "applied":
            raise DeployError("Latest transaction is not in applied status.")
        plan = execution_plan_from_json(read_remote_file(ssh, f"{target_dir}/apply-plan.json"))
        validate_rollback_plan(config, target, plan)
        validate_before_manifest(read_remote_file(ssh, f"{target_dir}/before-manifest.json"), plan)
        run_ssh_script(ssh, validate_before_checkpoint_script(config, target_dir, plan))

        with tempfile.TemporaryDirectory(prefix="ssh-archive-deploy-rollback-") as tmp:
            current_root = Path(tmp) / "current"
            current_root.mkdir()
            fetch_remote_archive(config, current_root, ssh)
            validate_rollback_preconditions(plan, current_root)

        rollback_dir = transaction_path(config, transaction_id)
        rollback_record = new_rollback_transaction(target, transaction_id, rollback_dir)
        run_ssh_script(ssh, create_transaction_dir_script(rollback_dir))
        run_ssh_script(ssh, rollback_preflight_script(config, plan))
        run_ssh_script(ssh, rollback_checkpoint_script(config, rollback_dir, plan))
        upload_text(
            ssh,
            transaction_to_json(rollback_record),
            f"{rollback_dir}/transaction.json",
        )
        run_ssh_script(ssh, rollback_script(config, target_dir, plan))
        rollback_record = with_status(rollback_record, "rolled_back")
        upload_text(
            ssh,
            transaction_to_json(rollback_record),
            f"{rollback_dir}/transaction.json",
        )
        update_pointer(ssh, config, "latest-transaction", transaction_id)
        remove_pointer(ssh, config, "latest-successful-apply")
        apply_retention(config, ssh)
        return RollbackResult(
            transaction_id=transaction_id,
            target_transaction_id=target_transaction_id,
            release_id=target.release_id,
            restored_files=len(target.replaced_files),
            deleted_files=len(target.created_files),
            checkpoint_path=rollback_dir,
        )
    except Exception as error:
        if rollback_record is not None:
            failed = with_status(
                rollback_record,
                "failed",
                error=TransactionError(stage="rollback", message=str(error)),
            )
            upload_text(
                ssh,
                transaction_to_json(failed),
                f"{transaction_path(config, rollback_record.transaction_id)}/transaction.json",
            )
        raise
    finally:
        lock.release()


class RemoteLock:
    def __init__(
        self,
        config: DeployConfig,
        ssh: SshConfig,
        transaction_id: str,
        mode: str,
        release_id: str,
    ) -> None:
        self.config = config
        self.ssh = ssh
        self.transaction_id = transaction_id
        self.mode = mode
        self.release_id = release_id
        self.acquired = False

    def acquire(self) -> None:
        lock_payload = json.dumps(
            {
                "transaction_id": self.transaction_id,
                "mode": self.mode,
                "release_id": self.release_id,
                "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            indent=2,
        )
        script = "\n".join(
            [
                "set -eu",
                f"workdir={quote(self.config.remote.workdir)}",
                'mkdir -p "$workdir/locks"',
                'mkdir -p "$workdir/transactions"',
                'mkdir -p "$workdir/pointers"',
                'mkdir -p "$workdir/baselines"',
                'lockdir="$workdir/locks/deploy.lock"',
                'if ! mkdir "$lockdir"; then',
                '  echo "deployment lock already exists: $lockdir" >&2',
                "  exit 78",
                "fi",
                f"printf '%s\\n' {quote(self.transaction_id)} > \"$lockdir/owner\"",
                'tmp="$lockdir/lock.json.tmp"',
                f"cat > \"$tmp\" <<'LOCK_JSON'\n{lock_payload}\nLOCK_JSON",
                'mv "$tmp" "$lockdir/lock.json"',
            ],
        )
        run_ssh_script(self.ssh, script)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        script = "\n".join(
            [
                "set -eu",
                f"lockdir={quote(self.config.remote.workdir + '/locks/deploy.lock')}",
                f"expected={quote(self.transaction_id)}",
                'if [ -f "$lockdir/owner" ] && [ "$(cat "$lockdir/owner")" = "$expected" ]; then',
                '  rm -rf "$lockdir"',
                "fi",
            ],
        )
        run_ssh_script(self.ssh, script, check=False)
        self.acquired = False


def validate_mutating_config(config: DeployConfig) -> None:
    if config.strategy.apply != "overlay":
        raise DeployError("Only strategy.apply=overlay is supported.")
    if config.strategy.delete_unknown:
        raise DeployError("strategy.delete_unknown=true is not supported.")
    if not config.remote.workdir or config.remote.workdir == config.remote.root:
        raise DeployError("remote.workdir must be configured outside remote.root for apply.")


def validate_mutating_ssh(ssh: SshConfig) -> None:
    if not ssh.known_hosts_file:
        raise DeployError("SSH known_hosts file is required for apply and rollback modes.")


def validate_transaction_id(transaction_id: str) -> None:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9._-]+-[0-9a-f]{12}-apply", transaction_id):
        raise DeployError(f"Unsafe transaction id: {transaction_id}")


def validate_rollback_target(config: DeployConfig, target: TransactionRecord) -> None:
    if target.mode != "apply":
        raise DeployError("Latest transaction is not an apply transaction.")
    if target.project != config.project:
        raise DeployError("Latest transaction project does not match config.")
    if target.remote_root != config.remote.root:
        raise DeployError("Latest transaction remote root does not match config.")
    if target.remote_workdir != config.remote.workdir:
        raise DeployError("Latest transaction remote workdir does not match config.")
    if target.strategy.apply != config.strategy.apply:
        raise DeployError("Latest transaction strategy does not match config.")
    if target.strategy.delete_unknown != config.strategy.delete_unknown:
        raise DeployError("Latest transaction strategy does not match config.")


def validate_rollback_plan(
    config: DeployConfig,
    target: TransactionRecord,
    plan: ExecutionPlan,
) -> None:
    if plan.project != config.project:
        raise DeployError("apply-plan.json project does not match config.")
    if plan.release_id != target.release_id:
        raise DeployError("apply-plan.json release id does not match transaction.")
    if plan.artifact_sha256 != target.artifact_sha256:
        raise DeployError("apply-plan.json artifact checksum does not match transaction.")
    if plan.strategy.apply != config.strategy.apply:
        raise DeployError("apply-plan.json strategy does not match config.")
    if plan.strategy.delete_unknown != config.strategy.delete_unknown:
        raise DeployError("apply-plan.json strategy does not match config.")
    if [operation.path for operation in plan.operations if operation.op == "create"] != (
        target.created_files
    ):
        raise DeployError("apply-plan.json created files do not match transaction.")
    if [operation.path for operation in plan.operations if operation.op == "replace"] != (
        target.replaced_files
    ):
        raise DeployError("apply-plan.json replaced files do not match transaction.")
    if plan.created_directories != target.created_directories:
        raise DeployError("apply-plan.json created directories do not match transaction.")
    if count_plan_operations(plan, "create") != target.summary.create:
        raise DeployError("apply-plan.json summary does not match transaction.")
    if count_plan_operations(plan, "replace") != target.summary.replace:
        raise DeployError("apply-plan.json summary does not match transaction.")
    if count_plan_operations(plan, "skip") != target.summary.skip:
        raise DeployError("apply-plan.json summary does not match transaction.")
    if count_plan_operations(plan, "conflict") != target.summary.conflict:
        raise DeployError("apply-plan.json summary does not match transaction.")


def count_plan_operations(plan: ExecutionPlan, operation: str) -> int:
    return sum(1 for item in plan.operations if item.op == operation)


def validate_before_manifest(raw: str, plan: ExecutionPlan) -> None:
    try:
        actual = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DeployError("before-manifest.json is invalid JSON.") from error
    expected = json.loads(before_manifest_to_json(before_manifest(plan)))
    if actual != expected:
        raise DeployError("before-manifest.json does not match apply-plan.json.")


def ensure_remote_state(config: DeployConfig, ssh: SshConfig) -> None:
    script = "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"workdir={quote(config.remote.workdir)}",
            '[ -d "$root" ] || { echo "remote.root does not exist: $root" >&2; exit 1; }',
            'mkdir -p "$workdir/transactions" "$workdir/pointers" "$workdir/baselines"',
            "command -v sh >/dev/null || exit 1",
            "command -v tar >/dev/null || exit 1",
            "command -v mktemp >/dev/null || exit 1",
            "command -v mkdir >/dev/null || exit 1",
            "command -v mv >/dev/null || exit 1",
            "command -v rm >/dev/null || exit 1",
            "command -v cat >/dev/null || exit 1",
            "command -v cut >/dev/null || exit 1",
            "command -v cp >/dev/null || exit 1",
            "command -v dirname >/dev/null || exit 1",
            "command -v rmdir >/dev/null || exit 1",
            "command -v basename >/dev/null || exit 1",
            "command -v find >/dev/null || exit 1",
            "command -v sort >/dev/null || exit 1",
            "command -v grep >/dev/null || exit 1",
            "command -v wc >/dev/null || exit 1",
            "command -v tr >/dev/null || exit 1",
            'command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }',
        ],
    )
    run_ssh_script(ssh, script)


def ensure_baseline(config: DeployConfig, ssh: SshConfig) -> None:
    baseline_dir = f"{config.remote.workdir}/baselines/{config.backup.baseline_id}"
    script = "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"baseline_dir={quote(baseline_dir)}",
            'if [ -f "$baseline_dir/baseline.json" ]; then exit 0; fi',
            'mkdir -p "$baseline_dir"',
            f"cd {quote(config.remote.root)}",
            list_file_script(config),
            f'tar {exclude_args(config)} -czf "$baseline_dir/baseline.tar.gz" \\',
            '  --files-from "$tmp_list"',
            "printf '%s\\n' '{\"schema_version\":1,\"files\":[]}' \\",
            '  > "$baseline_dir/baseline-manifest.json.tmp"',
            'mv "$baseline_dir/baseline-manifest.json.tmp" \\',
            '  "$baseline_dir/baseline-manifest.json"',
            "printf '%s\\n' '{\"schema_version\":1,\"protected\":true}' \\",
            '  > "$baseline_dir/baseline.json.tmp"',
            'mv "$baseline_dir/baseline.json.tmp" "$baseline_dir/baseline.json"',
        ],
    )
    run_ssh_script(ssh, script)


def existing_release_result(
    config: DeployConfig,
    ssh: SshConfig,
    release_id: str,
    artifact_sha256: str,
    archive: Path,
) -> ApplyResult | None:
    transactions = transactions_for_release(config, ssh, release_id)
    latest_successful = latest_successful_apply(config, ssh)
    if latest_successful is not None and latest_successful.release_id == release_id:
        if latest_successful.artifact_sha256 != artifact_sha256:
            raise DeployError(
                "The latest successful transaction uses this release id with another archive."
            )
        if latest_successful.status != "applied":
            raise DeployError("The matching release transaction is not in applied status.")
        return ApplyResult(
            transaction_id=latest_successful.transaction_id,
            release_id=latest_successful.release_id,
            created_files=latest_successful.summary.create,
            replaced_files=latest_successful.summary.replace,
            skipped_files=latest_successful.summary.skip,
            checkpoint_path=transaction_path(config, latest_successful.transaction_id),
            noop=True,
        )

    for previous in transactions:
        if previous.artifact_sha256 != artifact_sha256:
            raise DeployError("A previous transaction uses this release id with another archive.")
    recoverable = [
        transaction
        for transaction in sorted(transactions, key=lambda item: item.transaction_id, reverse=True)
        if transaction.status in {"preparing", "prepared", "applying", "failed"}
    ]
    if not recoverable:
        return None
    return recover_incomplete_apply(config, ssh, recoverable[0], archive)


@dataclass(frozen=True)
class ApplyRecoveryState:
    before: int
    deployed: int
    total: int


def recover_incomplete_apply(
    config: DeployConfig,
    ssh: SshConfig,
    transaction: TransactionRecord,
    archive: Path,
) -> ApplyResult:
    transaction_dir = transaction_path(config, transaction.transaction_id)
    plan = execution_plan_from_json(read_remote_file(ssh, f"{transaction_dir}/apply-plan.json"))
    validate_rollback_plan(config, transaction, plan)
    state = apply_recovery_state(config, ssh, transaction_dir, plan)

    if state.deployed > 0 and not remote_file_exists(ssh, f"{transaction_dir}/before.tar.gz"):
        raise DeployError(
            "Cannot recover partially applied transaction without before.tar.gz checkpoint."
        )
    if state.deployed == 0 and not remote_file_exists(ssh, f"{transaction_dir}/before.tar.gz"):
        run_ssh_script(ssh, apply_preflight_script(config, plan))
        run_ssh_script(ssh, checkpoint_script(config, transaction_dir, plan))
    validate_before_manifest(read_remote_file(ssh, f"{transaction_dir}/before-manifest.json"), plan)
    run_ssh_script(ssh, validate_before_checkpoint_script(config, transaction_dir, plan))
    ensure_remote_artifact(ssh, transaction_dir, transaction.artifact_sha256, archive)

    applying = with_status(transaction, "applying")
    upload_text(ssh, transaction_to_json(applying), f"{transaction_dir}/transaction.json")
    if state.before > 0:
        run_ssh_script(ssh, recovery_apply_script(config, transaction_dir, plan))
    applied = with_status(transaction, "applied")
    upload_text(ssh, transaction_to_json(applied), f"{transaction_dir}/transaction.json")
    update_pointer(ssh, config, "latest-successful-apply", transaction.transaction_id)
    update_pointer(ssh, config, "latest-transaction", transaction.transaction_id)
    apply_retention(config, ssh)

    return ApplyResult(
        transaction_id=transaction.transaction_id,
        release_id=transaction.release_id,
        created_files=transaction.summary.create,
        replaced_files=transaction.summary.replace,
        skipped_files=transaction.summary.skip,
        checkpoint_path=transaction_dir,
    )


def apply_recovery_state(
    config: DeployConfig,
    ssh: SshConfig,
    transaction_dir: str,
    plan: ExecutionPlan,
) -> ApplyRecoveryState:
    result = run_ssh_script(ssh, apply_recovery_state_script(config, transaction_dir, plan))
    payload = dict(
        item.split("=", 1) for item in result.stdout.decode("utf-8").strip().split() if "=" in item
    )
    return ApplyRecoveryState(
        before=int(payload.get("before", "0")),
        deployed=int(payload.get("deployed", "0")),
        total=int(payload.get("total", "0")),
    )


def latest_successful_apply(config: DeployConfig, ssh: SshConfig) -> TransactionRecord | None:
    pointer = f"{config.remote.workdir}/pointers/latest-successful-apply"
    result = run_ssh_script(
        ssh,
        f"set -eu\n[ ! -f {quote(pointer)} ] || cat {quote(pointer)}\n",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    transaction_id = result.stdout.decode("utf-8").strip()
    return transaction_from_json(
        read_remote_file(ssh, f"{transaction_path(config, transaction_id)}/transaction.json"),
    )


def transactions_for_release(
    config: DeployConfig,
    ssh: SshConfig,
    release_id: str,
) -> list[TransactionRecord]:
    script = "\n".join(
        [
            "set -eu",
            f"transactions={quote(config.remote.workdir + '/transactions')}",
            '[ -d "$transactions" ] || exit 0',
            'find "$transactions" -mindepth 2 -maxdepth 2 -name transaction.json | sort',
        ],
    )
    result = run_ssh_script(ssh, script)
    records: list[TransactionRecord] = []
    for remote_path in result.stdout.decode("utf-8").splitlines():
        if not remote_path:
            continue
        record = transaction_from_json(read_remote_file(ssh, remote_path))
        if record.release_id == release_id:
            records.append(record)
    return records


def create_transaction_dir_script(transaction_dir: str) -> str:
    return f"set -eu\nmkdir -p {quote(transaction_dir)}\n"


def checkpoint_script(config: DeployConfig, transaction_dir: str, plan: ExecutionPlan) -> str:
    files = [operation.path for operation in plan.operations if operation.op == "replace"]
    return "\n".join(
        [
            "set -eu",
            f"cd {quote(config.remote.root)}",
            'tmp_list="$(mktemp)"',
            'cleanup() { rm -f "$tmp_list"; }',
            "trap cleanup EXIT",
            *[f"printf '%s\\n' {quote(path)} >> \"$tmp_list\"" for path in files],
            f'tar -czf {quote(transaction_dir + "/before.tar.gz")} --files-from "$tmp_list"',
            f"tar -tzf {quote(transaction_dir + '/before.tar.gz')} >/dev/null",
        ],
    )


def rollback_checkpoint_script(
    config: DeployConfig,
    rollback_dir: str,
    plan: ExecutionPlan,
) -> str:
    files = [
        operation.path for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(
        [
            "set -eu",
            f"cd {quote(config.remote.root)}",
            'tmp_list="$(mktemp)"',
            'cleanup() { rm -f "$tmp_list"; }',
            "trap cleanup EXIT",
            *[f"printf '%s\\n' {quote(path)} >> \"$tmp_list\"" for path in files],
            f"tar -czf {quote(rollback_dir + '/before-rollback.tar.gz')} \\",
            '  --files-from "$tmp_list"',
            f"tar -tzf {quote(rollback_dir + '/before-rollback.tar.gz')} >/dev/null",
        ],
    )


def apply_script(config: DeployConfig, transaction_dir: str, plan: ExecutionPlan) -> str:
    operations = [
        operation for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"transaction_dir={quote(transaction_dir)}",
            'extract="$transaction_dir/extract"',
            'rm -rf "$extract"',
            'mkdir -p "$extract"',
            'tar -xzf "$transaction_dir/artifact.tar.gz" -C "$extract"',
            validate_artifact_extract_body(plan),
            apply_preflight_body(plan),
            *[apply_operation_line(operation) for operation in operations],
        ],
    )


def recovery_apply_script(config: DeployConfig, transaction_dir: str, plan: ExecutionPlan) -> str:
    operations = [
        operation for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"transaction_dir={quote(transaction_dir)}",
            'extract="$transaction_dir/extract"',
            'rm -rf "$extract"',
            'mkdir -p "$extract"',
            'tar -xzf "$transaction_dir/artifact.tar.gz" -C "$extract"',
            validate_artifact_extract_body(plan),
            *[safe_parent_line(operation.path) for operation in operations],
            *[recovery_apply_operation_line(operation) for operation in operations],
        ],
    )


def apply_recovery_state_script(
    config: DeployConfig,
    transaction_dir: str,
    plan: ExecutionPlan,
) -> str:
    operations = [
        operation for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"transaction_dir={quote(transaction_dir)}",
            '[ -f "$transaction_dir/apply-plan.json" ] '
            '|| { echo "missing apply-plan.json" >&2; exit 1; }',
            "before=0",
            "deployed=0",
            "total=0",
            *[safe_parent_line(operation.path) for operation in operations],
            *[apply_recovery_state_line(operation) for operation in operations],
            'printf "before=%s deployed=%s total=%s\\n" "$before" "$deployed" "$total"',
        ],
    )


def apply_preflight_script(config: DeployConfig, plan: ExecutionPlan) -> str:
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            apply_preflight_body(plan),
        ],
    )


def apply_preflight_body(plan: ExecutionPlan) -> str:
    operations = [
        operation for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(
        [
            *[safe_parent_line(operation.path) for operation in operations],
            *[apply_preflight_line(operation) for operation in operations],
        ]
    )


def validate_artifact_extract_body(plan: ExecutionPlan) -> str:
    operations = [
        operation for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(validate_artifact_file_line(operation) for operation in operations)


def validate_artifact_file_line(operation: ExecutionOperation) -> str:
    source = f'"$extract"/{quote(operation.path)}'
    return " ".join(
        [
            f"if [ -L {source} ] || [ ! -f {source} ]; then",
            f'echo "artifact file missing or unsafe: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'if [ "$(wc -c < {source} | tr -d " ")" != {quote(str(operation.size))} ]; then',
            f'echo "artifact file size mismatch: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'if [ "$(sha256sum {source} | cut -d" " -f1)" != {quote(operation.sha256)} ]; then',
            f'echo "artifact file checksum mismatch: {operation.path}" >&2;',
            "exit 1;",
            "fi",
        ],
    )


def apply_preflight_line(operation: ExecutionOperation) -> str:
    target = f'"$root"/{quote(operation.path)}'
    if operation.op == "create":
        return (
            f"if [ -e {target} ] || [ -L {target} ]; then "
            f'echo "remote path appeared before apply: {operation.path}" >&2; exit 1; fi'
        )
    if operation.op == "replace":
        expected = quote(require_remote_sha_before(operation))
        return " ".join(
            [
                f"if [ -L {target} ] || [ ! -f {target} ]; then",
                f'echo "remote path is no longer a regular file: {operation.path}" >&2;',
                "exit 1;",
                "fi;",
                f'if [ "$(sha256sum {target} | cut -d" " -f1)" != {expected} ]; then',
                f'echo "remote file changed before apply: {operation.path}" >&2;',
                "exit 1;",
                "fi",
            ],
        )
    raise DeployError(f"Unsupported apply preflight operation: {operation.op}")


def apply_operation_line(operation: ExecutionOperation) -> str:
    source = f'"$extract"/{quote(operation.path)}'
    target = f'"$root"/{quote(operation.path)}'
    expected = quote(operation.sha256)
    return "\n".join(
        [
            f'mkdir -p "$(dirname {target})";',
            safe_parent_line(operation.path),
            apply_preflight_line(operation),
            f"rm -f {target};",
            f"cp -p {source} {target};",
            safe_parent_line(operation.path),
            f"if [ -L {target} ] || [ ! -f {target} ]; then",
            f'echo "remote path is no longer a regular file: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'test "$(sha256sum {target} | cut -d" " -f1)" = {expected}',
        ],
    )


def recovery_apply_operation_line(operation: ExecutionOperation) -> str:
    source = f'"$extract"/{quote(operation.path)}'
    target = f'"$root"/{quote(operation.path)}'
    artifact_sha = quote(operation.sha256)
    if operation.op == "create":
        return "\n".join(
            [
                f"if [ ! -e {target} ] && [ ! -L {target} ]; then",
                f'mkdir -p "$(dirname {target})";',
                safe_parent_line(operation.path),
                f"cp -p {source} {target};",
                safe_parent_line(operation.path),
                f"elif [ -L {target} ] || [ ! -f {target} ]; then",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                f'elif [ "$(sha256sum {target} | cut -d" " -f1)" != {artifact_sha} ]; then',
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                "fi;",
                f'test "$(sha256sum {target} | cut -d" " -f1)" = {artifact_sha}',
            ],
        )
    if operation.op == "replace":
        before_sha = quote(require_remote_sha_before(operation))
        return "\n".join(
            [
                f"if [ -L {target} ] || [ ! -f {target} ]; then",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                "fi;",
                f'current_sha="$(sha256sum {target} | cut -d" " -f1)";',
                f'if [ "$current_sha" = {before_sha} ]; then',
                safe_parent_line(operation.path),
                f"rm -f {target};",
                f"cp -p {source} {target};",
                safe_parent_line(operation.path),
                f'elif [ "$current_sha" = {artifact_sha} ]; then',
                ":;",
                "else",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                "fi;",
                f'test "$(sha256sum {target} | cut -d" " -f1)" = {artifact_sha}',
            ],
        )
    raise DeployError(f"Unsupported recovery apply operation: {operation.op}")


def apply_recovery_state_line(operation: ExecutionOperation) -> str:
    target = f'"$root"/{quote(operation.path)}'
    artifact_sha = quote(operation.sha256)
    if operation.op == "create":
        return " ".join(
            [
                "total=$((total + 1));",
                f"if [ ! -e {target} ] && [ ! -L {target} ]; then",
                "before=$((before + 1));",
                f"elif [ -L {target} ] || [ ! -f {target} ]; then",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                f'elif [ "$(sha256sum {target} | cut -d" " -f1)" = {artifact_sha} ]; then',
                "deployed=$((deployed + 1));",
                "else",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                "fi",
            ],
        )
    if operation.op == "replace":
        before_sha = quote(require_remote_sha_before(operation))
        return " ".join(
            [
                "total=$((total + 1));",
                f"if [ -L {target} ] || [ ! -f {target} ]; then",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                "fi;",
                f'current_sha="$(sha256sum {target} | cut -d" " -f1)";',
                f'if [ "$current_sha" = {before_sha} ]; then',
                "before=$((before + 1));",
                f'elif [ "$current_sha" = {artifact_sha} ]; then',
                "deployed=$((deployed + 1));",
                "else",
                f'echo "remote path is not recoverable: {operation.path}" >&2;',
                "exit 1;",
                "fi",
            ],
        )
    raise DeployError(f"Unsupported recovery state operation: {operation.op}")


def rollback_script(config: DeployConfig, target_dir: str, plan: ExecutionPlan) -> str:
    created = [operation for operation in plan.operations if operation.op == "create"]
    replaced = [operation for operation in plan.operations if operation.op == "replace"]
    directories = sorted(plan.created_directories, key=lambda item: item.count("/"), reverse=True)
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"target_dir={quote(target_dir)}",
            before_checkpoint_validation_body(plan),
            rollback_preflight_body(plan),
            *[restore_replaced_file_line(operation) for operation in replaced],
            *[delete_created_file_line(operation) for operation in created],
            *[f'rmdir "$root"/{quote(directory)} 2>/dev/null || true' for directory in directories],
        ],
    )


def validate_before_checkpoint_script(
    config: DeployConfig,
    transaction_dir: str,
    plan: ExecutionPlan,
) -> str:
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            f"target_dir={quote(transaction_dir)}",
            before_checkpoint_validation_body(plan),
        ],
    )


def before_checkpoint_validation_body(plan: ExecutionPlan) -> str:
    replaced = [operation for operation in plan.operations if operation.op == "replace"]
    return "\n".join(
        [
            'checkpoint_extract="$(mktemp -d)"',
            'expected_list="$(mktemp)"',
            'actual_list="$(mktemp)"',
            'cleanup() { rm -rf "$checkpoint_extract"; rm -f "$expected_list" "$actual_list"; }',
            "trap cleanup EXIT",
            ': > "$expected_list"',
            *[
                f"printf '%s\\n' {quote(operation.path)} >> \"$expected_list\""
                for operation in replaced
            ],
            'sort "$expected_list" -o "$expected_list"',
            'tar -tzf "$target_dir/before.tar.gz" | sort > "$actual_list"',
            'if [ "$(cat "$expected_list")" != "$(cat "$actual_list")" ]; then',
            '  echo "before checkpoint file list mismatch" >&2;',
            "  exit 1;",
            "fi",
            'tar -xzf "$target_dir/before.tar.gz" -C "$checkpoint_extract"',
            *[validate_checkpoint_file_line(operation) for operation in replaced],
        ],
    )


def validate_checkpoint_file_line(operation: ExecutionOperation) -> str:
    source = f'"$checkpoint_extract"/{quote(operation.path)}'
    return " ".join(
        [
            f"if [ -L {source} ] || [ ! -f {source} ]; then",
            f'echo "before checkpoint file missing or unsafe: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'if [ "$(wc -c < {source} | tr -d " ")" != '
            f"{quote(str(require_remote_size_before(operation)))} ]; then",
            f'echo "before checkpoint file size mismatch: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'if [ "$(sha256sum {source} | cut -d" " -f1)" != '
            f"{quote(require_remote_sha_before(operation))} ]; then",
            f'echo "before checkpoint file checksum mismatch: {operation.path}" >&2;',
            "exit 1;",
            "fi",
        ],
    )


def rollback_preflight_script(config: DeployConfig, plan: ExecutionPlan) -> str:
    return "\n".join(
        [
            "set -eu",
            f"root={quote(config.remote.root)}",
            rollback_preflight_body(plan),
        ],
    )


def rollback_preflight_body(plan: ExecutionPlan) -> str:
    operations = [
        operation for operation in plan.operations if operation.op in ("create", "replace")
    ]
    return "\n".join(
        [
            *[safe_parent_line(operation.path) for operation in operations],
            *[rollback_preflight_line(operation) for operation in operations],
        ]
    )


def rollback_preflight_line(operation: ExecutionOperation) -> str:
    target = f'"$root"/{quote(operation.path)}'
    expected = quote(operation.sha256)
    if operation.op == "create":
        message = "created file changed before rollback"
    elif operation.op == "replace":
        message = "replaced file changed before rollback"
    else:
        raise DeployError(f"Unsupported rollback preflight operation: {operation.op}")
    return " ".join(
        [
            f"if [ -L {target} ] || [ ! -f {target} ]; then",
            f'echo "{message}: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'if [ "$(sha256sum {target} | cut -d" " -f1)" != {expected} ]; then',
            f'echo "{message}: {operation.path}" >&2;',
            "exit 1;",
            "fi",
        ],
    )


def validate_rollback_preconditions(plan: ExecutionPlan, current_root: Path) -> None:
    for operation in plan.operations:
        current = current_root / operation.path
        if operation.op == "create":
            if not current.is_file():
                raise DeployError(f"Cannot rollback missing created file: {operation.path}")
            if sha256_file(current) != operation.sha256:
                raise DeployError(f"Cannot rollback modified created file: {operation.path}")
        if operation.op == "replace":
            if not current.is_file():
                raise DeployError(f"Cannot rollback missing replaced file: {operation.path}")
            if sha256_file(current) != operation.sha256:
                raise DeployError(f"Cannot rollback modified replaced file: {operation.path}")


def delete_created_file_line(operation: ExecutionOperation) -> str:
    target = f'"$root"/{quote(operation.path)}'
    return "\n".join(
        [
            safe_parent_line(operation.path),
            rollback_preflight_line(operation),
            f"rm -f {target};",
        ],
    )


def restore_replaced_file_line(operation: ExecutionOperation) -> str:
    source = f'"$checkpoint_extract"/{quote(operation.path)}'
    target = f'"$root"/{quote(operation.path)}'
    return "\n".join(
        [
            safe_parent_line(operation.path),
            rollback_preflight_line(operation),
            f"rm -f {target};",
            f"cp -p {source} {target};",
            f"if [ -L {target} ] || [ ! -f {target} ]; then",
            f'echo "replaced file changed before rollback: {operation.path}" >&2;',
            "exit 1;",
            "fi;",
            f'test "$(sha256sum {target} | cut -d" " -f1)" = '
            f"{quote(require_remote_sha_before(operation))}",
        ],
    )


def safe_parent_line(path: str) -> str:
    parent = str(Path(path).parent)
    if parent == ".":
        return ":;"
    return " ".join(
        [
            'current="$root";',
            f"relative_parent={quote(parent)};",
            'old_ifs="$IFS";',
            "IFS=/;",
            "for part in $relative_parent; do",
            '  IFS="$old_ifs";',
            '  current="$current/$part";',
            '  if [ -L "$current" ]; then',
            f'    echo "remote parent is a symlink: {path}" >&2;',
            "    exit 1;",
            "  fi;",
            '  if [ -e "$current" ] && [ ! -d "$current" ]; then',
            f'    echo "remote parent is not a directory: {path}" >&2;',
            "    exit 1;",
            "  fi;",
            "  IFS=/;",
            "done;",
            'IFS="$old_ifs"',
        ],
    )


def update_pointer(ssh: SshConfig, config: DeployConfig, name: str, value: str) -> None:
    upload_text(ssh, f"{value}\n", f"{config.remote.workdir}/pointers/{name}")


def remove_pointer(ssh: SshConfig, config: DeployConfig, name: str) -> None:
    run_ssh_script(ssh, f"set -eu\nrm -f {quote(config.remote.workdir + '/pointers/' + name)}\n")


def apply_retention(config: DeployConfig, ssh: SshConfig) -> None:
    script = "\n".join(
        [
            "set -eu",
            f"workdir={quote(config.remote.workdir)}",
            f"transactions={quote(config.remote.workdir + '/transactions')}",
            f"retention={config.backup.retention}",
            '[ -d "$transactions" ] || exit 0',
            'latest_successful_pointer="$workdir/pointers/latest-successful-apply"',
            'latest_transaction_pointer="$workdir/pointers/latest-transaction"',
            'latest_successful="$(cat "$latest_successful_pointer" 2>/dev/null || true)"',
            'latest_transaction="$(cat "$latest_transaction_pointer" 2>/dev/null || true)"',
            'applied_status=\'"status": "applied"\'',
            'rolled_back_status=\'"status": "rolled_back"\'',
            "kept=0",
            'find "$transactions" -mindepth 1 -maxdepth 1 -type d \\',
            "  | sort -r \\",
            "  | while read -r dir; do",
            '  id="$(basename "$dir")"',
            '  [ "$id" = "$latest_successful" ] && continue',
            '  [ "$id" = "$latest_transaction" ] && continue',
            '  if [ "$kept" -lt "$retention" ]; then',
            "    kept=$((kept + 1))",
            "    continue",
            "  fi",
            '  [ -f "$dir/transaction.json" ] || continue',
            '  if grep -F "$applied_status" "$dir/transaction.json" >/dev/null 2>&1; then',
            '    rm -rf "$dir"',
            '  elif grep -F "$rolled_back_status" "$dir/transaction.json" >/dev/null 2>&1; then',
            '    rm -rf "$dir"',
            "  fi",
            "done",
        ],
    )
    run_ssh_script(ssh, script)


def upload_text(ssh: SshConfig, content: str, remote_path: str) -> None:
    run_upload(ssh, content.encode("utf-8"), remote_path)


def upload_file(ssh: SshConfig, local_path: Path, remote_path: str) -> None:
    run_upload(ssh, local_path.read_bytes(), remote_path)


def run_upload(ssh: SshConfig, content: bytes, remote_path: str) -> None:
    remote_parent = remote_path.rsplit("/", 1)[0]
    command = [
        *ssh_command(ssh),
        (
            "set -eu; "
            f"mkdir -p {quote(remote_parent)}; "
            f"tmp={quote(remote_path)}.$$.tmp; "
            'cat > "$tmp"; '
            f'mv "$tmp" {quote(remote_path)}'
        ),
    ]
    result = subprocess.run(command, input=content, capture_output=True, check=False)
    if result.returncode != 0:
        raise DeployError(result.stderr.decode("utf-8", errors="replace").strip())


def read_remote_file(ssh: SshConfig, remote_path: str) -> str:
    result = run_ssh_script(ssh, f"set -eu\ncat {quote(remote_path)}\n")
    return result.stdout.decode("utf-8")


def remote_file_exists(ssh: SshConfig, remote_path: str) -> bool:
    result = run_ssh_script(ssh, f"set -eu\n[ -f {quote(remote_path)} ]\n", check=False)
    return result.returncode == 0


def remote_file_sha256(ssh: SshConfig, remote_path: str) -> str | None:
    remote = quote(remote_path)
    result = run_ssh_script(
        ssh,
        f'set -eu\n[ ! -f {remote} ] || sha256sum {remote} | cut -d" " -f1\n',
        check=False,
    )
    if result.returncode != 0:
        return None
    digest = result.stdout.decode("utf-8").strip()
    return digest or None


def ensure_remote_artifact(
    ssh: SshConfig,
    transaction_dir: str,
    expected_sha256: str,
    archive: Path,
) -> None:
    if sha256_file(archive) != expected_sha256:
        raise DeployError("Local archive checksum does not match transaction.")
    remote_path = f"{transaction_dir}/artifact.tar.gz"
    if remote_file_sha256(ssh, remote_path) != expected_sha256:
        upload_file(ssh, archive, remote_path)
    if remote_file_sha256(ssh, remote_path) != expected_sha256:
        raise DeployError("Remote artifact checksum does not match transaction.")


def run_ssh_script(
    ssh: SshConfig, script: str, *, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [*ssh_command(ssh), "sh -s"],
        input=(script + "\n").encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise DeployError(result.stderr.decode("utf-8", errors="replace").strip())
    return result


def list_file_script(config: DeployConfig) -> str:
    lines = [
        'tmp_list="$(mktemp)"',
        'cleanup() { rm -f "$tmp_list"; }',
        "trap cleanup EXIT",
    ]
    for path in remote_include_paths(config):
        lines.append(f"[ ! -e {quote(path)} ] || printf '%s\\n' {quote(path)} >> \"$tmp_list\"")
    return "\n".join(lines)


def exclude_args(config: DeployConfig) -> str:
    parts: list[str] = []
    for pattern in remote_exclude_patterns(config):
        parts.extend(["--exclude", quote(pattern)])
    return " ".join(parts)


def transaction_path(config: DeployConfig, transaction_id: str) -> str:
    return f"{config.remote.workdir}/transactions/{transaction_id}"


def make_transaction_id(release_id: str, artifact_sha256: str, mode: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_release = "".join(
        character if character.isalnum() or character in "._-" else "-" for character in release_id
    )
    return f"{timestamp}-{safe_release}-{artifact_sha256[:12]}-{mode}"


def require_manifest_str(manifest: dict[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise DeployError(f"Archive manifest {key} must be a non-empty string.")
    return value


def require_operation_sha(operation: ExecutionOperation) -> str:
    if not operation.sha256:
        raise DeployError(f"Missing operation checksum: {operation.path}")
    return operation.sha256


def require_remote_sha_before(operation: ExecutionOperation) -> str:
    if operation.remote_sha256_before is None:
        raise DeployError(f"Missing previous checksum for rollback: {operation.path}")
    return operation.remote_sha256_before


def require_remote_size_before(operation: ExecutionOperation) -> int:
    if operation.remote_size_before is None:
        raise DeployError(f"Missing previous size for rollback: {operation.path}")
    return operation.remote_size_before


def print_apply_result(result: ApplyResult) -> None:
    suffix = " (no-op)" if result.noop else ""
    print(f"Applied release: {result.release_id}{suffix}")
    print(f"Transaction id: {result.transaction_id}")
    print(f"Checkpoint: {result.checkpoint_path}")
    print(f"Created files: {result.created_files}")
    print(f"Replaced files: {result.replaced_files}")
    print(f"Skipped files: {result.skipped_files}")
    print("Remote-only files were preserved by the overlay strategy.")


def print_rollback_result(result: RollbackResult) -> None:
    print(f"Rolled back release: {result.release_id}")
    print(f"Rollback transaction id: {result.transaction_id}")
    print(f"Target transaction id: {result.target_transaction_id}")
    print(f"Checkpoint: {result.checkpoint_path}")
    print(f"Restored files: {result.restored_files}")
    print(f"Deleted files: {result.deleted_files}")
