from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from ssh_archive_deploy.complete_backup import (
    BACKUP_MANIFEST_NAME,
    BACKUP_MANIFEST_SCHEMA_VERSION,
    BACKUP_METADATA_NAME,
    BACKUP_SCHEMA_VERSION,
    BackupArchive,
    BackupDirectoryEntry,
    BackupFileEntry,
    BackupManifest,
    BackupManifestReference,
    BackupMetadata,
    BackupScope,
    backup_manifest_to_json,
    backup_metadata_to_json,
)

FIXTURE_DIR = Path(__file__).parents[1] / "tests/fixtures/complete-backup"
ARCHIVE_PATH = FIXTURE_DIR / "archive.tar.gz"

CONTENTS = {
    "catalogue/data.bin": b"\x00\x01\x02\xffcomplete-backup\x00",
    "wp-content/themes/example/remote-only.txt": b"server-side content\n",
    "wp-content/themes/example/style.css": b"body { color: #123456; }\n",
}


def file_entry(path: str, scope: str, mode: int) -> BackupFileEntry:
    content = CONTENTS[path]
    return BackupFileEntry(
        path=path,
        scope=scope,
        type="file",
        mode=mode,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def fixture_manifest() -> BackupManifest:
    return BackupManifest(
        schema_version=BACKUP_MANIFEST_SCHEMA_VERSION,
        backup_id="baseline-fixture",
        scopes=(
            BackupScope(name="catalogue", target="catalogue"),
            BackupScope(name="theme", target="wp-content/themes/example"),
        ),
        entries=(
            BackupDirectoryEntry(
                path="catalogue",
                scope="catalogue",
                type="directory",
                mode=0o755,
            ),
            file_entry("catalogue/data.bin", "catalogue", 0o640),
            BackupDirectoryEntry(
                path="catalogue/empty",
                scope="catalogue",
                type="directory",
                mode=0o750,
            ),
            BackupDirectoryEntry(
                path="wp-content/themes/example",
                scope="theme",
                type="directory",
                mode=0o755,
            ),
            file_entry("wp-content/themes/example/remote-only.txt", "theme", 0o600),
            file_entry("wp-content/themes/example/style.css", "theme", 0o644),
        ),
    )


def tar_info(path: str, mode: int, *, directory: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = mode
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def write_archive(manifest: BackupManifest) -> None:
    with (
        ARCHIVE_PATH.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for entry in manifest.entries:
            if isinstance(entry, BackupDirectoryEntry):
                archive.addfile(tar_info(entry.path, entry.mode, directory=True))
            else:
                content = CONTENTS[entry.path]
                archive.addfile(
                    tar_info(
                        entry.path,
                        entry.mode,
                        directory=False,
                        size=len(content),
                    ),
                    io.BytesIO(content),
                )


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = fixture_manifest()
    manifest_json = backup_manifest_to_json(manifest)
    (FIXTURE_DIR / BACKUP_MANIFEST_NAME).write_text(manifest_json, encoding="utf-8")
    write_archive(manifest)
    archive_bytes = ARCHIVE_PATH.read_bytes()
    metadata = BackupMetadata(
        schema_version=BACKUP_SCHEMA_VERSION,
        backup_id=manifest.backup_id,
        kind="baseline",
        created_at="2026-07-20T12:34:56Z",
        project="fixture-project",
        remote_root="/srv/www/fixture",
        scope_config_digest=hashlib.sha256(b"fixture-scope-config").hexdigest(),
        protected=True,
        archive=BackupArchive(
            path=ARCHIVE_PATH.name,
            size=len(archive_bytes),
            sha256=hashlib.sha256(archive_bytes).hexdigest(),
        ),
        manifest=BackupManifestReference(
            path=BACKUP_MANIFEST_NAME,
            sha256=hashlib.sha256(manifest_json.encode()).hexdigest(),
        ),
    )
    (FIXTURE_DIR / BACKUP_METADATA_NAME).write_text(
        backup_metadata_to_json(metadata),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
