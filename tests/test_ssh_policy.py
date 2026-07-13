from __future__ import annotations

import pytest

from ssh_archive_deploy.cli import build_parser
from ssh_archive_deploy.deploy import validate_mutating_ssh
from ssh_archive_deploy.errors import DeployError
from ssh_archive_deploy.report import SshConfig, ssh_command


def test_mutating_modes_require_known_hosts_file() -> None:
    ssh = SshConfig(
        host="example.com",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file=None,
    )

    with pytest.raises(DeployError, match="known_hosts file is required"):
        validate_mutating_ssh(ssh)


def test_read_only_ssh_requires_known_hosts_by_default() -> None:
    ssh = SshConfig(
        host="example.com",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file=None,
    )

    with pytest.raises(DeployError, match="known_hosts file is required"):
        ssh_command(ssh)


def test_read_only_ssh_can_explicitly_discover_host_key_without_persisting_it() -> None:
    ssh = SshConfig(
        host="example.com",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file=None,
        allow_host_key_discovery=True,
    )

    command = ssh_command(ssh)

    assert "StrictHostKeyChecking=accept-new" in command
    assert "UserKnownHostsFile=/dev/null" in command


def test_mutating_modes_reject_host_key_discovery() -> None:
    ssh = SshConfig(
        host="example.com",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file=None,
        allow_host_key_discovery=True,
    )

    with pytest.raises(DeployError, match="not allowed for mutating modes"):
        validate_mutating_ssh(ssh)


def test_known_hosts_file_uses_strict_checking() -> None:
    ssh = SshConfig(
        host="example.com",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file="known_hosts",
    )

    command = ssh_command(ssh)

    assert "StrictHostKeyChecking=yes" in command
    assert "UserKnownHostsFile=known_hosts" in command


def test_host_key_discovery_flag_only_exists_on_read_only_commands() -> None:
    parser = build_parser()

    doctor = parser.parse_args(
        [
            "doctor",
            "--config",
            "deploy.yml",
            "--target-name",
            "production",
            "--allow-host-key-discovery",
        ]
    )
    report = parser.parse_args(
        [
            "report",
            "--config",
            "deploy.yml",
            "--archive",
            "site.tar.gz",
            "--allow-host-key-discovery",
        ]
    )

    assert doctor.allow_host_key_discovery is True
    assert report.allow_host_key_discovery is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "apply",
                "--config",
                "deploy.yml",
                "--archive",
                "site.tar.gz",
                "--allow-host-key-discovery",
            ]
        )
