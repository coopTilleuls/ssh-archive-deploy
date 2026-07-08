from __future__ import annotations

import pytest

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


def test_report_ssh_command_can_still_accept_new_host_key() -> None:
    ssh = SshConfig(
        host="example.com",
        user="deploy",
        port="22",
        alias=None,
        private_key_file="deploy.key",
        known_hosts_file=None,
    )

    command = ssh_command(ssh)

    assert "StrictHostKeyChecking=accept-new" in command


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
