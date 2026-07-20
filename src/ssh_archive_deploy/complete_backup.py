from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from ssh_archive_deploy.errors import DeployError

if TYPE_CHECKING:
    from ssh_archive_deploy.config import DeployConfig

LAYOUT_VERSION = 2
BACKUP_SCHEMA_VERSION = 1
BACKUP_MANIFEST_SCHEMA_VERSION = 1
ARCHIVE_NAME = "archive.tar.gz"
BACKUP_METADATA_NAME = "backup.json"
BACKUP_MANIFEST_NAME = "manifest.json"

BackupKind = Literal["baseline", "pre_apply", "pre_rollback"]
EntryType = Literal["file", "directory"]

IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
UTC_TIMESTAMP_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
LEGACY_STATE_ROOTS = frozenset({"transactions", "baselines", "pointers"})
CURRENT_STATE_ROOT = "ssh-archive-deploy/state-v2"
MAX_PERMISSION_MODE = 0o7777
ASCII_CONTROL_CODES = frozenset(range(0x20)) | {0x7F}
MIN_SURROGATE_CODE_POINT = 0xD800
MAX_SURROGATE_CODE_POINT = 0xDFFF


@dataclass(frozen=True)
class Layout:
    layout_version: int = LAYOUT_VERSION


@dataclass(frozen=True)
class BackupArchive:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupManifestReference:
    path: str
    sha256: str


@dataclass(frozen=True)
class BackupSource:
    operation_id: str
    release_id: str
    artifact_sha256: str


@dataclass(frozen=True)
class BackupMetadata:
    schema_version: int
    backup_id: str
    kind: BackupKind
    created_at: str
    project: str
    remote_root: str
    scope_config_digest: str
    protected: bool
    archive: BackupArchive
    manifest: BackupManifestReference
    source: BackupSource | None = None


@dataclass(frozen=True)
class BackupScope:
    name: str
    target: str


@dataclass(frozen=True)
class BackupFileEntry:
    path: str
    scope: str
    type: Literal["file"]
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupDirectoryEntry:
    path: str
    scope: str
    type: Literal["directory"]
    mode: int


BackupEntry = BackupFileEntry | BackupDirectoryEntry


@dataclass(frozen=True)
class BackupManifest:
    schema_version: int
    backup_id: str
    scopes: tuple[BackupScope, ...]
    entries: tuple[BackupEntry, ...]


@dataclass(frozen=True)
class NamespaceClassification:
    state_v2_present: bool
    legacy_roots: tuple[str, ...]


def canonical_json(payload: object) -> str:
    try:
        result = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise DeployError(f"Value cannot be serialized as canonical JSON: {error}") from error


def layout_to_json(layout: Layout) -> str:
    validate_layout(layout)
    return canonical_json({"layout_version": layout.layout_version})


def layout_from_json(raw: str) -> Layout:
    payload = strict_json_object(raw, "layout.json")
    require_exact_keys(payload, {"layout_version"}, set(), "layout.json")
    layout = Layout(layout_version=require_int(payload, "layout_version", "layout.json"))
    validate_layout(layout)
    return layout


def backup_metadata_to_json(metadata: BackupMetadata) -> str:
    validate_backup_metadata(metadata)
    return canonical_json(backup_metadata_payload(metadata))


