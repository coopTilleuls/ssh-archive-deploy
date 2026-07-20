from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from ssh_archive_deploy.complete_backup import (
    BACKUP_MANIFEST_SCHEMA_VERSION,
    BACKUP_SCHEMA_VERSION,
    LAYOUT_VERSION,
    BackupArchive,
    BackupDirectoryEntry,
    BackupFileEntry,
    BackupKind,
    BackupManifest,
    BackupManifestReference,
    BackupMetadata,
    BackupScope,
    BackupSource,
    Layout,
    backup_manifest_from_json,
    backup_manifest_to_json,
    backup_metadata_from_json,
    backup_metadata_to_json,
    classify_namespace_paths,
    layout_from_json,
    layout_to_json,
    scope_config_digest,
    select_retention_deletions,
    validate_complete_backup_bundle,
)
from ssh_archive_deploy.config import parse_config
from ssh_archive_deploy.errors import DeployError

FIXTURE_DIR = Path(__file__).parent / "fixtures/complete-backup"
ZERO_DIGEST = "0" * 64
ONE_DIGEST = "1" * 64


def sample_source() -> BackupSource:
    return BackupSource(
        operation_id="apply-20260720",
        release_id="release-20260720",
        artifact_sha256=ONE_DIGEST,
    )


def sample_metadata(
    backup_id: str = "backup-20260720",
    *,
    kind: str = "pre_apply",
    created_at: str = "2026-07-20T12:34:56Z",
    protected: bool = False,
) -> BackupMetadata:
    return BackupMetadata(
        schema_version=BACKUP_SCHEMA_VERSION,
        backup_id=backup_id,
        kind=cast(BackupKind, kind),
        created_at=created_at,
        project="demo",
        remote_root="/srv/www/demo",
        scope_config_digest=ZERO_DIGEST,
        protected=protected,
        archive=BackupArchive(path="archive.tar.gz", size=123, sha256=ZERO_DIGEST),
        manifest=BackupManifestReference(path="manifest.json", sha256=ONE_DIGEST),
        source=None if kind == "baseline" else sample_source(),
    )


def sample_manifest(*, empty: bool = False) -> BackupManifest:
    entries = ()
    if not empty:
        entries = (
            BackupDirectoryEntry(
                path="wp-content/themes/example",
                scope="theme",
                type="directory",
                mode=0o755,
            ),
            BackupFileEntry(
                path="wp-content/themes/example/index.php",
                scope="theme",
                type="file",
                mode=0o640,
                size=5,
                sha256=ZERO_DIGEST,
            ),
        )
    return BackupManifest(
        schema_version=BACKUP_MANIFEST_SCHEMA_VERSION,
        backup_id="backup-20260720",
        scopes=(BackupScope(name="theme", target="wp-content/themes/example"),),
        entries=entries,
    )


def update_bundle_metadata(bundle: Path) -> None:
    metadata_path = bundle / "backup.json"
    manifest_path = bundle / "manifest.json"
    archive_path = bundle / "archive.tar.gz"
    metadata = backup_metadata_from_json(metadata_path.read_text())
    manifest_bytes = manifest_path.read_bytes()
    archive_bytes = archive_path.read_bytes()
    metadata_path.write_text(
        backup_metadata_to_json(
            replace(
                metadata,
                archive=replace(
                    metadata.archive,
                    size=len(archive_bytes),
                    sha256=hashlib.sha256(archive_bytes).hexdigest(),
                ),
                manifest=replace(
                    metadata.manifest,
                    sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                ),
            )
        )
    )


def test_layout_round_trip_is_canonical_and_strict() -> None:
    raw = layout_to_json(Layout())

    assert raw == '{"layout_version":2}'
    assert layout_from_json(raw) == Layout(layout_version=LAYOUT_VERSION)

    with pytest.raises(DeployError, match="Unsupported.*version"):
        layout_from_json('{"layout_version":3}')
    with pytest.raises(DeployError, match="unsupported field"):
        layout_from_json('{"layout_version":2,"unknown":true}')
    with pytest.raises(DeployError, match="duplicate key"):
        layout_from_json('{"layout_version":2,"layout_version":2}')
    with pytest.raises(DeployError, match="non-JSON numeric"):
        layout_from_json('{"layout_version":NaN}')


