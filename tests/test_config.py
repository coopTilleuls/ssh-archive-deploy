from __future__ import annotations

import pytest

from ssh_archive_deploy.config import parse_config
from ssh_archive_deploy.errors import DeployError


def minimal_config() -> dict[str, object]:
    return {
        "version": 1,
        "project": "demo",
        "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
        "scope": [{"name": "theme", "source": "theme", "target": "wp-content/themes/demo"}],
    }


def test_parse_minimal_config() -> None:
    config = parse_config(minimal_config())

    assert config.version == 1
    assert config.project == "demo"
    assert config.remote.root == "/var/www/html"
    assert config.strategy.apply == "overlay"
    assert config.scopes[0].source == "theme"


def test_rejects_absolute_source() -> None:
    raw = minimal_config()
    raw["scope"] = [{"name": "bad", "source": "/theme", "target": "theme"}]

    with pytest.raises(DeployError, match="Relative path expected"):
        parse_config(raw)


def test_rejects_root_remote_path() -> None:
    raw = minimal_config()
    raw["remote"] = {"root": "/", "workdir": "/.deploy/demo"}

    with pytest.raises(DeployError, match="must not be /"):
        parse_config(raw)


def test_rejects_missing_remote_workdir() -> None:
    raw = minimal_config()
    raw["remote"] = {"root": "/var/www/html"}

    with pytest.raises(DeployError, match="workdir"):
        parse_config(raw)


def test_rejects_remote_workdir_inside_remote_root() -> None:
    raw = minimal_config()
    raw["remote"] = {"root": "/var/www/html", "workdir": "/var/www/html/.deploy"}

    with pytest.raises(DeployError, match="outside remote.root"):
        parse_config(raw)


def test_rejects_legacy_apply_strategy() -> None:
    raw = minimal_config()
    raw["strategy"] = {"apply": "backup_then_replace"}

    with pytest.raises(DeployError, match="overlay"):
        parse_config(raw)


def test_rejects_delete_unknown() -> None:
    raw = minimal_config()
    raw["strategy"] = {"apply": "overlay", "delete_unknown": True}

    with pytest.raises(DeployError, match="delete_unknown"):
        parse_config(raw)


def test_rejects_missing_version() -> None:
    raw = minimal_config()
    del raw["version"]

    with pytest.raises(DeployError, match="version"):
        parse_config(raw)


def test_rejects_string_delete_unknown() -> None:
    raw = minimal_config()
    raw["strategy"] = {"apply": "overlay", "delete_unknown": "false"}

    with pytest.raises(DeployError, match="boolean"):
        parse_config(raw)


def test_rejects_string_retention() -> None:
    raw = minimal_config()
    raw["backup"] = {"retention": "3"}

    with pytest.raises(DeployError, match="integer"):
        parse_config(raw)


def test_rejects_unknown_keys() -> None:
    raw = minimal_config()
    raw["unexpected"] = True

    with pytest.raises(DeployError, match="unsupported key"):
        parse_config(raw)
