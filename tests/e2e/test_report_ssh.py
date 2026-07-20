from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from .harness import (
    CommandResult,
    SshServer,
    assert_no_sensitive_aqb_paths,
    assert_report_matches_golden,
    build_validate_report,
    prepare_scenario,
    run,
    run_cli,
)


@pytest.mark.e2e
def test_doctor_is_strict_read_only_and_reports_tested_capabilities(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")
    doctor_report = scenario.project / "dist/deploy-doctor/doctor.json"
    expected_tar_version = os.environ.get("E2E_EXPECTED_TAR_VERSION", "1.34")

    with SshServer(tmp_path, scenario) as ssh_server:
        before = remote_state_snapshot(ssh_server)
        result = run_cli(
            scenario,
            [
                "doctor",
                "--config",
                str(scenario.config),
                "--target-name",
                "e2e-managed-hosting",
                "--output",
                str(doctor_report),
            ],
            ssh_server=ssh_server,
        )
        after = remote_state_snapshot(ssh_server)

    payload = json.loads(doctor_report.read_text(encoding="utf-8"))
    assert before == after
    assert payload["schema_version"] == 1
    assert payload["target"] == {"name": "e2e-managed-hosting"}
    assert payload["ssh"] == {"host_key_policy": "strict"}
    assert payload["compatibility"] == "compatible"
    assert payload["tar"]["implementation"] == "gnu"
    assert payload["tar"]["version"] == expected_tar_version
    assert payload["tar"]["version_status"] == "tested"
    assert all(payload["tar"]["options"].values())
    assert payload["remote"]["root"]["readable"] is True
    assert payload["remote"]["workdir"]["writable_hint"] is True
    serialized = doctor_report.read_text(encoding="utf-8") + result.stdout
    assert "127.0.0.1" not in serialized
    assert str(ssh_server.key) not in serialized


@pytest.mark.e2e
def test_doctor_requires_known_hosts_by_default(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")

    with SshServer(tmp_path, scenario) as ssh_server:
        result = run_cli(
            scenario,
            [
                "doctor",
                "--config",
                str(scenario.config),
                "--target-name",
                "e2e-managed-hosting",
            ],
            ssh_server=ssh_server,
            include_known_hosts=False,
            check=False,
        )

    assert result.stdout == ""
    assert "known_hosts file is required" in result.stderr


@pytest.mark.e2e
@pytest.mark.parametrize("scenario_name", ["wordpress-themes", "wordpress-catalog"])
def test_report_happy_path_over_ssh(tmp_path: Path, scenario_name: str) -> None:
    scenario = prepare_scenario(tmp_path, scenario_name)

    with SshServer(tmp_path, scenario) as ssh_server:
        build_validate_report(scenario, ssh_server)

    assert_report_matches_golden(scenario)
    assert (scenario.report_dir / "summary.json").exists() is False
    assert (scenario.report_dir / "report.json").exists()
    expected_scope = "public-theme" if scenario_name == "wordpress-themes" else "root-static"
    assert (scenario.report_dir / expected_scope / "changed-files.txt").exists()
    if scenario_name == "wordpress-catalog":
        assert_no_sensitive_aqb_paths(scenario)


@pytest.mark.e2e
def test_report_does_not_require_remote_mktemp(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")

    with SshServer(tmp_path, scenario) as ssh_server:
        ssh_server.compose(
            [
                "exec",
                "-T",
                "--user",
                "root",
                "ssh",
                "mv",
                "/usr/bin/mktemp",
                "/usr/bin/mktemp.disabled",
            ]
        )
        build_validate_report(scenario, ssh_server)

    assert_report_matches_golden(scenario)


@pytest.mark.e2e
def test_report_fails_with_empty_known_hosts(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")
    empty_known_hosts = tmp_path / "empty-known-hosts"
    empty_known_hosts.write_text("", encoding="utf-8")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-wordpress-catalog",
            ],
        )
        run_cli(
            scenario,
            ["validate", "--config", str(scenario.config), "--archive", str(scenario.archive)],
        )
        result = run_cli(
            scenario,
            [
                "report",
                "--config",
                str(scenario.config),
                "--archive",
                str(scenario.archive),
                "--output-dir",
                str(scenario.report_dir),
            ],
            ssh_server=ssh_server,
            known_hosts=empty_known_hosts,
            check=False,
        )

    assert result.stdout == ""
    assert result.stderr
    assert "ERROR:" in result.stderr
    assert not (scenario.report_dir / "report.json").exists()


@pytest.mark.e2e
def test_report_rejects_remote_symlink_over_ssh(tmp_path: Path) -> None:
    def add_remote_symlink(remote: Path) -> None:
        (remote / "images/bad-link").symlink_to("remote-only.svg")

    scenario = prepare_scenario(tmp_path, "wordpress-catalog", mutate_remote=add_remote_symlink)

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-wordpress-catalog",
            ],
        )
        run_cli(
            scenario,
            ["validate", "--config", str(scenario.config), "--archive", str(scenario.archive)],
        )
        result = run_cli(
            scenario,
            [
                "report",
                "--config",
                str(scenario.config),
                "--archive",
                str(scenario.archive),
                "--output-dir",
                str(scenario.report_dir),
            ],
            ssh_server=ssh_server,
            check=False,
        )

    assert result.stdout == ""
    assert "Unsupported remote archive entry type" in result.stderr
    assert not (scenario.report_dir / "report.json").exists()


