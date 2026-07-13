from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ssh_archive_deploy.config import DeployConfig, parse_config
from ssh_archive_deploy.doctor import (
    DOCTOR_SCHEMA_VERSION,
    OPTIONAL_COMMANDS,
    REQUIRED_COMMANDS,
    REQUIRED_TAR_OPTIONS,
    PathObservation,
    RemoteObservations,
    evaluate_doctor,
    parse_remote_observations,
    render_doctor_github_summary,
    write_doctor_result,
)
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.report import SshConfig


def test_parses_remote_protocol_and_serializes_versioned_result(tmp_path: Path) -> None:
    observations = parse_remote_observations(protocol_payload())
    result = evaluate_doctor(
        config(),
        "preproduction",
        observations,
        ssh_config(),
        checked_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
    )
    output = tmp_path / "doctor.json"

    write_doctor_result(output, result)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == DOCTOR_SCHEMA_VERSION
    assert payload["operation"] == "doctor"
    assert payload["checked_at"] == "2026-07-10T12:30:00Z"
    assert payload["project"] == "example"
    assert payload["target"] == {"name": "preproduction"}
    assert payload["ssh"] == {"host_key_policy": "strict"}
    assert payload["compatibility"] == "compatible"
    assert payload["tar"] == {
        "available": True,
        "implementation": "gnu",
        "version": "1.34",
        "version_status": "tested",
        "options": {option: True for option in REQUIRED_TAR_OPTIONS},
        "compatibility": "compatible",
    }
    assert payload["remote"]["root"]["readable"] is True
    assert payload["remote"]["workdir"]["writable_hint"] is True


def test_missing_required_command_is_incompatible() -> None:
    observations = observations_with(commands={"cp": False})

    result = evaluate_doctor(config(), "production", observations, ssh_config())

    assert result.compatibility == "incompatible"
    assert diagnostic_codes(result) == ["required-command-missing"]


def test_missing_optional_command_does_not_change_compatibility() -> None:
    observations = observations_with(commands={"rsync": False, "wp": False})

    result = evaluate_doctor(config(), "production", observations, ssh_config())

    assert result.compatibility == "compatible"
    assert result.diagnostics == []
    payload = result.to_dict()
    assert payload["commands"]["absent"] == ["rsync", "wp"]  # type: ignore[index]


def test_tar_missing_required_option_is_incompatible() -> None:
    observations = observations_with(tar_options={"--directory": False})

    result = evaluate_doctor(config(), "production", observations, ssh_config())

    assert result.tar.compatibility == "incompatible"
    assert result.compatibility == "incompatible"
    assert diagnostic_codes(result) == ["tar-incompatible"]


def test_untested_tar_version_with_missing_option_is_incompatible() -> None:
    observations = observations_with(
        tar_version_line="tar (GNU tar) 1.35",
        tar_options={"--directory": False},
    )

    result = evaluate_doctor(config(), "production", observations, ssh_config())

    assert result.tar.version == "1.35"
    assert result.tar.version_status == "untested"
    assert result.tar.compatibility == "incompatible"
    assert result.compatibility == "incompatible"
    assert diagnostic_codes(result) == ["tar-incompatible"]


def test_untested_tar_version_with_required_options_is_undetermined() -> None:
    observations = observations_with(tar_version_line="tar (GNU tar) 1.35")

    result = evaluate_doctor(config(), "production", observations, ssh_config())

    assert result.tar.version == "1.35"
    assert result.tar.version_status == "untested"
    assert result.tar.compatibility == "undetermined"
    assert result.compatibility == "undetermined"
    assert diagnostic_codes(result) == ["tar-version-untested"]


def test_absent_workdir_is_undetermined_without_write_probe() -> None:
    observations = observations_with(
        workdir=PathObservation(False, False, False, False),
    )

    result = evaluate_doctor(config(), "production", observations, ssh_config())

    assert result.compatibility == "undetermined"
    assert diagnostic_codes(result) == ["remote-workdir-absent"]


def test_invalid_remote_protocol_is_rejected() -> None:
    with pytest.raises(DeployError, match="invalid result"):
        parse_remote_observations("protocol\t1\ncommand\tsh\t1\n")


def test_target_name_rejects_control_characters() -> None:
    with pytest.raises(DeployError, match="control characters"):
        evaluate_doctor(config(), "production\nsecret", observations_with(), ssh_config())


def test_github_summary_contains_only_non_secret_target_identity(tmp_path: Path) -> None:
    result = evaluate_doctor(
        config(),
        "preproduction",
        observations_with(),
        ssh_config(),
        checked_at=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
    )
    output = tmp_path / "doctor.json"
    write_doctor_result(output, result)

    summary = render_doctor_github_summary(output)

    assert summary.startswith("## Remote Capability Doctor\n")
    assert "preproduction" in summary
    assert "example.internal" not in summary
    assert "deploy.key" not in summary


def config() -> DeployConfig:
    raw: dict[str, Any] = {
        "version": 1,
        "project": "example",
        "remote": {"root": "/srv/project", "workdir": "/srv/deploy"},
        "scope": [{"name": "theme", "source": "theme", "target": "theme"}],
    }
    return parse_config(raw)


def ssh_config() -> SshConfig:
    return SshConfig(
        host="example.internal",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file="known_hosts",
    )


def observations_with(
    *,
    commands: dict[str, bool] | None = None,
    tar_version_line: str = "tar (GNU tar) 1.34",
    tar_options: dict[str, bool] | None = None,
    root: PathObservation | None = None,
    workdir: PathObservation | None = None,
) -> RemoteObservations:
    command_values = {name: True for name in (*REQUIRED_COMMANDS, *OPTIONAL_COMMANDS)}
    command_values.update(commands or {})
    option_values = {option: True for option in REQUIRED_TAR_OPTIONS}
    option_values.update(tar_options or {})
    return RemoteObservations(
        commands=command_values,
        tar_version_line=tar_version_line,
        tar_options=option_values,
        root=root or PathObservation(True, True, True, True),
        workdir=workdir or PathObservation(True, True, True, True),
    )


def protocol_payload() -> str:
    lines = ["protocol\t1"]
    lines.extend(f"command\t{name}\t1" for name in (*REQUIRED_COMMANDS, *OPTIONAL_COMMANDS))
    lines.append("tar-version\ttar (GNU tar) 1.34")
    lines.extend(f"tar-option\t{option}\t1" for option in REQUIRED_TAR_OPTIONS)
    lines.extend(
        [
            "path\troot\t1\t1\t1\t1",
            "path\tworkdir\t1\t1\t1\t1",
        ]
    )
    return "\n".join(lines) + "\n"


def diagnostic_codes(result: object) -> list[str]:
    assert hasattr(result, "diagnostics")
    return [diagnostic.code for diagnostic in result.diagnostics]  # type: ignore[attr-defined]
