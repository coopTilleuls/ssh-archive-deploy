from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.harness import CommandResult, E2EError, ScenarioWorkspace, SshServer


def test_ssh_server_cleans_up_when_enter_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = ScenarioWorkspace(
        name="broken",
        root=tmp_path,
        project=tmp_path / "project",
        remote=tmp_path / "remote",
        config=tmp_path / "project/deploy.yml",
        archive=tmp_path / "project/dist/site.tar.gz",
        report_dir=tmp_path / "project/dist/deploy-report",
        expected_report=tmp_path / "expected/report.json",
    )
    server = SshServer(tmp_path, scenario)
    calls: list[list[str]] = []

    monkeypatch.setattr(server, "generate_client_key", lambda: None)
    monkeypatch.setattr(server, "resolve_port", lambda: (_ for _ in ()).throw(E2EError("boom")))
    monkeypatch.setattr(server, "compose_logs", lambda: "compose logs")

    def fake_compose(args: list[str], *, check: bool = True) -> CommandResult:
        calls.append(args)
        return CommandResult("", "")

    monkeypatch.setattr(server, "compose", fake_compose)

    with pytest.raises(E2EError, match="compose logs"):
        server.__enter__()

    assert calls == [
        ["up", "-d", "--build"],
        ["down", "-v", "--remove-orphans"],
    ]
