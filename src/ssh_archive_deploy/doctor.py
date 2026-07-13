from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shlex import quote
from typing import Any, Literal

from ssh_archive_deploy.config import DeployConfig
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.report import SshConfig, ssh_command

DOCTOR_SCHEMA_VERSION = 1
COMMAND_RECORD_FIELDS = 3
TAR_VERSION_MINIMUM_FIELDS = 2
PATH_RECORD_FIELDS = 6
CONTROL_CHARACTER_LIMIT = 32
DELETE_CHARACTER = 127

REQUIRED_COMMANDS = ("sh", "tar", "cp", "mkdir", "rm")
OPTIONAL_COMMANDS = (
    "composer",
    "find",
    "flock",
    "git",
    "jq",
    "php",
    "realpath",
    "rsync",
    "sha256sum",
    "sort",
    "wp",
)
REQUIRED_TAR_OPTIONS = ("--create", "--extract", "--file", "--directory", "--gzip")

# Compatibility is deliberately limited to versions exercised by the SSH E2E suite.
TESTED_TAR_VERSIONS = frozenset({("gnu", "1.34")})

Compatibility = Literal["compatible", "incompatible", "undetermined"]
Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class PathObservation:
    exists: bool
    directory: bool
    readable: bool
    writable_hint: bool


@dataclass(frozen=True)
class RemoteObservations:
    commands: dict[str, bool]
    tar_version_line: str | None
    tar_options: dict[str, bool]
    root: PathObservation
    workdir: PathObservation


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class TarCapability:
    available: bool
    implementation: str | None
    version: str | None
    version_status: Literal["tested", "untested", "unavailable"]
    options: dict[str, bool]
    compatibility: Compatibility

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "implementation": self.implementation,
            "version": self.version,
            "version_status": self.version_status,
            "options": self.options,
            "compatibility": self.compatibility,
        }


@dataclass(frozen=True)
class DoctorResult:
    checked_at: str
    project: str
    target_name: str
    host_key_policy: Literal["strict", "discovery"]
    compatibility: Compatibility
    commands: dict[str, bool]
    tar: TarCapability
    root: PathObservation
    workdir: PathObservation
    diagnostics: list[Diagnostic]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "operation": "doctor",
            "checked_at": self.checked_at,
            "project": self.project,
            "target": {"name": self.target_name},
            "ssh": {"host_key_policy": self.host_key_policy},
            "compatibility": self.compatibility,
            "commands": {
                "required": [
                    {"name": name, "available": self.commands[name]} for name in REQUIRED_COMMANDS
                ],
                "optional": [
                    {"name": name, "available": self.commands[name]} for name in OPTIONAL_COMMANDS
                ],
                "absent": [name for name, available in self.commands.items() if not available],
            },
            "tar": self.tar.to_dict(),
            "remote": {
                "root": serialize_path_observation(self.root),
                "workdir": serialize_path_observation(self.workdir),
            },
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def run_doctor(
    config: DeployConfig,
    target_name: str,
    output: Path,
    ssh: SshConfig,
    *,
    checked_at: datetime | None = None,
) -> DoctorResult:
    validate_target_name(target_name)
    observations = inspect_remote(config, ssh)
    result = evaluate_doctor(
        config,
        target_name,
        observations,
        ssh,
        checked_at=checked_at,
    )
    write_doctor_result(output, result)
    print_doctor_result(result, output)
    return result