def backup_metadata_from_json(raw: str) -> BackupMetadata:
    payload = strict_json_object(raw, BACKUP_METADATA_NAME)
    required = {
        "schema_version",
        "backup_id",
        "kind",
        "created_at",
        "project",
        "remote_root",
        "scope_config_digest",
        "protected",
        "archive",
        "manifest",
    }
    require_exact_keys(payload, required, {"source"}, BACKUP_METADATA_NAME)

    archive_payload = require_mapping(payload, "archive", BACKUP_METADATA_NAME)
    require_exact_keys(
        archive_payload,
        {"path", "size", "sha256"},
        set(),
        "backup.json archive",
    )
    manifest_payload = require_mapping(payload, "manifest", BACKUP_METADATA_NAME)
    require_exact_keys(
        manifest_payload,
        {"path", "sha256"},
        set(),
        "backup.json manifest",
    )

    raw_source = payload.get("source")
    source: BackupSource | None = None
    if "source" in payload:
        if not isinstance(raw_source, dict):
            raise DeployError("backup.json source must be an object.")
        source_payload = cast(dict[str, object], raw_source)
        require_exact_keys(
            source_payload,
            {"operation_id", "release_id", "artifact_sha256"},
            set(),
            "backup.json source",
        )
        source = BackupSource(
            operation_id=require_str(source_payload, "operation_id", "backup.json source"),
            release_id=require_str(source_payload, "release_id", "backup.json source"),
            artifact_sha256=require_str(
                source_payload,
                "artifact_sha256",
                "backup.json source",
            ),
        )

    metadata = BackupMetadata(
        schema_version=require_int(payload, "schema_version", BACKUP_METADATA_NAME),
        backup_id=require_str(payload, "backup_id", BACKUP_METADATA_NAME),
        kind=require_backup_kind(payload),
        created_at=require_str(payload, "created_at", BACKUP_METADATA_NAME),
        project=require_str(payload, "project", BACKUP_METADATA_NAME),
        remote_root=require_str(payload, "remote_root", BACKUP_METADATA_NAME),
        scope_config_digest=require_str(
            payload,
            "scope_config_digest",
            BACKUP_METADATA_NAME,
        ),
        protected=require_bool(payload, "protected", BACKUP_METADATA_NAME),
        archive=BackupArchive(
            path=require_str(archive_payload, "path", "backup.json archive"),
            size=require_int(archive_payload, "size", "backup.json archive"),
            sha256=require_str(archive_payload, "sha256", "backup.json archive"),
        ),
        manifest=BackupManifestReference(
            path=require_str(manifest_payload, "path", "backup.json manifest"),
            sha256=require_str(manifest_payload, "sha256", "backup.json manifest"),
        ),
        source=source,
    )
    validate_backup_metadata(metadata)
    return metadata


def backup_manifest_to_json(manifest: BackupManifest) -> str:
    validate_backup_manifest(manifest)
    return canonical_json(backup_manifest_payload(manifest))


def backup_manifest_from_json(raw: str) -> BackupManifest:
    payload = strict_json_object(raw, BACKUP_MANIFEST_NAME)
    require_exact_keys(
        payload,
        {"schema_version", "backup_id", "scopes", "entries"},
        set(),
        BACKUP_MANIFEST_NAME,
    )
    raw_scopes = require_list(payload, "scopes", BACKUP_MANIFEST_NAME)
    raw_entries = require_list(payload, "entries", BACKUP_MANIFEST_NAME)

    scopes = tuple(parse_scope(item, index) for index, item in enumerate(raw_scopes))
    entries = tuple(parse_entry(item, index) for index, item in enumerate(raw_entries))
    manifest = BackupManifest(
        schema_version=require_int(payload, "schema_version", BACKUP_MANIFEST_NAME),
        backup_id=require_str(payload, "backup_id", BACKUP_MANIFEST_NAME),
        scopes=scopes,
        entries=entries,
    )
    validate_backup_manifest(manifest)
    return manifest


