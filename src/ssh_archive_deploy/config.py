from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

from ssh_archive_deploy.errors import DeployError


@dataclass(frozen=True)
class RemoteConfig:
    root: str
    workdir: str


@dataclass(frozen=True)
class StrategyConfig:
    apply: str = "overlay"
    delete_unknown: bool = False


@dataclass(frozen=True)
class BackupConfig:
    retention: int = 3
    baseline_id: str = "server-baseline-before-ci"


@dataclass(frozen=True)
class ScopeConfig:
    name: str
    source: str
    target: str
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeployConfig:
    version: int
    project: str
    remote: RemoteConfig
    strategy: StrategyConfig
    backup: BackupConfig
    scopes: list[ScopeConfig]
    exclude: list[str] = field(default_factory=list)


def load_config(path: str) -> DeployConfig:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise DeployError("Config file must contain a YAML mapping.")
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> DeployConfig:
    reject_unknown_keys(
        raw,
        {"version", "project", "remote", "strategy", "backup", "scope", "exclude"},
        "config",
    )
    version = require_int(raw, "version")
    if version != 1:
        raise DeployError("Only config version=1 is supported.")

    project = require_str(raw, "project")
    remote_raw = require_mapping(raw, "remote")
    reject_unknown_keys(remote_raw, {"root", "workdir"}, "remote")
    strategy_raw = optional_mapping(raw, "strategy")
    reject_unknown_keys(strategy_raw, {"apply", "delete_unknown"}, "strategy")
    backup_raw = optional_mapping(raw, "backup")
    reject_unknown_keys(backup_raw, {"retention", "baseline_id"}, "backup")

    remote = RemoteConfig(
        root=require_str(remote_raw, "root"),
        workdir=require_str(remote_raw, "workdir"),
    )
    validate_absolute_safe_path("remote.root", remote.root)
    validate_absolute_safe_path("remote.workdir", remote.workdir)
    if is_same_or_child(remote.workdir, remote.root):
        raise DeployError("remote.workdir must be outside remote.root.")

    strategy = StrategyConfig(
        apply=optional_str(strategy_raw, "apply", "overlay"),
        delete_unknown=optional_bool(strategy_raw, "delete_unknown", False),
    )
    if strategy.apply != "overlay":
        raise DeployError("Only strategy.apply=overlay is supported.")
    if strategy.delete_unknown:
        raise DeployError("strategy.delete_unknown=true is not supported in this increment.")

    backup = BackupConfig(
        retention=optional_int(backup_raw, "retention", 3),
        baseline_id=optional_str(backup_raw, "baseline_id", "server-baseline-before-ci"),
    )
    if backup.retention < 1:
        raise DeployError("backup.retention must be a positive integer.")

    scopes_raw = raw.get("scope")
    if not isinstance(scopes_raw, list) or not scopes_raw:
        raise DeployError("scope must be a non-empty list.")

    scopes = [parse_scope(index, item) for index, item in enumerate(scopes_raw)]
    names = [scope.name for scope in scopes]
    if len(names) != len(set(names)):
        raise DeployError("scope names must be unique.")

    excludes = string_list(raw.get("exclude", []), "exclude")
    for pattern in excludes:
        validate_relative_pattern("exclude", pattern)

    return DeployConfig(
        version=version,
        project=project,
        remote=remote,
        strategy=strategy,
        backup=backup,
        scopes=scopes,
        exclude=excludes,
    )


def parse_scope(index: int, raw: Any) -> ScopeConfig:
    if not isinstance(raw, dict):
        raise DeployError(f"scope[{index}] must be a mapping.")
    reject_unknown_keys(raw, {"name", "source", "target", "include", "exclude"}, f"scope[{index}]")

    scope = ScopeConfig(
        name=require_str(raw, "name"),
        source=normalize_relative_path(require_str(raw, "source"), allow_dot=True),
        target=normalize_relative_path(require_str(raw, "target"), allow_dot=True),
        include=string_list(raw.get("include", []), f"scope[{index}].include"),
        exclude=string_list(raw.get("exclude", []), f"scope[{index}].exclude"),
    )
    for pattern in [*scope.include, *scope.exclude]:
        validate_relative_pattern(f"scope[{index}] pattern", pattern)
    return scope


def reject_unknown_keys(raw: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise DeployError(f"{name} contains unsupported key: {unknown[0]}")


def require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise DeployError(f"{key} must be a non-empty string.")
    return value


def optional_str(raw: dict[str, Any], key: str, default: str) -> str:
    if key not in raw:
        return default
    return require_str(raw, key)


def require_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeployError(f"{key} must be an integer.")
    return value


def optional_int(raw: dict[str, Any], key: str, default: int) -> int:
    if key not in raw:
        return default
    return require_int(raw, key)


def optional_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw.get(key)
    if not isinstance(value, bool):
        raise DeployError(f"{key} must be a boolean.")
    return value


def require_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise DeployError(f"{key} must be a mapping.")
    return value


def optional_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in raw:
        return {}
    return require_mapping(raw, key)


def string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeployError(f"{name} must be a list of strings.")
    return value


def validate_absolute_safe_path(name: str, value: str) -> None:
    if not value.startswith("/"):
        raise DeployError(f"{name} must be an absolute path.")
    if value == "/":
        raise DeployError(f"{name} must not be /.")
    if "\n" in value or "\r" in value:
        raise DeployError(f"{name} must not contain newlines.")
    path = PurePosixPath(value)
    if ".." in path.parts or "." in path.parts:
        raise DeployError(f"{name} must not contain . or .. path segments.")


def is_same_or_child(candidate: str, parent: str) -> bool:
    candidate_parts = PurePosixPath(candidate).parts
    parent_parts = PurePosixPath(parent).parts
    return candidate_parts[: len(parent_parts)] == parent_parts


def normalize_relative_path(value: str, *, allow_dot: bool) -> str:
    if value == "." and allow_dot:
        return "."
    if value.startswith("/"):
        raise DeployError(f"Relative path expected, got absolute path: {value}")
    if "\n" in value or "\r" in value:
        raise DeployError(f"Path must not contain newlines: {value}")
    path = PurePosixPath(value)
    if ".." in path.parts or "." in path.parts:
        raise DeployError(f"Path must not contain . or .. segments: {value}")
    normalized = path.as_posix()
    if normalized == "." and not allow_dot:
        raise DeployError("Path must not be empty.")
    return normalized


def validate_relative_pattern(name: str, value: str) -> None:
    if not value:
        raise DeployError(f"{name} pattern must not be empty.")
    if value.startswith("/"):
        raise DeployError(f"{name} pattern must be relative: {value}")
    if "\n" in value or "\r" in value:
        raise DeployError(f"{name} pattern must not contain newlines.")
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise DeployError(f"{name} pattern must not contain ..: {value}")