@pytest.mark.e2e
def test_apply_and_rollback_latest_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-themes")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-wordpress-themes-apply",
            ],
        )
        apply_result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )

        assert "Applied release: e2e-wordpress-themes-apply" in apply_result.stdout
        assert "Remote-only files were preserved" in apply_result.stdout
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/index.php")
            == "<?php\necho 'new public-theme';\n"
        )
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/added.php")
            == "<?php\necho 'added';\n"
        )
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/remote-only.php")
            == "<?php\necho 'manual';\n"
        )
        remote_exec(
            ssh_server, "[ -f /srv/deploy/baselines/server-baseline-before-ci/baseline.tar.gz ]"
        )

        second_apply = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )

        assert "(no-op)" in second_apply.stdout

        rollback_result = run_cli(
            scenario,
            ["rollback", "--config", str(scenario.config), "--release", "latest"],
            ssh_server=ssh_server,
        )

        assert "Rolled back release: e2e-wordpress-themes-apply" in rollback_result.stdout
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/index.php")
            == "<?php\necho 'old public-theme';\n"
        )
        remote_exec(ssh_server, "[ ! -f /srv/project/wp-content/themes/public-theme/added.php ]")
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/remote-only.php")
            == "<?php\necho 'manual';\n"
        )

        reapplied = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )

        assert "(no-op)" not in reapplied.stdout
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/index.php")
            == "<?php\necho 'new public-theme';\n"
        )


@pytest.mark.e2e
def test_apply_recovers_failed_partial_transaction_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-themes")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-recover",
            ],
        )
        first_apply = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )
        transaction_id = output_value(first_apply.stdout, "Transaction id")

        remote_exec(
            ssh_server,
            "\n".join(
                [
                    "set -eu",
                    "printf '%s\\n' \"<?php\" \"echo 'old public-theme';\" "
                    "> /srv/project/wp-content/themes/public-theme/index.php",
                    'tmp="$(mktemp -d)"',
                    'mkdir -p "$tmp/wp-content/themes/public-theme"',
                    "printf '%s\\n' \"<?php\" \"echo 'wrong artifact';\" "
                    '> "$tmp/wp-content/themes/public-theme/index.php"',
                    f"tar -czf /srv/deploy/transactions/{transaction_id}/artifact.tar.gz "
                    '-C "$tmp" wp-content',
                    'rm -rf "$tmp"',
                    'sed -i \'s/"status": "applied"/"status": "failed"/\' '
                    f"/srv/deploy/transactions/{transaction_id}/transaction.json",
                    "rm -f /srv/deploy/pointers/latest-successful-apply",
                ],
            ),
        )

        recovered = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )

        assert f"Transaction id: {transaction_id}" in recovered.stdout
        assert "(no-op)" not in recovered.stdout
        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/index.php")
            == "<?php\necho 'new public-theme';\n"
        )
        assert (
            remote_read(
                ssh_server,
                f"/srv/deploy/transactions/{transaction_id}/transaction.json",
            )
            .split('"status": "', 1)[1]
            .split('"', 1)[0]
            == "applied"
        )


@pytest.mark.e2e
def test_apply_rejects_partial_recovery_without_checkpoint_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-themes")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-missing-checkpoint",
            ],
        )
        first_apply = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )
        transaction_id = output_value(first_apply.stdout, "Transaction id")

        remote_exec(
            ssh_server,
            "\n".join(
                [
                    "set -eu",
                    "printf '%s\\n' \"<?php\" \"echo 'old public-theme';\" "
                    "> /srv/project/wp-content/themes/public-theme/index.php",
                    f"rm -f /srv/deploy/transactions/{transaction_id}/before.tar.gz",
                    'sed -i \'s/"status": "applied"/"status": "failed"/\' '
                    f"/srv/deploy/transactions/{transaction_id}/transaction.json",
                    "rm -f /srv/deploy/pointers/latest-successful-apply",
                ],
            ),
        )

        result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
            check=False,
        )

    assert result.stdout == ""
    assert "Cannot recover partially applied transaction without before.tar.gz" in result.stderr


@pytest.mark.e2e
def test_apply_requires_known_hosts_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-known-hosts-required",
            ],
        )
        result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
            include_known_hosts=False,
            check=False,
        )

    assert result.stdout == ""
    assert "known_hosts file is required" in result.stderr


@pytest.mark.e2e
def test_rollback_rejects_unsafe_transaction_pointer_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-themes")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-bad-pointer",
            ],
        )
        run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )
        remote_exec(
            ssh_server,
            "printf '%s\\n' '../../bad' > /srv/deploy/pointers/latest-successful-apply",
        )

        result = run_cli(
            scenario,
            ["rollback", "--config", str(scenario.config), "--release", "latest"],
            ssh_server=ssh_server,
            check=False,
        )

    assert result.stdout == ""
    assert "Unsafe transaction id" in result.stderr


