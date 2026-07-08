from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_ROOT = REPO_ROOT / "tests/e2e"
SCENARIOS_ROOT = REPO_ROOT / "tests/fixtures/scenarios"


class E2EError(AssertionError):
    pass


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ScenarioWorkspace:
    name: str
    root: Path
    project: Path
    remote: Path
    config: Path
    archive: Path
    report_dir: Path
    expected_report: Path


def require_e2e_prerequisites() -> None:
    missing = [
        command
        for command in ("docker", "ssh", "ssh-keygen", "ssh-keyscan")
        if shutil.which(command) is None
    ]
    if missing:
        raise E2EError(f"Missing E2E prerequisite(s): {', '.join(missing)}")
    run(["docker", "compose", "version"])


def prepare_scenario(
    tmp_path: Path,
    name: str,
    *,
    mutate_remote: Callable[[Path], None] | None = None,
) -> ScenarioWorkspace:
    scenario = SCENARIOS_ROOT / name
    if not scenario.is_dir():
        raise E2EError(f"Scenario not found: {scenario}")

    root = tmp_path / name
    project = root / "project"
    remote = root / "remote"
    shutil.copytree(scenario / "project", project)
    shutil.copytree(scenario / "remote", remote, symlinks=True)
    shutil.copy2(scenario / "deploy.yml", project / "deploy.yml")

    if name == "wordpress-themes":
        write_wordpress_themes_binary_files(project, remote)
    if mutate_remote is not None:
        mutate_remote(remote)

    init_git_repo(project)
    return ScenarioWorkspace(
        name=name,
        root=root,
        project=project,
        remote=remote,
        config=project / "deploy.yml",
        archive=project / "dist/site.tar.gz",
        report_dir=project / "dist/deploy-report",
        expected_report=scenario / "expected/report.json",
    )


def write_wordpress_themes_binary_files(project: Path, remote: Path) -> None:
    project_file = project / "src/wp-content/themes/public-theme/images/logo.dat"
    remote_file = remote / "wp-content/themes/public-theme/images/logo.dat"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    remote_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_bytes(b"\x00new-logo\n")
    remote_file.write_bytes(b"\x00old-logo\n")


def init_git_repo(path: Path) -> None:
    run(["git", "init"], cwd=path)
    run(["git", "config", "user.email", "test@example.com"], cwd=path)
    run(["git", "config", "user.name", "E2E Test"], cwd=path)
    run(["git", "add", "."], cwd=path)
    run(["git", "commit", "-m", "e2e fixture"], cwd=path)


class SshServer:
    def __init__(self, tmp_path: Path, scenario: ScenarioWorkspace) -> None:
        self.tmp_path = tmp_path
        self.scenario = scenario
        self.project_name = f"sshadeploy-{scenario.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.key = tmp_path / f"{scenario.name}-id_ed25519"
        self.known_hosts = tmp_path / f"{scenario.name}-known_hosts"
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise E2EError("SSH server has not been started.")
        return self._port

    def __enter__(self) -> SshServer:
        self.generate_client_key()
        try:
            self.compose(["up", "-d", "--build"])
            self._port = self.resolve_port()
            self.wait_for_tcp()
            self.write_known_hosts()
            self.assert_ssh_ready()
        except BaseException as error:
            logs = self.compose_logs()
            self.cleanup()
            raise E2EError(logs) from error
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self.compose(["down", "-v", "--remove-orphans"], check=False)

    def generate_client_key(self) -> None:
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(self.key), "-q"])

    def compose_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["E2E_REMOTE_FIXTURE"] = str(self.scenario.remote)
        env["E2E_AUTHORIZED_KEYS"] = str(self.key.with_suffix(".pub"))
        return env

    def compose(self, args: list[str], *, check: bool = True) -> CommandResult:
        return run(
            [
                "docker",
                "compose",
                "-f",
                str(E2E_ROOT / "compose.yml"),
                "-p",
                self.project_name,
                *args,
            ],
            env=self.compose_env(),
            check=check,
        )

    def resolve_port(self) -> int:
        result = self.compose(["port", "ssh", "22"])
        endpoint = result.stdout.strip().splitlines()[-1]
        try:
            return int(endpoint.rsplit(":", 1)[1])
        except (IndexError, ValueError) as error:
            message = f"Cannot parse SSH port from docker compose output: {endpoint}"
            raise E2EError(message) from error

    def wait_for_tcp(self) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise E2EError(f"Timed out waiting for SSH port {self.port}.\n{self.compose_logs()}")

    def write_known_hosts(self) -> None:
        result = run(["ssh-keyscan", "-p", str(self.port), "127.0.0.1"])
        self.known_hosts.write_text(result.stdout, encoding="utf-8")

    def assert_ssh_ready(self) -> None:
        run(
            [
                "ssh",
                "-i",
                str(self.key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.known_hosts}",
                "-p",
                str(self.port),
                "deploy@127.0.0.1",
                "true",
            ],
        )

    def compose_logs(self) -> str:
        result = self.compose(["logs", "--no-color"], check=False)
        return result.stdout + result.stderr


