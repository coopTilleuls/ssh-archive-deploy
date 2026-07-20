# Complete Backup Format

SSH Archive Deploy defines a versioned complete-backup format for the next
mutable deployment engine. The Python library can parse, serialize, and validate
this format, but the current `apply` and `rollback` commands do not create or
restore it yet. They continue to use the transaction and checkpoint format
documented in [Configuration](configuration.md#apply-and-rollback-contract).

This separation allows consumers and reviewers to inspect the storage contract
before it is connected to remote mutations.

## Backup Directory

Each backup is self-contained:

```text
<backup_id>/
├── backup.json
├── manifest.json
└── archive.tar.gz
```

The future remote engine owns a versioned namespace below the configured
workdir:

```text
<remote.workdir>/ssh-archive-deploy/state-v2/
├── layout.json
├── backups/<backup_id>/
├── pointers/
│   ├── baseline
│   └── latest
└── staging/
```

`layout.json` contains exactly `{"layout_version":2}`. Unknown layout or
document versions are rejected. The implementation does not migrate unversioned
transaction, baseline, or pointer state into this namespace.

## Canonical JSON

`layout.json`, `backup.json`, and `manifest.json` use canonical UTF-8 JSON:

- object keys are sorted;
- arrays use the order required by their schema;
- non-ASCII characters are stored directly;
- duplicate keys, unknown fields, non-JSON numeric values, insignificant
  whitespace, and trailing newlines are rejected for stored bundles.

Digests use lowercase hexadecimal SHA-256. Identifiers used in paths must match
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.

## Backup Metadata

`backup.json` schema version 1 records the stored archive and manifest rather
than relying on filenames inferred by a shell script:

```json
{
  "archive": {
    "path": "archive.tar.gz",
    "sha256": "<sha256>",
    "size": 123
  },
  "backup_id": "apply-20260720-123456",
  "created_at": "2026-07-20T12:34:56Z",
  "kind": "pre_apply",
  "manifest": {
    "path": "manifest.json",
    "sha256": "<sha256>"
  },
  "project": "example",
  "protected": false,
  "remote_root": "/srv/www/example",
  "schema_version": 1,
  "scope_config_digest": "<sha256>",
  "source": {
    "artifact_sha256": "<sha256>",
    "operation_id": "apply-20260720-123456",
    "release_id": "release-123"
  }
}
```

`kind` is `baseline`, `pre_apply`, or `pre_rollback`. A baseline is protected
from retention and omits `source`. The other kinds are unprotected and require
all three source fields. Timestamps use the exact UTC form
`YYYY-MM-DDTHH:MM:SSZ`; archive and manifest paths are fixed.

`scope_config_digest` identifies remote ownership independently of local source
paths. It hashes canonical JSON containing configuration version 2, sorted
global exclusions, and scopes sorted by name with their target and sorted
include and exclude patterns.

## Manifest

`manifest.json` schema version 1 inventories both regular files and
directories. Scopes are ordered by name and entries by normalized POSIX path:

```json
{
  "backup_id": "apply-20260720-123456",
  "entries": [
    {
      "mode": 493,
      "path": "wp-content/themes/example",
      "scope": "theme",
      "type": "directory"
    },
    {
      "mode": 420,
      "path": "wp-content/themes/example/index.php",
      "scope": "theme",
      "sha256": "<sha256>",
      "size": 1234,
      "type": "file"
    }
  ],
  "schema_version": 1,
  "scopes": [
    {
      "name": "theme",
      "target": "wp-content/themes/example"
    }
  ]
}
```

Modes are integer permission bits from `0000` through `07777`. User and group
ownership, names, and timestamps are not part of the contract. Paths are
relative to `remote.root`; absolute paths, traversal, non-normalized paths,
duplicates, symlinks, hard links, and special files are rejected.

Every entry belongs to exactly one scope. Equal or nested scope targets are
rejected, even if include and exclude patterns appear disjoint. A scope with no
entries is valid and represents an empty restorable ownership boundary.

## Integrity Validation

A bundle is valid only when:

- all JSON documents are canonical and use supported versions;
- metadata and manifest backup identifiers match;
- the manifest and archive match their recorded size and SHA-256 digests;
- the archive contains exactly the manifest entries;
- every entry has the same path, type, permission mode, size, and content digest
  in the archive and manifest.

The archive is a POSIX pax tar compressed with gzip. Its byte digest protects
the actual stored stream; archives are not required to be reproducible byte for
byte.

## Pointers And Retention

The future `baseline` pointer identifies the protected initial backup. `latest`
identifies the state immediately before the last successful mutation, including
a `pre_rollback` backup after a rollback.

Retention counts unprotected `pre_apply` and `pre_rollback` backups. It keeps the
configured number of newest unprotected backups plus any older backup referenced
by `baseline`, `latest`, or an active operation. Selection is deterministic by
`created_at` and then `backup_id`; protected or referenced backups are never
selected for deletion.

The remote executor, pointer updates, backup creation, and restore behavior will
be connected in later increments. This document does not make the complete
format available through the CLI or GitHub Action.
