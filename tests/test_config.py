from __future__ import annotations

import pytest

from ssh_archive_deploy.config import CONFIG_VERSION, parse_config
from ssh_archive_deploy.errors import DeployError


def minimal_config() -> dict[str, object]:
    return {
        "version": 2,
        "project": "demo",
        "remote": {"root": "/var/www/html", "workdir": "/.deploy/demo"},
        "scope": [{"name": "theme", "source": "theme", "target": "wp-content/themes/demo"}],
    }


def test_parse_minimal_config() -> None:
    config = parse_config(minimal_config())

    assert config.version == CONFIG_VERSION
    assert config.project == "demo"
    assert config.remote.root == "/var/www/html"
    assert config.strategy.apply == "overlay"
    assert config.scopes[0].source == "theme"


@pytest.mark.parametrize(
    "baseline_id",
    ["", ".", "..", "../escape", "path/value", "bad\nvalue", "a" * 129],
)
def test_rejects_unsafe_baseline_identifier(baseline_id: str) -> None:
    raw = minimal_config()
    raw["backup"] = {"baseline_id": baseline_id}

    with pytest.raises(DeployError, match="baseline_id"):
        parse_config(raw)


def test_accepts_maximum_length_baseline_identifier() -> None:
    raw = minimal_config()
    raw["backup"] = {"baseline_id": "a" * 128}

    assert parse_config(raw).backup.baseline_id == "a" * 128


def test_rejects_overlapping_scope_targets() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {"name": "parent", "source": "parent", "target": "wp-content"},
        {
            "name": "theme",
            "source": "theme",
            "target": "wp-content/themes/example",
        },
    ]

    with pytest.raises(DeployError, match="must not overlap"):
        parse_config(raw)


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


def test_rejects_version_one() -> None:
    raw = minimal_config()
    raw["version"] = 1

    with pytest.raises(DeployError, match="version=1 is not compatible"):
        parse_config(raw)


def test_parses_generated_inputs() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "wp-content/themes/demo",
            "generated": [
                {"path": "vendor", "required_paths": ["autoload.php"]},
                {"path": "public/build"},
            ],
        }
    ]

    config = parse_config(raw)

    assert config.scopes[0].generated[0].path == "vendor"
    assert config.scopes[0].generated[0].required_paths == ["autoload.php"]
    assert config.scopes[0].generated[1].required_paths == []


@pytest.mark.parametrize("path", ["/vendor", "../vendor", "."])
def test_rejects_unsafe_generated_path(path: str) -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "theme",
            "generated": [{"path": path}],
        }
    ]

    with pytest.raises(DeployError, match="Path|Relative"):
        parse_config(raw)


def test_rejects_unsafe_generated_required_path() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "theme",
            "generated": [{"path": "vendor", "required_paths": ["../autoload.php"]}],
        }
    ]

    with pytest.raises(DeployError, match=r"\.\."):
        parse_config(raw)


def test_rejects_overlapping_generated_paths() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "theme",
            "generated": [{"path": "vendor"}, {"path": "vendor/package"}],
        }
    ]

    with pytest.raises(DeployError, match="must not overlap"):
        parse_config(raw)


def test_rejects_duplicate_generated_required_paths() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "theme",
            "generated": [
                {
                    "path": "vendor",
                    "required_paths": ["autoload.php", "autoload.php"],
                }
            ],
        }
    ]

    with pytest.raises(DeployError, match="required_paths must be unique"):
        parse_config(raw)


def test_rejects_non_list_generated_inputs() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "theme",
            "generated": {"path": "vendor"},
        }
    ]

    with pytest.raises(DeployError, match="generated must be a list"):
        parse_config(raw)


def test_rejects_unknown_generated_key() -> None:
    raw = minimal_config()
    raw["scope"] = [
        {
            "name": "theme",
            "source": "theme",
            "target": "theme",
            "generated": [{"path": "vendor", "optional": True}],
        }
    ]

    with pytest.raises(DeployError, match="unsupported key"):
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
