from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shlex import quote

from ssh_archive_deploy.archive import extract_archive, read_manifest_from_archive, validate_archive
from ssh_archive_deploy.config import DeployConfig, ScopeConfig
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.planner import (
    ApplyPlan,
    ArtifactSnapshot,
    RemoteSnapshot,
    ScopePlan,
    TextChange,
    compute_plan,
)

REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScopeSummary:
    name: str
    changed_files: int
    artifact_only_files: int
    remote_only_files: int
    text_changed_files: int
    binary_changed_files: int
    insertions: int
    deletions: int


@dataclass(frozen=True)
class ReportSummary:
    project: str
    release_id: str
    scopes: list[ScopeSummary]


@dataclass(frozen=True)
class SshConfig:
    host: str | None
    user: str | None
    port: str
    alias: str | None
    private_key_file: str | None
    known_hosts_file: str | None


def generate_report(
    config: DeployConfig,
    archive: Path,
    output_dir: Path,
    ssh: SshConfig,
) -> ReportSummary:
    validate_archive(archive, config)
    manifest = read_manifest_from_archive(archive)
    release_id = str(manifest["release_id"])

    with tempfile.TemporaryDirectory(prefix="ssh-archive-deploy-report-") as tmp:
        tmp_path = Path(tmp)
        current_root = tmp_path / "current"
        new_root = tmp_path / "new"
        current_root.mkdir()
        new_root.mkdir()

        extract_archive(archive, new_root)
        fetch_remote_archive(config, current_root, ssh)
        summary = write_report(config, new_root, current_root, output_dir, release_id)
        print_report(summary, output_dir)
        return summary