def test_backup_metadata_round_trip_is_canonical() -> None:
    metadata = sample_metadata()

    raw = backup_metadata_to_json(metadata)

    assert raw == json.dumps(
        json.loads(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert not raw.endswith("\n")
    assert backup_metadata_from_json(raw) == metadata


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (sample_metadata("../escape"), "unsafe"),
        (replace(sample_metadata(), created_at="2026-07-20T12:34:56+00:00"), "UTC format"),
        (replace(sample_metadata(), scope_config_digest="A" * 64), "lowercase"),
        (
            replace(sample_metadata(), archive=BackupArchive("other.tar.gz", 123, ZERO_DIGEST)),
            "archive path",
        ),
        (replace(sample_metadata(), source=None), "contain source"),
        (sample_metadata(kind="baseline", protected=False), "must be protected"),
    ],
)
def test_backup_metadata_rejects_invalid_contract(
    metadata: BackupMetadata,
    message: str,
) -> None:
    with pytest.raises(DeployError, match=message):
        backup_metadata_to_json(metadata)


def test_backup_metadata_rejects_unknown_version_and_fields() -> None:
    raw = json.loads(backup_metadata_to_json(sample_metadata()))
    raw["schema_version"] = 2
    with pytest.raises(DeployError, match="Unsupported backup.json schema"):
        backup_metadata_from_json(json.dumps(raw))

    raw["schema_version"] = 1
    raw["unexpected"] = True
    with pytest.raises(DeployError, match="unsupported field"):
        backup_metadata_from_json(json.dumps(raw))

    baseline = sample_metadata("baseline", kind="baseline", protected=True)
    raw = json.loads(backup_metadata_to_json(baseline))
    raw["source"] = None
    with pytest.raises(DeployError, match="source must be an object"):
        backup_metadata_from_json(json.dumps(raw))


def test_backup_manifest_round_trip_supports_empty_scope() -> None:
    manifest = sample_manifest(empty=True)

    raw = backup_manifest_to_json(manifest)

    assert not raw.endswith("\n")
    assert backup_manifest_from_json(raw) == manifest


def test_backup_manifest_rejects_unknown_version_and_fields() -> None:
    raw = json.loads(backup_manifest_to_json(sample_manifest()))
    raw["schema_version"] = 2
    with pytest.raises(DeployError, match="Unsupported manifest.json schema"):
        backup_manifest_from_json(json.dumps(raw))

    raw["schema_version"] = 1
    raw["unexpected"] = True
    with pytest.raises(DeployError, match="unsupported field"):
        backup_manifest_from_json(json.dumps(raw))


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (
            replace(
                sample_manifest(),
                scopes=(
                    BackupScope(name="parent", target="wp-content"),
                    BackupScope(name="theme", target="wp-content/themes/example"),
                ),
            ),
            "must not overlap",
        ),
        (
            replace(
                sample_manifest(),
                entries=tuple(reversed(sample_manifest().entries)),
            ),
            "ordered by path",
        ),
        (
            replace(
                sample_manifest(),
                entries=(replace(sample_manifest().entries[1], path="../escape"),),
            ),
            "normalized relative",
        ),
        (
            replace(
                sample_manifest(),
                entries=(
                    sample_manifest().entries[0],
                    replace(sample_manifest().entries[1], mode=0o10000),
                ),
            ),
            "permission-bit range",
        ),
        (
            replace(
                sample_manifest(),
                entries=(replace(sample_manifest().entries[1], scope="unknown"),),
            ),
            "unknown scope",
        ),
        (
            replace(
                sample_manifest(),
                entries=(sample_manifest().entries[0], sample_manifest().entries[0]),
            ),
            "duplicate path",
        ),
        (
            replace(
                sample_manifest(),
                entries=(sample_manifest().entries[1],),
            ),
            "missing parent directory",
        ),
    ],
)
def test_backup_manifest_rejects_invalid_contract(
    manifest: BackupManifest,
    message: str,
) -> None:
    with pytest.raises(DeployError, match=message):
        backup_manifest_to_json(manifest)