def scope_config_digest(config: DeployConfig) -> str:
    validate_scope_targets(
        tuple(BackupScope(name=scope.name, target=scope.target) for scope in config.scopes)
    )
    payload = {
        "exclude": sorted(config.exclude),
        "scopes": [
            {
                "exclude": sorted(scope.exclude),
                "include": sorted(scope.include),
                "name": scope.name,
                "target": scope.target,
            }
            for scope in sorted(config.scopes, key=lambda item: item.name)
        ],
        "version": config.version,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def validate_complete_backup_bundle(bundle: Path) -> tuple[BackupMetadata, BackupManifest]:
    metadata_path = bundle / BACKUP_METADATA_NAME
    manifest_path = bundle / BACKUP_MANIFEST_NAME
    archive_path = bundle / ARCHIVE_NAME
    for path in (metadata_path, manifest_path, archive_path):
        if not path.is_file():
            raise DeployError(f"Complete backup file not found: {path.name}")

    metadata_raw = read_utf8(metadata_path, BACKUP_METADATA_NAME)
    manifest_raw = read_utf8(manifest_path, BACKUP_MANIFEST_NAME)
    metadata = backup_metadata_from_json(metadata_raw)
    manifest = backup_manifest_from_json(manifest_raw)
    if metadata_raw != backup_metadata_to_json(metadata):
        raise DeployError("backup.json is not canonical JSON.")
    if manifest_raw != backup_manifest_to_json(manifest):
        raise DeployError("manifest.json is not canonical JSON.")
    if metadata.backup_id != manifest.backup_id:
        raise DeployError("Backup metadata and manifest identifiers do not match.")

    manifest_bytes = manifest_raw.encode("utf-8")
    if sha256_bytes(manifest_bytes) != metadata.manifest.sha256:
        raise DeployError("Backup manifest checksum does not match backup metadata.")
    archive_size, archive_sha256 = file_size_and_sha256(archive_path)
    if archive_size != metadata.archive.size:
        raise DeployError("Backup archive size does not match backup metadata.")
    if archive_sha256 != metadata.archive.sha256:
        raise DeployError("Backup archive checksum does not match backup metadata.")

    validate_backup_archive(archive_path, manifest)
    return metadata, manifest


def validate_backup_archive(archive_path: Path, manifest: BackupManifest) -> None:
    expected = {entry.path: entry for entry in manifest.entries}
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            actual: dict[str, tarfile.TarInfo] = {}
            for member in members:
                validate_backup_entry_path(member.name, "Backup archive entry path")
                if member.name in actual:
                    raise DeployError(f"Backup archive contains duplicate path: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise DeployError(f"Backup archive contains unsupported entry: {member.name}")
                actual[member.name] = member

            if set(actual) != set(expected):
                raise DeployError("Backup archive entries do not match manifest entries.")

            for path, entry in expected.items():
                validate_archive_member(archive, actual[path], entry)
    except (tarfile.TarError, OSError) as error:
        raise DeployError(f"Backup archive is not a readable gzip tar archive: {error}") from error


def validate_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    entry: BackupEntry,
) -> None:
    path = entry.path
    if member.mode & MAX_PERMISSION_MODE != entry.mode:
        raise DeployError(f"Backup archive mode does not match manifest: {path}")
    if isinstance(entry, BackupDirectoryEntry):
        if not member.isdir():
            raise DeployError(f"Backup archive type does not match manifest: {path}")
        return
    if not member.isfile() or member.size != entry.size:
        raise DeployError(f"Backup archive file metadata does not match manifest: {path}")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise DeployError(f"Backup archive file is not readable: {path}")
    digest = hashlib.sha256()
    for chunk in iter(lambda file=extracted: file.read(1024 * 1024), b""):
        digest.update(chunk)
    if digest.hexdigest() != entry.sha256:
        raise DeployError(f"Backup archive checksum does not match manifest: {path}")


def select_retention_deletions(
    backups: Sequence[BackupMetadata],
    retention: int,
    *,
    baseline_id: str | None,
    latest_id: str | None,
    active_backup_ids: Collection[str] = (),
) -> tuple[str, ...]:
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 1:
        raise DeployError("Backup retention must be a positive integer.")
    by_id: dict[str, BackupMetadata] = {}
    for backup in backups:
        validate_backup_metadata(backup)
        if backup.backup_id in by_id:
            raise DeployError(f"Duplicate backup identifier: {backup.backup_id}")
        by_id[backup.backup_id] = backup

    referenced_ids = {
        identifier
        for identifier in (baseline_id, latest_id, *active_backup_ids)
        if identifier is not None
    }
    for identifier in referenced_ids:
        validate_identifier(identifier, "Referenced backup identifier")
        if identifier not in by_id:
            raise DeployError(f"Referenced backup does not exist: {identifier}")
    if baseline_id is not None and by_id[baseline_id].kind != "baseline":
        raise DeployError("The baseline pointer must reference a baseline backup.")

    non_protected = sorted(
        (backup for backup in backups if not backup.protected),
        key=lambda item: (item.created_at, item.backup_id),
        reverse=True,
    )
    kept_ids = {backup.backup_id for backup in non_protected[:retention]}
    kept_ids.update(referenced_ids)
    deletions = [backup for backup in non_protected if backup.backup_id not in kept_ids]
    deletions.sort(key=lambda item: (item.created_at, item.backup_id))
    return tuple(backup.backup_id for backup in deletions)


def classify_namespace_paths(paths: Iterable[str]) -> NamespaceClassification:
    state_v2_present = False
    legacy_roots: set[str] = set()
    for path in paths:
        validate_backup_entry_path(path, "Namespace path")
        if path == CURRENT_STATE_ROOT or path.startswith(f"{CURRENT_STATE_ROOT}/"):
            state_v2_present = True
        root = path.split("/", 1)[0]
        if root in LEGACY_STATE_ROOTS:
            legacy_roots.add(root)
    return NamespaceClassification(
        state_v2_present=state_v2_present,
        legacy_roots=tuple(sorted(legacy_roots)),
    )


def validate_identifier(value: str, name: str = "Identifier") -> None:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise DeployError(f"{name} is unsafe: {value!r}")


def validate_scope_targets(scopes: Sequence[BackupScope]) -> None:
    if not scopes:
        raise DeployError("Backup manifest must contain at least one scope.")
    targets: dict[str, str] = {}
    for scope in scopes:
        validate_line_safe(scope.name, "Backup scope name")
        if not scope.name:
            raise DeployError("Backup scope name must be non-empty.")
        validate_backup_entry_path(scope.target, "Backup scope target", allow_dot=True)
        if scope.name in targets:
            raise DeployError(f"Backup manifest contains duplicate scope: {scope.name}")
        targets[scope.name] = scope.target

    ordered = list(scopes)
    for index, scope in enumerate(ordered):
        for previous in ordered[:index]:
            if paths_overlap(scope.target, previous.target):
                raise DeployError(
                    f"Backup scope targets must not overlap: {previous.target} and {scope.target}"
                )


def validate_layout(layout: Layout) -> None:
    if isinstance(layout.layout_version, bool) or layout.layout_version != LAYOUT_VERSION:
        raise DeployError(f"Unsupported complete backup layout version: {layout.layout_version!r}")


def validate_backup_metadata(metadata: BackupMetadata) -> None:
    if (
        isinstance(metadata.schema_version, bool)
        or metadata.schema_version != BACKUP_SCHEMA_VERSION
    ):
        raise DeployError(f"Unsupported backup.json schema version: {metadata.schema_version!r}")
    validate_identifier(metadata.backup_id, "Backup identifier")
    if metadata.kind not in ("baseline", "pre_apply", "pre_rollback"):
        raise DeployError(f"Unsupported backup kind: {metadata.kind!r}")
    validate_utc_timestamp(metadata.created_at)
    validate_line_safe(metadata.project, "Backup project")
    if not metadata.project:
        raise DeployError("Backup project must be non-empty.")
    validate_absolute_root(metadata.remote_root)
    validate_sha256(metadata.scope_config_digest, "Scope configuration digest")
    if not isinstance(metadata.protected, bool):
        raise DeployError("Backup protected must be a boolean.")
    if metadata.archive.path != ARCHIVE_NAME:
        raise DeployError(f"Backup archive path must be {ARCHIVE_NAME}.")
    if (
        not isinstance(metadata.archive.size, int)
        or isinstance(metadata.archive.size, bool)
        or metadata.archive.size <= 0
    ):
        raise DeployError("Backup archive size must be a positive integer.")
    validate_sha256(metadata.archive.sha256, "Backup archive digest")
    if metadata.manifest.path != BACKUP_MANIFEST_NAME:
        raise DeployError(f"Backup manifest path must be {BACKUP_MANIFEST_NAME}.")
    validate_sha256(metadata.manifest.sha256, "Backup manifest digest")

    if metadata.kind == "baseline":
        if not metadata.protected or metadata.source is not None:
            raise DeployError("A baseline backup must be protected and omit source metadata.")
    elif metadata.protected or metadata.source is None:
        raise DeployError(
            "A pre-apply or pre-rollback backup must be unprotected and contain source metadata."
        )
    if metadata.source is not None:
        validate_identifier(metadata.source.operation_id, "Backup operation identifier")
        validate_identifier(metadata.source.release_id, "Backup release identifier")
        validate_sha256(metadata.source.artifact_sha256, "Backup artifact digest")


def validate_backup_manifest(manifest: BackupManifest) -> None:
    if (
        isinstance(manifest.schema_version, bool)
        or manifest.schema_version != BACKUP_MANIFEST_SCHEMA_VERSION
    ):
        raise DeployError(f"Unsupported manifest.json schema version: {manifest.schema_version!r}")
    validate_identifier(manifest.backup_id, "Backup manifest identifier")
    validate_scope_targets(manifest.scopes)
    if tuple(sorted(manifest.scopes, key=lambda item: item.name)) != manifest.scopes:
        raise DeployError("Backup manifest scopes must be ordered by name.")

    scope_targets = {scope.name: scope.target for scope in manifest.scopes}
    directory_paths = {
        entry.path for entry in manifest.entries if isinstance(entry, BackupDirectoryEntry)
    }
    seen_paths: set[str] = set()
    previous_path: str | None = None
    for entry in manifest.entries:
        validate_backup_entry_path(entry.path, "Backup manifest entry path")
        validate_line_safe(entry.scope, "Backup manifest entry scope")
        target = scope_targets.get(entry.scope)
        if target is None:
            raise DeployError(f"Backup manifest entry has an unknown scope: {entry.scope}")
        if entry.path in seen_paths:
            raise DeployError(f"Backup manifest contains duplicate path: {entry.path}")
        if previous_path is not None and entry.path < previous_path:
            raise DeployError("Backup manifest entries must be ordered by path.")
        seen_paths.add(entry.path)
        previous_path = entry.path
        if not path_in_target(entry.path, target):
            raise DeployError(f"Backup manifest entry is outside its scope: {entry.path}")
        validate_entry_type(entry)
        if entry.path == target and isinstance(entry, BackupFileEntry):
            raise DeployError(f"Backup scope target must be a directory: {entry.path}")
        for parent in required_directory_paths(entry.path, target):
            if parent not in directory_paths:
                raise DeployError(f"Backup manifest is missing parent directory: {parent}")
        validate_mode(entry.mode)
        if isinstance(entry, BackupFileEntry):
            if not isinstance(entry.size, int) or isinstance(entry.size, bool) or entry.size < 0:
                raise DeployError(f"Backup file size must be non-negative: {entry.path}")
            validate_sha256(entry.sha256, f"Backup file digest for {entry.path}")


def validate_entry_type(entry: BackupEntry) -> None:
    if isinstance(entry, BackupFileEntry) and entry.type != "file":
        raise DeployError(f"Backup file entry has an invalid type: {entry.path}")
    if isinstance(entry, BackupDirectoryEntry) and entry.type != "directory":
        raise DeployError(f"Backup directory entry has an invalid type: {entry.path}")


def required_directory_paths(path: str, target: str) -> tuple[str, ...]:
    path_parts = PurePosixPath(path).parts
    target_parts = () if target == "." else PurePosixPath(target).parts
    return tuple(
        PurePosixPath(*path_parts[:length]).as_posix()
        for length in range(max(1, len(target_parts)), len(path_parts))
    )


def backup_metadata_payload(metadata: BackupMetadata) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": metadata.schema_version,
        "backup_id": metadata.backup_id,
        "kind": metadata.kind,
        "created_at": metadata.created_at,
        "project": metadata.project,
        "remote_root": metadata.remote_root,
        "scope_config_digest": metadata.scope_config_digest,
        "protected": metadata.protected,
        "archive": {
            "path": metadata.archive.path,
            "size": metadata.archive.size,
            "sha256": metadata.archive.sha256,
        },
        "manifest": {
            "path": metadata.manifest.path,
            "sha256": metadata.manifest.sha256,
        },
    }
    if metadata.source is not None:
        payload["source"] = {
            "operation_id": metadata.source.operation_id,
            "release_id": metadata.source.release_id,
            "artifact_sha256": metadata.source.artifact_sha256,
        }
    return payload


