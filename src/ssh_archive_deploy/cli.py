from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ssh_archive_deploy.archive import build_archive, validate_archive
from ssh_archive_deploy.config import load_config
from ssh_archive_deploy.deploy import (
    apply_deployment,
    print_apply_result,
    print_rollback_result,
    rollback_deployment,
)
from ssh_archive_deploy.doctor import render_doctor_github_summary, run_doctor
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.report import SshConfig, generate_report
from ssh_archive_deploy.summary import render_github_summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except DeployError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssh-archive-deploy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build a scoped deployment archive.")
    build.add_argument("--config", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--release-id")

    validate = subparsers.add_parser("validate", help="Validate a deployment archive.")
    validate.add_argument("--config", required=True)
    validate.add_argument("--archive", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect remote capabilities read-only.")
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--target-name", required=True)
    doctor.add_argument("--output", default="dist/deploy-doctor/doctor.json")
    add_ssh_arguments(doctor, allow_host_key_discovery=True)

    report = subparsers.add_parser("report", help="Generate a read-only deployment report.")
    report.add_argument("--config", required=True)
    report.add_argument("--archive", required=True)
    report.add_argument("--output-dir", default="dist/deploy-report")
    add_ssh_arguments(report, allow_host_key_discovery=True)

    summarize_doctor = subparsers.add_parser("summarize-doctor", help="Render a doctor summary.")
    summarize_doctor.add_argument("--report", required=True)
    summarize_doctor.add_argument("--format", choices=["github"], default="github")

    summarize = subparsers.add_parser("summarize-report", help="Render a report summary.")
    summarize.add_argument("--report", required=True)
    summarize.add_argument("--format", choices=["github"], default="github")

    apply = subparsers.add_parser("apply", help="Apply a deployment archive.")
    apply.add_argument("--config", required=True)
    apply.add_argument("--archive", required=True)
    apply.add_argument("--result-json")
    add_ssh_arguments(apply)

    rollback = subparsers.add_parser("rollback", help="Rollback a deployment.")
    rollback.add_argument("--config", required=True)
    rollback.add_argument("--release", default="latest")
    rollback.add_argument("--result-json")
    add_ssh_arguments(rollback)

    return parser


def add_ssh_arguments(
    parser: argparse.ArgumentParser,
    *,
    allow_host_key_discovery: bool = False,
) -> None:
    parser.add_argument("--ssh-host", default=os.getenv("SSH_HOST"))
    parser.add_argument("--ssh-user", default=os.getenv("SSH_USER"))
    parser.add_argument("--ssh-port", default=os.getenv("SSH_PORT", "22"))
    parser.add_argument("--ssh-alias", default=os.getenv("SSH_ALIAS"))
    parser.add_argument("--ssh-private-key-file", default=os.getenv("SSH_PRIVATE_KEY_FILE"))
    parser.add_argument("--ssh-known-hosts-file", default=os.getenv("SSH_KNOWN_HOSTS_FILE"))
    if allow_host_key_discovery:
        parser.add_argument("--allow-host-key-discovery", action="store_true")


def run(args: argparse.Namespace) -> int:
    if args.command == "build":
        config = load_config(args.config)
        manifest = build_archive(config, Path(args.output), args.release_id)
        print(f"Built deployment artifact: {args.output}")
        print(f"Release id: {manifest.release_id}")
        return 0

    if args.command == "validate":
        config = load_config(args.config)
        validate_archive(Path(args.archive), config)
        print(f"Archive is valid: {args.archive}")
        return 0

    if args.command in {"doctor", "report"}:
        return run_read_only(args)

    if args.command in {"summarize-doctor", "summarize-report"}:
        if args.command == "summarize-doctor":
            print(render_doctor_github_summary(Path(args.report)), end="")
        else:
            print(render_github_summary(Path(args.report)), end="")
        return 0

    if args.command == "apply":
        config = load_config(args.config)
        result = apply_deployment(
            config,
            Path(args.archive),
            SshConfig(
                host=args.ssh_host,
                user=args.ssh_user,
                port=args.ssh_port,
                alias=args.ssh_alias,
                private_key_file=args.ssh_private_key_file,
                known_hosts_file=args.ssh_known_hosts_file,
            ),
        )
        print_apply_result(result)
        if args.result_json:
            write_json(
                Path(args.result_json),
                {
                    "mode": "apply",
                    "release_id": result.release_id,
                    "transaction_id": result.transaction_id,
                    "checkpoint_path": result.checkpoint_path,
                    "rollback_command": "ssh-archive-deploy rollback --release latest",
                    "noop": result.noop,
                },
            )
        return 0

    if args.command == "rollback":
        config = load_config(args.config)
        result = rollback_deployment(
            config,
            args.release,
            SshConfig(
                host=args.ssh_host,
                user=args.ssh_user,
                port=args.ssh_port,
                alias=args.ssh_alias,
                private_key_file=args.ssh_private_key_file,
                known_hosts_file=args.ssh_known_hosts_file,
            ),
        )
        print_rollback_result(result)
        if args.result_json:
            write_json(
                Path(args.result_json),
                {
                    "mode": "rollback",
                    "release_id": result.release_id,
                    "transaction_id": result.transaction_id,
                    "target_transaction_id": result.target_transaction_id,
                    "checkpoint_path": result.checkpoint_path,
                },
            )
        return 0

    raise DeployError(f"Unsupported command: {args.command}")


def run_read_only(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ssh = SshConfig(
        host=args.ssh_host,
        user=args.ssh_user,
        port=args.ssh_port,
        alias=args.ssh_alias,
        private_key_file=args.ssh_private_key_file,
        known_hosts_file=args.ssh_known_hosts_file,
        allow_host_key_discovery=args.allow_host_key_discovery,
    )
    if args.command == "doctor":
        run_doctor(config, args.target_name, Path(args.output), ssh)
        return 0
    if args.command == "report":
        generate_report(config, Path(args.archive), Path(args.output_dir), ssh)
        return 0
    raise DeployError(f"Unsupported read-only command: {args.command}")


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
