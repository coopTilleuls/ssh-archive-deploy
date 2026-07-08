from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from ssh_archive_deploy.errors import DeployError


def render_github_summary(report_path: Path) -> str:
    report = load_report(report_path)
    project = require_str(report, "project")
    release_id = require_str(report, "release_id")
    strategy = require_mapping(report, "strategy")
    summary = require_mapping(report, "summary")
    scopes = require_scope_list(report)

    lines = [
        "## Deployment Report",
        "",
        f"Project: {markdown_code(project)}",
        f"Release id: {markdown_code(release_id)}",
        "",
        "### Strategy",
        "",
        f"- apply: {markdown_code(require_str(strategy, 'apply'))}",
        f"- delete unknown: {markdown_code(str(require_bool(strategy, 'delete_unknown')).lower())}",
        "",
        "### Summary",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Changed files | {require_int(summary, 'changed_files')} |",
        f"| Files only in artifact | {require_int(summary, 'artifact_only_files')} |",
        f"| Remote-only files | {require_int(summary, 'remote_only_files')} |",
        f"| Text changed files | {require_int(summary, 'text_changed_files')} |",
        f"| Binary changed files | {require_int(summary, 'binary_changed_files')} |",
        f"| Insertions | {require_int(summary, 'insertions')} |",
        f"| Deletions | {require_int(summary, 'deletions')} |",
        "",
        "### Scopes",
        "",
        "| Scope | Target | Changed | Artifact only | Remote only | Text | Binary | + | - |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for scope in scopes:
        scope_summary = require_mapping(scope, "summary")
        lines.append(
            "| "
            f"{markdown_cell(require_str(scope, 'name'))} | "
            f"{markdown_cell(require_str(scope, 'target'))} | "
            f"{require_int(scope_summary, 'changed_files')} | "
            f"{require_int(scope_summary, 'artifact_only_files')} | "
            f"{require_int(scope_summary, 'remote_only_files')} | "
            f"{require_int(scope_summary, 'text_changed_files')} | "
            f"{require_int(scope_summary, 'binary_changed_files')} | "
            f"{require_int(scope_summary, 'insertions')} | "
            f"{require_int(scope_summary, 'deletions')} |"
        )

    lines.extend(["", result_line(summary)])
    if require_int(summary, "remote_only_files") > 0:
        lines.append("Note: the configured overlay strategy keeps remote-only files in place.")
    lines.append("Detailed file lists are available in the uploaded report artifacts.")
    lines.append("")
    return "\n".join(lines)


def load_report(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DeployError(f"Cannot read report: {path}") from error
    except json.JSONDecodeError as error:
        raise DeployError(f"Invalid JSON report: {path}") from error
    if not isinstance(raw, dict):
        raise DeployError("Report must be a JSON object.")
    return cast(dict[str, object], raw)


def require_mapping(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise DeployError(f"Report field must be an object: {key}")
    return cast(dict[str, object], value)


def require_scope_list(data: dict[str, object]) -> list[dict[str, object]]:
    value = data.get("scopes")
    if not isinstance(value, list):
        raise DeployError("Report field must be a list: scopes")
    scopes: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise DeployError(f"Report scope must be an object at index {index}")
        scopes.append(cast(dict[str, object], item))
    return scopes


def require_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise DeployError(f"Report field must be a string: {key}")
    return value


def require_bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise DeployError(f"Report field must be a boolean: {key}")
    return value


def require_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeployError(f"Report field must be an integer: {key}")
    return value


def result_line(summary: dict[str, object]) -> str:
    if (
        require_int(summary, "changed_files") == 0
        and require_int(summary, "artifact_only_files") == 0
        and require_int(summary, "remote_only_files") == 0
    ):
        return "Result: no drift detected."
    return "Result: drift detected. Review the uploaded report artifacts before deploying."


def markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown_code(value: str) -> str:
    normalized = value.replace("\r", " ").replace("\n", " ")
    longest_run = max((len(run) for run in normalized.split("`") if run == ""), default=0)
    current_run = 0
    for character in normalized:
        if character == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    fence = "`" * (longest_run + 1)
    if normalized.startswith("`") or normalized.endswith("`") or " " in normalized:
        return f"{fence} {normalized} {fence}"
    return f"{fence}{normalized}{fence}"