def backup_manifest_payload(manifest: BackupManifest) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for entry in manifest.entries:
        payload: dict[str, object] = {
            "path": entry.path,
            "scope": entry.scope,
            "type": entry.type,
            "mode": entry.mode,
        }
        if isinstance(entry, BackupFileEntry):
            payload["size"] = entry.size
            payload["sha256"] = entry.sha256
        entries.append(payload)
    return {
        "schema_version": manifest.schema_version,
        "backup_id": manifest.backup_id,
        "scopes": [{"name": scope.name, "target": scope.target} for scope in manifest.scopes],
        "entries": entries,
    }


def parse_scope(raw: object, index: int) -> BackupScope:
    context = f"manifest.json scopes[{index}]"
    payload = require_object(raw, context)
    require_exact_keys(payload, {"name", "target"}, set(), context)
    return BackupScope(
        name=require_str(payload, "name", context),
        target=require_str(payload, "target", context),
    )


def parse_entry(raw: object, index: int) -> BackupEntry:
    context = f"manifest.json entries[{index}]"
    payload = require_object(raw, context)
    entry_type = require_str(payload, "type", context)
    common = {"path", "scope", "type", "mode"}
    if entry_type == "file":
        require_exact_keys(payload, common | {"size", "sha256"}, set(), context)
        return BackupFileEntry(
            path=require_str(payload, "path", context),
            scope=require_str(payload, "scope", context),
            type="file",
            mode=require_int(payload, "mode", context),
            size=require_int(payload, "size", context),
            sha256=require_str(payload, "sha256", context),
        )
    if entry_type == "directory":
        require_exact_keys(payload, common, set(), context)
        return BackupDirectoryEntry(
            path=require_str(payload, "path", context),
            scope=require_str(payload, "scope", context),
            type="directory",
            mode=require_int(payload, "mode", context),
        )
    raise DeployError(f"{context} has an unsupported type: {entry_type!r}")