@pytest.mark.e2e
def test_rollback_rejects_corrupt_before_checkpoint_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-themes")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-bad-checkpoint",
            ],
        )
        apply_result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )
        transaction_id = output_value(apply_result.stdout, "Transaction id")
        remote_exec(
            ssh_server,
            "\n".join(
                [
                    "set -eu",
                    'tmp="$(mktemp -d)"',
                    'mkdir -p "$tmp/wp-content/themes/public-theme"',
                    "printf '%s\\n' \"<?php\" \"echo 'wrong checkpoint';\" "
                    '> "$tmp/wp-content/themes/public-theme/index.php"',
                    f"tar -czf /srv/deploy/transactions/{transaction_id}/before.tar.gz "
                    '-C "$tmp" wp-content',
                    'rm -rf "$tmp"',
                ],
            ),
        )

        result = run_cli(
            scenario,
            ["rollback", "--config", str(scenario.config), "--release", "latest"],
            ssh_server=ssh_server,
            check=False,
        )

        assert (
            remote_read(ssh_server, "/srv/project/wp-content/themes/public-theme/index.php")
            == "<?php\necho 'new public-theme';\n"
        )

    assert result.stdout == ""
    assert "before checkpoint file" in result.stderr


@pytest.mark.e2e
def test_rollback_rejects_unsafe_apply_plan_over_ssh(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-themes")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-bad-plan",
            ],
        )
        apply_result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )
        transaction_id = output_value(apply_result.stdout, "Transaction id")
        remote_exec(
            ssh_server,
            "\n".join(
                [
                    "set -eu",
                    f"cat > /srv/deploy/transactions/{transaction_id}/apply-plan.json <<'JSON'",
                    "{",
                    '  "schema_version": 1,',
                    '  "project": "wordpress-themes",',
                    '  "release_id": "e2e-bad-plan",',
                    '  "artifact_sha256": "ignored-before-path-validation",',
                    '  "strategy": {"apply": "overlay", "delete_unknown": false},',
                    '  "operations": [',
                    "    {",
                    '      "op": "create",',
                    '      "path": "../evil.php",',
                    '      "scope": "public-theme",',
                    '      "size": 1,',
                    '      "sha256": "sha"',
                    "    }",
                    "  ],",
                    '  "created_directories": []',
                    "}",
                    "JSON",
                ],
            ),
        )

        result = run_cli(
            scenario,
            ["rollback", "--config", str(scenario.config), "--release", "latest"],
            ssh_server=ssh_server,
            check=False,
        )

    assert result.stdout == ""
    assert "Unsafe path in apply-plan.json" in result.stderr


@pytest.mark.e2e
def test_apply_rejects_same_release_with_different_archive(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-conflicting-release",
            ],
        )
        run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
        )
        (scenario.project / "index.html").write_text("changed again\n", encoding="utf-8")
        run(["git", "add", "index.html"], cwd=scenario.project)
        run(["git", "commit", "-m", "change index"], cwd=scenario.project)
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-conflicting-release",
            ],
        )

        result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
            check=False,
        )

    assert result.stdout == ""
    assert "uses this release id with another archive" in result.stderr


@pytest.mark.e2e
def test_apply_fails_when_remote_lock_exists(tmp_path: Path) -> None:
    scenario = prepare_scenario(tmp_path, "wordpress-catalog")

    with SshServer(tmp_path, scenario) as ssh_server:
        run_cli(
            scenario,
            [
                "build",
                "--config",
                str(scenario.config),
                "--output",
                str(scenario.archive),
                "--release-id",
                "e2e-lock",
            ],
        )
        remote_exec(ssh_server, "mkdir -p /srv/deploy/locks/deploy.lock")
        result = run_cli(
            scenario,
            ["apply", "--config", str(scenario.config), "--archive", str(scenario.archive)],
            ssh_server=ssh_server,
            check=False,
        )

    assert result.stdout == ""
    assert "deployment lock already exists" in result.stderr


def remote_read(ssh_server: SshServer, path: str) -> str:
    return remote_exec(ssh_server, f"cat {path}").stdout


def remote_state_snapshot(ssh_server: SshServer) -> str:
    return remote_exec(
        ssh_server,
        "find /srv/project /srv/deploy -printf '%p\\t%y\\t%s\\n' | sort; "
        "find /srv/project /srv/deploy -type f -exec sha256sum {} \\; | sort",
    ).stdout


def remote_exec(ssh_server: SshServer, script: str, *, check: bool = True) -> CommandResult:
    return run(
        [
            "ssh",
            "-i",
            str(ssh_server.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={ssh_server.known_hosts}",
            "-p",
            str(ssh_server.port),
            "deploy@127.0.0.1",
            script,
        ],
        check=check,
    )


def output_value(output: str, label: str) -> str:
    prefix = f"{label}: "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    raise AssertionError(f"Missing output line: {prefix}")