def run_cli(
    scenario: ScenarioWorkspace,
    args: list[str],
    *,
    ssh_server: SshServer | None = None,
    check: bool = True,
    known_hosts: Path | None = None,
    include_known_hosts: bool = True,
) -> CommandResult:
    command = [*cli_command(), *args]
    if ssh_server is None:
        return run(command, cwd=scenario.project, check=check)
    ssh_args = [
        "--ssh-host",
        "127.0.0.1",
        "--ssh-user",
        "deploy",
        "--ssh-port",
        str(ssh_server.port),
        "--ssh-private-key-file",
        str(ssh_server.key),
    ]
    if include_known_hosts:
        ssh_args.extend(["--ssh-known-hosts-file", str(known_hosts or ssh_server.known_hosts)])
    return run(
        [
            *command,
            *ssh_args,
        ],
        cwd=scenario.project,
        check=check,
    )


def cli_command() -> list[str]:
    executable = os.environ.get("SSH_ARCHIVE_DEPLOY_E2E_EXECUTABLE")
    if executable is None:
        return ["uv", "run", "--project", str(REPO_ROOT), "ssh-archive-deploy"]

    path = Path(executable)
    if not path.is_absolute():
        raise E2EError("SSH_ARCHIVE_DEPLOY_E2E_EXECUTABLE must be an absolute path.")
    if not path.is_file():
        raise E2EError(f"SSH_ARCHIVE_DEPLOY_E2E_EXECUTABLE does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise E2EError(f"SSH_ARCHIVE_DEPLOY_E2E_EXECUTABLE is not executable: {path}")
    return [str(path)]


def build_validate_report(scenario: ScenarioWorkspace, ssh_server: SshServer) -> None:
    run_cli(
        scenario,
        [
            "build",
            "--config",
            str(scenario.config),
            "--output",
            str(scenario.archive),
            "--release-id",
            f"e2e-{scenario.name}",
        ],
    )
    run_cli(
        scenario,
        ["validate", "--config", str(scenario.config), "--archive", str(scenario.archive)],
    )
    run_cli(
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
    )


def assert_report_matches_golden(scenario: ScenarioWorkspace) -> None:
    actual = json.loads((scenario.report_dir / "report.json").read_text(encoding="utf-8"))
    expected = json.loads(scenario.expected_report.read_text(encoding="utf-8"))
    assert actual == expected


def assert_no_sensitive_aqb_paths(scenario: ScenarioWorkspace) -> None:
    report = (scenario.report_dir / "report.json").read_text(encoding="utf-8")
    forbidden = [
        ".env",
        ".htpasswd",
        "wordpress/wp-content/uploads",
        "catalog/setup.php",
        "catalog/admin/.htpasswd",
        "catalog/admin/filemanager/userfiles",
        "DEPLOYMENT_MANIFEST.json",
    ]
    for path in forbidden:
        assert path not in report


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise E2EError(
            "Command failed:\n"
            f"command: {format_command(command)}\n"
            f"exit code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return CommandResult(result.stdout, result.stderr)


def format_command(command: list[str]) -> str:
    return " ".join(command)