def test_manifest_parser_rejects_specialized_entry_fields() -> None:
    raw = json.loads(backup_manifest_to_json(sample_manifest()))
    raw["entries"][0]["sha256"] = ZERO_DIGEST
    with pytest.raises(DeployError, match="unsupported field"):
        backup_manifest_from_json(json.dumps(raw))

    raw = json.loads(backup_manifest_to_json(sample_manifest()))
    raw["entries"][1]["type"] = "symlink"
    with pytest.raises(DeployError, match="unsupported type"):
        backup_manifest_from_json(json.dumps(raw))


def test_scope_config_digest_ignores_sources_and_order() -> None:
    first = parse_config(
        {
            "version": 2,
            "project": "demo",
            "remote": {"root": "/srv/www", "workdir": "/srv/deploy"},
            "exclude": ["*.tmp", "cache/**"],
            "scope": [
                {
                    "name": "theme",
                    "source": "local/theme",
                    "target": "wp-content/themes/demo",
                    "include": ["*.php", "assets/**"],
                    "exclude": ["vendor/tests/**", "*.log"],
                }
            ],
        }
    )
    second = parse_config(
        {
            "version": 2,
            "project": "other-project",
            "remote": {"root": "/other/root", "workdir": "/other/workdir"},
            "exclude": ["cache/**", "*.tmp"],
            "scope": [
                {
                    "name": "theme",
                    "source": "different/source",
                    "target": "wp-content/themes/demo",
                    "include": ["assets/**", "*.php"],
                    "exclude": ["*.log", "vendor/tests/**"],
                }
            ],
        }
    )

    assert scope_config_digest(first) == scope_config_digest(second)


def test_golden_complete_backup_bundle_is_valid() -> None:
    metadata, manifest = validate_complete_backup_bundle(FIXTURE_DIR)

    assert metadata.kind == "baseline"
    assert [scope.name for scope in manifest.scopes] == ["catalogue", "theme"]
    assert "catalogue/data.bin" in [entry.path for entry in manifest.entries]
    assert "catalogue/empty" in [entry.path for entry in manifest.entries]
    assert "wp-content/themes/example/remote-only.txt" in [entry.path for entry in manifest.entries]
    assert [entry.mode for entry in manifest.entries] == [
        0o755,
        0o640,
        0o750,
        0o755,
        0o600,
        0o644,
    ]


@pytest.mark.parametrize("tampered_file", ["backup.json", "manifest.json", "archive.tar.gz"])
def test_complete_backup_bundle_detects_tampering(tmp_path: Path, tampered_file: str) -> None:
    bundle = tmp_path / "backup"
    shutil.copytree(FIXTURE_DIR, bundle)
    path = bundle / tampered_file
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(DeployError, match="canonical|size|checksum|JSON"):
        validate_complete_backup_bundle(bundle)