def inspect_remote(config: DeployConfig, ssh: SshConfig) -> RemoteObservations:
    result = subprocess.run(
        [*ssh_command(ssh), "sh -s"],
        input=remote_doctor_script(config),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DeployError(f"Remote capability inspection failed (SSH exit {result.returncode}).")
    return parse_remote_observations(result.stdout)


def remote_doctor_script(config: DeployConfig) -> str:
    lines = [
        "set -u",
        "printf 'protocol\\t1\\n'",
    ]
    for command in (*REQUIRED_COMMANDS, *OPTIONAL_COMMANDS):
        lines.extend(
            [
                f"if command -v {quote(command)} >/dev/null 2>&1; then",
                f"  printf 'command\\t{command}\\t1\\n'",
                "else",
                f"  printf 'command\\t{command}\\t0\\n'",
                "fi",
            ]
        )

    lines.extend(remote_path_probe("root", config.remote.root))
    lines.extend(remote_path_probe("workdir", config.remote.workdir))
    lines.extend(
        [
            "if command -v tar >/dev/null 2>&1; then",
            "  tar_version_line=",
            "  tar --version 2>&1 | { IFS= read -r tar_version_line || :; "
            "printf 'tar-version\\t%s\\n' \"$tar_version_line\"; }",
            '  tar_help="$(tar --help 2>&1 || :)"',
        ]
    )
    for option in REQUIRED_TAR_OPTIONS:
        lines.extend(
            [
                f'  case "$tar_help" in *{option}*)',
                f"    printf 'tar-option\\t{option}\\t1\\n' ;;",
                "  *)",
                f"    printf 'tar-option\\t{option}\\t0\\n' ;;",
                "  esac",
            ]
        )
    lines.append("fi")
    return "\n".join(lines) + "\n"


def remote_path_probe(name: str, path: str) -> list[str]:
    quoted_path = quote(path)
    return [
        f"if [ -e {quoted_path} ]; then path_exists=1; else path_exists=0; fi",
        f"if [ -d {quoted_path} ]; then path_directory=1; else path_directory=0; fi",
        f"if [ -r {quoted_path} ]; then path_readable=1; else path_readable=0; fi",
        f"if [ -w {quoted_path} ]; then path_writable=1; else path_writable=0; fi",
        f"printf 'path\\t{name}\\t%s\\t%s\\t%s\\t%s\\n' "
        '"$path_exists" "$path_directory" "$path_readable" "$path_writable"',
    ]


def parse_remote_observations(payload: str) -> RemoteObservations:
    protocol_seen = False
    commands: dict[str, bool] = {}
    tar_version_line: str | None = None
    tar_options: dict[str, bool] = {}
    paths: dict[str, PathObservation] = {}

    for line in payload.splitlines():
        fields = line.split("\t")
        if fields == ["protocol", "1"]:
            protocol_seen = True
        elif len(fields) == COMMAND_RECORD_FIELDS and fields[0] == "command":
            commands[fields[1]] = parse_protocol_bool(fields[2])
        elif len(fields) >= TAR_VERSION_MINIMUM_FIELDS and fields[0] == "tar-version":
            tar_version_line = "\t".join(fields[1:])
        elif len(fields) == COMMAND_RECORD_FIELDS and fields[0] == "tar-option":
            tar_options[fields[1]] = parse_protocol_bool(fields[2])
        elif len(fields) == PATH_RECORD_FIELDS and fields[0] == "path":
            paths[fields[1]] = PathObservation(
                exists=parse_protocol_bool(fields[2]),
                directory=parse_protocol_bool(fields[3]),
                readable=parse_protocol_bool(fields[4]),
                writable_hint=parse_protocol_bool(fields[5]),
            )

    expected_commands = set((*REQUIRED_COMMANDS, *OPTIONAL_COMMANDS))
    if not protocol_seen or set(commands) != expected_commands or set(paths) != {"root", "workdir"}:
        raise DeployError("Remote capability inspection returned an invalid result.")
    if commands["tar"] and (
        tar_version_line is None or set(tar_options) != set(REQUIRED_TAR_OPTIONS)
    ):
        raise DeployError("Remote tar capability inspection returned an invalid result.")

    return RemoteObservations(
        commands=commands,
        tar_version_line=tar_version_line,
        tar_options=tar_options,
        root=paths["root"],
        workdir=paths["workdir"],
    )


def parse_protocol_bool(value: str) -> bool:
    if value == "1":
        return True
    if value == "0":
        return False
    raise DeployError("Remote capability inspection returned an invalid boolean value.")


def evaluate_doctor(
    config: DeployConfig,
    target_name: str,
    observations: RemoteObservations,
    ssh: SshConfig,
    *,
    checked_at: datetime | None = None,
) -> DoctorResult:
    validate_target_name(target_name)
    diagnostics: list[Diagnostic] = []
    statuses: list[Compatibility] = []

    missing_required = [name for name in REQUIRED_COMMANDS if not observations.commands[name]]
    if missing_required:
        statuses.append("incompatible")
        diagnostics.append(
            Diagnostic(
                "error",
                "required-command-missing",
                f"Portable profile command(s) missing: {', '.join(missing_required)}.",
            )
        )

    tar = evaluate_tar(observations)
    statuses.append(tar.compatibility)
    if tar.compatibility == "incompatible":
        diagnostics.append(
            Diagnostic(
                "error",
                "tar-incompatible",
                "Remote tar does not expose every option required by the portable profile.",
            )
        )
    elif tar.compatibility == "undetermined":
        diagnostics.append(
            Diagnostic(
                "warning",
                "tar-version-untested",
                "Remote tar implementation or version is not covered by the SSH E2E matrix.",
            )
        )

    if (
        not observations.root.exists
        or not observations.root.directory
        or not observations.root.readable
    ):
        statuses.append("incompatible")
        diagnostics.append(
            Diagnostic(
                "error",
                "remote-root-unreadable",
                "remote.root must exist as a readable directory.",
            )
        )

    evaluate_workdir(observations.workdir, statuses, diagnostics)

    observed_at = checked_at or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise DeployError("Doctor timestamp must include a timezone.")
    return DoctorResult(
        checked_at=observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        project=config.project,
        target_name=target_name.strip(),
        host_key_policy="discovery" if ssh.allow_host_key_discovery else "strict",
        compatibility=combine_compatibility(statuses),
        commands=observations.commands,
        tar=tar,
        root=observations.root,
        workdir=observations.workdir,
        diagnostics=diagnostics,
    )


def evaluate_tar(observations: RemoteObservations) -> TarCapability:
    if not observations.commands["tar"]:
        return TarCapability(False, None, None, "unavailable", {}, "incompatible")

    implementation, version = parse_tar_identity(observations.tar_version_line or "")
    missing_options = [
        option for option in REQUIRED_TAR_OPTIONS if not observations.tar_options.get(option, False)
    ]
    tested_version = (implementation, version) in TESTED_TAR_VERSIONS
    if missing_options:
        compatibility = "incompatible"
    elif not tested_version:
        compatibility = "undetermined"
    else:
        compatibility = "compatible"

    return TarCapability(
        available=True,
        implementation=implementation,
        version=version,
        version_status="tested" if tested_version else "untested",
        options={option: observations.tar_options[option] for option in REQUIRED_TAR_OPTIONS},
        compatibility=compatibility,
    )


def parse_tar_identity(version_line: str) -> tuple[str | None, str | None]:
    patterns = (
        ("gnu", r"\(GNU tar\)\s+([0-9]+(?:\.[0-9]+)+)"),
        ("busybox", r"(?:BusyBox v|tar \(busybox\)\s+)([0-9]+(?:\.[0-9]+)+)"),
        ("bsdtar", r"bsdtar\s+([0-9]+(?:\.[0-9]+)+)"),
    )
    for implementation, pattern in patterns:
        match = re.search(pattern, version_line, flags=re.IGNORECASE)
        if match:
            return implementation, match.group(1)
    return None, None


def evaluate_workdir(
    workdir: PathObservation,
    statuses: list[Compatibility],
    diagnostics: list[Diagnostic],
) -> None:
    if not workdir.exists:
        statuses.append("undetermined")
        diagnostics.append(
            Diagnostic(
                "warning",
                "remote-workdir-absent",
                "remote.workdir does not exist; write access remains an apply preflight check.",
            )
        )
        return
    if not workdir.directory or not workdir.readable or not workdir.writable_hint:
        statuses.append("incompatible")
        diagnostics.append(
            Diagnostic(
                "error",
                "remote-workdir-unusable",
                "remote.workdir must be a readable directory with observable write permission.",
            )
        )


def combine_compatibility(statuses: list[Compatibility]) -> Compatibility:
    if "incompatible" in statuses:
        return "incompatible"
    if "undetermined" in statuses:
        return "undetermined"
    return "compatible"


def validate_target_name(target_name: str) -> None:
    if not target_name.strip():
        raise DeployError("Doctor target name must be a non-empty, non-secret label.")
    if any(
        ord(character) < CONTROL_CHARACTER_LIMIT or ord(character) == DELETE_CHARACTER
        for character in target_name
    ):
        raise DeployError("Doctor target name must not contain control characters.")


def write_doctor_result(path: Path, result: DoctorResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")


def serialize_path_observation(observation: PathObservation) -> dict[str, bool]:
    return {
        "exists": observation.exists,
        "directory": observation.directory,
        "readable": observation.readable,
        "writable_hint": observation.writable_hint,
    }


def print_doctor_result(result: DoctorResult, output: Path) -> None:
    required_available = sum(result.commands[name] for name in REQUIRED_COMMANDS)
    optional_available = [name for name in OPTIONAL_COMMANDS if result.commands[name]]
    tar_identity = "unidentified"
    if result.tar.implementation and result.tar.version:
        tar_identity = f"{result.tar.implementation} {result.tar.version}"

    print(f"Remote capability doctor for {result.project} ({result.target_name})")
    print(f"Compatibility: {result.compatibility}")
    print(f"SSH host-key policy: {result.host_key_policy}")
    print(f"Required commands: {required_available}/{len(REQUIRED_COMMANDS)} available")
    print(f"Optional commands available: {', '.join(optional_available) or 'none'}")
    print(f"tar: {tar_identity} ({result.tar.compatibility})")
    root_readable = result.root.exists and result.root.directory and result.root.readable
    print(f"remote.root readable: {root_readable}")
    print(
        "remote.workdir observable: "
        f"exists={result.workdir.exists}, directory={result.workdir.directory}, "
        f"readable={result.workdir.readable}, writable_hint={result.workdir.writable_hint}"
    )
    if result.diagnostics:
        print("Diagnostics:")
        for diagnostic in result.diagnostics:
            print(f"- [{diagnostic.severity}] {diagnostic.message}")
    print(f"Doctor report written to {output}")


def render_doctor_github_summary(path: Path) -> str:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != DOCTOR_SCHEMA_VERSION:
        raise DeployError("Unsupported doctor report schema.")
    project = markdown_cell(payload.get("project"))
    target = payload.get("target")
    target_name = markdown_cell(target.get("name") if isinstance(target, dict) else None)
    compatibility = markdown_cell(payload.get("compatibility"))
    tar = payload.get("tar")
    tar_identity = "unidentified"
    tar_compatibility = "undetermined"
    if isinstance(tar, dict):
        implementation = tar.get("implementation")
        version = tar.get("version")
        if isinstance(implementation, str) and isinstance(version, str):
            tar_identity = f"{implementation} {version}"
        if isinstance(tar.get("compatibility"), str):
            tar_compatibility = tar["compatibility"]

    lines = [
        "## Remote Capability Doctor",
        "",
        "| Project | Target | Compatibility | tar | tar verdict |",
        "| --- | --- | --- | --- | --- |",
        f"| {project} | {target_name} | {compatibility} | "
        f"{markdown_cell(tar_identity)} | {markdown_cell(tar_compatibility)} |",
        "",
        "The doctor is read-only. `writable_hint` is observational; apply must test actual writes.",
        "",
    ]
    return "\n".join(lines)


def markdown_cell(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