def write_report(
    config: DeployConfig,
    new_root: Path,
    current_root: Path,
    output_dir: Path,
    release_id: str,
) -> ReportSummary:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    plan = compute_plan(
        config,
        ArtifactSnapshot(new_root),
        RemoteSnapshot(current_root),
        release_id,
    )
    scope_summaries = write_plan_report(config, plan, output_dir)
    summary = ReportSummary(config.project, release_id, scope_summaries)
    (output_dir / "report.json").write_text(
        json.dumps(serialize_report(config, plan), indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def write_plan_report(
    config: DeployConfig,
    plan: ApplyPlan,
    output_dir: Path,
) -> list[ScopeSummary]:
    return [
        write_scope_report(scope, plan_scope, output_dir / scope.name)
        for scope, plan_scope in zip(config.scopes, plan.scopes, strict=True)
    ]


def write_scope_report(
    scope: ScopeConfig,
    plan_scope: ScopePlan,
    scope_dir: Path,
) -> ScopeSummary:
    scope_dir.mkdir()
    text_changed = [change.path for change in plan_scope.text_changes]
    line_counts = [
        f"{change.path}\t+{change.insertions}\t-{change.deletions}"
        for change in plan_scope.text_changes
    ]
    insertions = sum(change.insertions for change in plan_scope.text_changes)
    deletions = sum(change.deletions for change in plan_scope.text_changes)

    write_lines(scope_dir / "changed-files.txt", plan_scope.changed_files)
    write_lines(scope_dir / "changed-text-files.txt", text_changed)
    write_lines(scope_dir / "changed-binary-files.txt", plan_scope.binary_changed_files)
    write_lines(scope_dir / "artifact-only-files.txt", plan_scope.artifact_only_files)
    write_lines(scope_dir / "remote-only-files.txt", plan_scope.remote_only_files)
    write_lines(scope_dir / "text-line-changes.tsv", line_counts)

    return ScopeSummary(
        name=scope.name,
        changed_files=len(plan_scope.changed_files),
        artifact_only_files=len(plan_scope.artifact_only_files),
        remote_only_files=len(plan_scope.remote_only_files),
        text_changed_files=len(text_changed),
        binary_changed_files=len(plan_scope.binary_changed_files),
        insertions=insertions,
        deletions=deletions,
    )


def fetch_remote_archive(config: DeployConfig, destination: Path, ssh: SshConfig) -> None:
    command = ssh_command(ssh)
    remote_script = remote_tar_script(config)
    result = subprocess.run(
        [*command, "sh -s"],
        input=remote_script,
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0:
        raise DeployError(result.stderr.decode("utf-8", errors="replace").strip())

    archive_path = destination.parent / "remote.tar.gz"
    archive_path.write_bytes(result.stdout)
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise DeployError(f"Unsafe path from remote archive: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise DeployError(f"Unsupported remote archive entry type: {member.name}")
        tar.extractall(destination, filter="data")


def remote_tar_script(config: DeployConfig) -> bytes:
    includes = remote_include_paths(config)
    excludes = remote_exclude_patterns(config)
    lines = [
        "set -eu",
        f"cd {quote(config.remote.root)}",
        'tmp_list="$(mktemp)"',
        'cleanup() { rm -f "$tmp_list"; }',
        "trap cleanup EXIT",
    ]
    for path in includes:
        lines.append(f"[ ! -e {quote(path)} ] || printf '%s\\n' {quote(path)} >> \"$tmp_list\"")
    tar_parts = ["tar", "-czf", "-"]
    for pattern in excludes:
        tar_parts.extend(["--exclude", quote(pattern)])
    tar_parts.extend(["--files-from", '"$tmp_list"'])
    lines.append(" ".join(tar_parts))
    return ("\n".join(lines) + "\n").encode("utf-8")


def remote_include_paths(config: DeployConfig) -> list[str]:
    paths: set[str] = set()
    for scope in config.scopes:
        if not scope.include:
            paths.add(scope.target)
            continue
        for pattern in scope.include:
            root = pattern.removesuffix("/**")
            if "*" in root or "?" in root or "[" in root:
                continue
            if scope.target == ".":
                paths.add(root)
            else:
                paths.add(f"{scope.target}/{root}")
    return sorted("." if path == "" else path for path in paths)


def remote_exclude_patterns(config: DeployConfig) -> list[str]:
    patterns = set(config.exclude)
    for scope in config.scopes:
        for pattern in scope.exclude:
            patterns.add(pattern if scope.target == "." else f"{scope.target}/{pattern}")
    return sorted(patterns)


def ssh_command(ssh: SshConfig) -> list[str]:
    target = ssh.alias
    if target is None:
        if not ssh.host or not ssh.user:
            raise DeployError("SSH host/user or SSH alias is required for report mode.")
        target = f"{ssh.user}@{ssh.host}"

    command = [
        "ssh",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        "PasswordAuthentication=no",
    ]
    if ssh.known_hosts_file:
        command.extend(
            [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={ssh.known_hosts_file}",
            ],
        )
    else:
        command.extend(["-o", "StrictHostKeyChecking=accept-new"])
    if ssh.private_key_file:
        command.extend(["-o", "IdentitiesOnly=yes", "-i", ssh.private_key_file])
    if ssh.port:
        command.extend(["-p", ssh.port])
    command.append(target)
    return command


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def serialize_report(config: DeployConfig, plan: ApplyPlan) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "project": plan.project,
        "release_id": plan.release_id,
        "strategy": {
            "apply": config.strategy.apply,
            "delete_unknown": config.strategy.delete_unknown,
        },
        "summary": serialize_summary(plan.scopes),
        "scopes": [
            serialize_scope_report(scope, plan_scope)
            for scope, plan_scope in zip(config.scopes, plan.scopes, strict=True)
        ],
    }


def serialize_summary(scopes: list[ScopePlan]) -> dict[str, int]:
    return {
        "changed_files": sum(len(scope.changed_files) for scope in scopes),
        "artifact_only_files": sum(len(scope.artifact_only_files) for scope in scopes),
        "remote_only_files": sum(len(scope.remote_only_files) for scope in scopes),
        "text_changed_files": sum(len(scope.text_changes) for scope in scopes),
        "binary_changed_files": sum(len(scope.binary_changed_files) for scope in scopes),
        "insertions": sum(change.insertions for scope in scopes for change in scope.text_changes),
        "deletions": sum(change.deletions for scope in scopes for change in scope.text_changes),
    }


def serialize_scope_report(scope: ScopeConfig, plan_scope: ScopePlan) -> dict[str, object]:
    return {
        "name": scope.name,
        "target": scope.target,
        "summary": serialize_summary([plan_scope]),
        "changed_files": root_relative_paths(scope, plan_scope.changed_files),
        "changed_text_files": root_relative_paths(
            scope,
            [change.path for change in plan_scope.text_changes],
        ),
        "changed_binary_files": root_relative_paths(scope, plan_scope.binary_changed_files),
        "artifact_only_files": root_relative_paths(scope, plan_scope.artifact_only_files),
        "remote_only_files": root_relative_paths(scope, plan_scope.remote_only_files),
        "text_line_changes": [
            serialize_text_change(scope, change) for change in plan_scope.text_changes
        ],
    }


def serialize_text_change(scope: ScopeConfig, change: TextChange) -> dict[str, object]:
    return {
        "path": root_relative_path(scope, change.path),
        "insertions": change.insertions,
        "deletions": change.deletions,
    }


def root_relative_paths(scope: ScopeConfig, paths: list[str]) -> list[str]:
    return [root_relative_path(scope, path) for path in paths]


def root_relative_path(scope: ScopeConfig, path: str) -> str:
    if scope.target == ".":
        return path
    return f"{scope.target}/{path}"


def print_report(summary: ReportSummary, output_dir: Path) -> None:
    total_changed = sum(scope.changed_files for scope in summary.scopes)
    total_artifact_only = sum(scope.artifact_only_files for scope in summary.scopes)
    total_remote_only = sum(scope.remote_only_files for scope in summary.scopes)
    total_insertions = sum(scope.insertions for scope in summary.scopes)
    total_deletions = sum(scope.deletions for scope in summary.scopes)

    print(f"Deployment report for {summary.project}")
    print(f"Release id: {summary.release_id}")
    print("")
    print("Summary:")
    print(f"- changed files: {total_changed}")
    print(f"- files only in artifact: {total_artifact_only}")
    print(f"- remote-only files kept as drift: {total_remote_only}")
    print(f"- text insertions: {total_insertions}")
    print(f"- text deletions: {total_deletions}")
    print("")

    for scope in summary.scopes:
        print(f"Scope: {scope.name}")
        print(f"- changed files: {scope.changed_files}")
        print(f"- files only in artifact: {scope.artifact_only_files}")
        print(f"- remote-only files kept as drift: {scope.remote_only_files}")
        print(f"- text changed files: {scope.text_changed_files}")
        print(f"- binary changed files: {scope.binary_changed_files}")
        print(f"- text insertions: {scope.insertions}")
        print(f"- text deletions: {scope.deletions}")
        print("")

    print(f"Detailed report written to {output_dir}")
    print("Note: the configured overlay strategy keeps remote-only files in place.")