def test_complete_backup_bundle_detects_manifest_archive_divergence(tmp_path: Path) -> None:
    bundle = tmp_path / "backup"
    shutil.copytree(FIXTURE_DIR, bundle)
    archive_path = bundle / "archive.tar.gz"
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    (extracted / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(extracted.rglob("*")):
            archive.add(
                path,
                arcname=path.relative_to(extracted).as_posix(),
                recursive=False,
            )

    update_bundle_metadata(bundle)

    with pytest.raises(DeployError, match="entries do not match"):
        validate_complete_backup_bundle(bundle)


def test_complete_backup_bundle_detects_identifier_divergence(tmp_path: Path) -> None:
    bundle = tmp_path / "backup"
    shutil.copytree(FIXTURE_DIR, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = backup_manifest_from_json(manifest_path.read_text())
    manifest_path.write_text(
        backup_manifest_to_json(replace(manifest, backup_id="different-backup"))
    )
    update_bundle_metadata(bundle)

    with pytest.raises(DeployError, match="identifiers do not match"):
        validate_complete_backup_bundle(bundle)


def test_complete_backup_bundle_detects_mode_divergence(tmp_path: Path) -> None:
    bundle = tmp_path / "backup"
    shutil.copytree(FIXTURE_DIR, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = backup_manifest_from_json(manifest_path.read_text())
    manifest_path.write_text(
        backup_manifest_to_json(
            replace(
                manifest,
                entries=(replace(manifest.entries[0], mode=0o700), *manifest.entries[1:]),
            )
        )
    )
    update_bundle_metadata(bundle)

    with pytest.raises(DeployError, match="mode does not match"):
        validate_complete_backup_bundle(bundle)


@pytest.mark.parametrize("entry_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_complete_backup_bundle_rejects_special_archive_entry(
    tmp_path: Path,
    entry_type: bytes,
) -> None:
    bundle = tmp_path / "backup"
    shutil.copytree(FIXTURE_DIR, bundle)
    archive_path = bundle / "archive.tar.gz"
    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        special = tarfile.TarInfo("catalogue")
        special.type = entry_type
        special.linkname = "outside"
        archive.addfile(special)
    update_bundle_metadata(bundle)

    with pytest.raises(DeployError, match="unsupported entry"):
        validate_complete_backup_bundle(bundle)


def test_complete_backup_bundle_supports_empty_scope(tmp_path: Path) -> None:
    bundle = tmp_path / "backup"
    shutil.copytree(FIXTURE_DIR, bundle)
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        backup_manifest_to_json(
            BackupManifest(
                schema_version=BACKUP_MANIFEST_SCHEMA_VERSION,
                backup_id="baseline-fixture",
                scopes=(BackupScope(name="empty", target="empty"),),
                entries=(),
            )
        )
    )
    with tarfile.open(bundle / "archive.tar.gz", "w:gz", format=tarfile.PAX_FORMAT):
        pass
    update_bundle_metadata(bundle)

    _, manifest = validate_complete_backup_bundle(bundle)

    assert manifest.entries == ()


def test_retention_keeps_baseline_latest_active_and_newest() -> None:
    backups = [
        sample_metadata(
            "baseline",
            kind="baseline",
            protected=True,
            created_at="2026-07-01T00:00:00Z",
        ),
        sample_metadata("old", created_at="2026-07-02T00:00:00Z"),
        sample_metadata("latest", created_at="2026-07-03T00:00:00Z"),
        sample_metadata("active", created_at="2026-07-04T00:00:00Z"),
        sample_metadata("newest", created_at="2026-07-05T00:00:00Z"),
    ]

    deletions = select_retention_deletions(
        backups,
        1,
        baseline_id="baseline",
        latest_id="latest",
        active_backup_ids={"active"},
    )

    assert deletions == ("old",)


def test_retention_uses_created_at_then_identifier() -> None:
    backups = [
        sample_metadata("same-a", created_at="2026-07-05T00:00:00Z"),
        sample_metadata("same-b", created_at="2026-07-05T00:00:00Z"),
        sample_metadata("older", created_at="2026-07-04T00:00:00Z"),
    ]

    assert select_retention_deletions(
        backups,
        1,
        baseline_id=None,
        latest_id=None,
    ) == ("older", "same-a")


def test_retention_rejects_duplicate_or_missing_references() -> None:
    duplicate = sample_metadata("duplicate")
    with pytest.raises(DeployError, match="Duplicate"):
        select_retention_deletions(
            [duplicate, duplicate],
            1,
            baseline_id=None,
            latest_id=None,
        )
    with pytest.raises(DeployError, match="does not exist"):
        select_retention_deletions(
            [sample_metadata()],
            1,
            baseline_id=None,
            latest_id="missing",
        )
    with pytest.raises(DeployError, match="must reference a baseline"):
        select_retention_deletions(
            [sample_metadata("not-a-baseline")],
            1,
            baseline_id="not-a-baseline",
            latest_id=None,
        )


def test_namespace_classification_ignores_consumer_state() -> None:
    classification = classify_namespace_paths(
        [
            "backups/historical.tar.gz",
            "stage/release",
            "incoming/archive.tar.gz",
            "transactions/legacy-id",
            "pointers/latest",
            "ssh-archive-deploy/state-v2/layout.json",
        ]
    )

    assert classification.state_v2_present is True
    assert classification.legacy_roots == ("pointers", "transactions")