def strict_json_object(raw: str, name: str) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise DeployError(f"{name} contains a duplicate key: {key}")
            payload[key] = value
        return payload

    def reject_non_json_constant(value: str) -> object:
        raise DeployError(f"{name} contains a non-JSON numeric value: {value}")

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_json_constant,
        )
    except json.JSONDecodeError as error:
        raise DeployError(f"{name} is not valid JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise DeployError(f"{name} must contain an object.")
    return cast(dict[str, object], payload)


def require_exact_keys(
    payload: dict[str, object],
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    missing = sorted(required - set(payload))
    if missing:
        raise DeployError(f"{context} is missing required field: {missing[0]}")
    unknown = sorted(set(payload) - required - optional)
    if unknown:
        raise DeployError(f"{context} contains unsupported field: {unknown[0]}")


def require_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DeployError(f"{context} must be an object.")
    return cast(dict[str, object], value)


def require_mapping(payload: dict[str, object], key: str, context: str) -> dict[str, object]:
    return require_object(payload.get(key), f"{context} {key}")


def require_list(payload: dict[str, object], key: str, context: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise DeployError(f"{context} {key} must be an array.")
    return cast(list[object], value)


def require_str(payload: dict[str, object], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DeployError(f"{context} {key} must be a non-empty string.")
    return value


def require_int(payload: dict[str, object], key: str, context: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DeployError(f"{context} {key} must be an integer.")
    return value


def require_bool(payload: dict[str, object], key: str, context: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise DeployError(f"{context} {key} must be a boolean.")
    return value


def require_backup_kind(payload: dict[str, object]) -> BackupKind:
    value = require_str(payload, "kind", BACKUP_METADATA_NAME)
    if value not in ("baseline", "pre_apply", "pre_rollback"):
        raise DeployError(f"backup.json has an unsupported kind: {value!r}")
    return cast(BackupKind, value)


def validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise DeployError(f"{name} must be a lowercase hexadecimal SHA-256 digest.")


def validate_utc_timestamp(value: str) -> None:
    if not UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise DeployError("Backup created_at must use YYYY-MM-DDTHH:MM:SSZ UTC format.")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise DeployError("Backup created_at is not a valid UTC timestamp.") from error


def validate_mode(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_PERMISSION_MODE
    ):
        raise DeployError(f"Backup entry mode is outside permission-bit range: {value!r}")


def validate_absolute_root(value: str) -> None:
    validate_line_safe(value, "Backup remote root")
    if not value.startswith("/") or value == "/" or "\\" in value:
        raise DeployError("Backup remote root must be a safe absolute path other than /.")
    parts = PurePosixPath(value).parts
    if "." in parts or ".." in parts or PurePosixPath(value).as_posix() != value:
        raise DeployError("Backup remote root must be a normalized absolute path.")


def validate_backup_entry_path(value: str, name: str, *, allow_dot: bool = False) -> None:
    validate_line_safe(value, name)
    if value == "." and allow_dot:
        return
    if (
        not value
        or value == "."
        or value.startswith("/")
        or value.startswith("./")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
    ):
        raise DeployError(f"{name} must be a normalized relative POSIX path: {value!r}")
    parts = PurePosixPath(value).parts
    if "." in parts or ".." in parts or PurePosixPath(value).as_posix() != value:
        raise DeployError(f"{name} must be a normalized relative POSIX path: {value!r}")


def validate_line_safe(value: str, name: str) -> None:
    if any(
        ord(character) in ASCII_CONTROL_CODES
        or MIN_SURROGATE_CODE_POINT <= ord(character) <= MAX_SURROGATE_CODE_POINT
        for character in value
    ):
        raise DeployError(f"{name} contains control characters.")


def paths_overlap(first: str, second: str) -> bool:
    return path_in_target(first, second) or path_in_target(second, first)


def path_in_target(path: str, target: str) -> bool:
    return target in (".", path) or path.startswith(f"{target}/")


def read_utf8(path: Path, name: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DeployError(f"{name} is not readable UTF-8: {error}") from error


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise DeployError(f"Complete backup archive is not readable: {error}") from error
    return size, digest.hexdigest()
