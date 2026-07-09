# Configuration

Consumers define deployment scope in a versioned YAML file.

```yaml
version: 1

project: example

remote:
  root: /absolute/document/root
  workdir: /.deploy/example

strategy:
  apply: overlay
  delete_unknown: false

backup:
  retention: 3
  baseline_id: server-baseline-before-ci

scope:
  - name: static
    source: public
    target: .
    include:
      - index.html
      - assets/**
    exclude:
      - assets/cache/**

exclude:
  - .env
  - .git/**
```

Rules:

- `version` must be the integer `1`.
- `remote.root` must be an absolute path and cannot be `/`.
- `remote.workdir` must be an absolute path. Mutating modes use it for locks,
  baselines, checkpoints, transactions, and pointers.
- `scope[].source` must be relative to the repository root.
- `scope[].target` must be relative to `remote.root`; use `.` for the remote
  root itself.
- `include` and `exclude` use shell-style globs.
- Global excludes are matched against archive paths relative to `remote.root`.
- Scope excludes are matched both against paths relative to the scope source and
  paths relative to `remote.root`.
- `strategy.apply` only accepts `overlay`: files from the archive are written
  over matching remote files, new files are created, and unknown remote files
  are preserved.
- `strategy.delete_unknown` must stay `false`; destructive replacement is not
  part of V1.
- Mutating modes require GNU tar with `--keep-old-files` support on the remote
  server. They do not require Python on the remote server.
- Scalar values are parsed strictly. For example, `delete_unknown: "false"` and
  `retention: "3"` are rejected because they are strings, not booleans or
  integers.
- Unknown keys are rejected so the versioned contract can evolve explicitly.
- `backup.retention` controls how many transaction records are retained.
- `backup.baseline_id` names the protected initial server baseline. The baseline
  is created before the first apply when absent and is not removed by retention.

## Apply And Rollback Contract

`apply` validates the archive, takes a remote lock, computes a fresh execution
plan from the current server state, creates a checkpoint, then writes only
`create` and `replace` files through confined tar extraction. `skip` files are
left untouched and remote-only files are preserved.

If a previous apply for the same `release_id` and archive is incomplete or
failed, a retry attempts recovery instead of blindly blocking. Recovery is
allowed only when every planned file is still either in its original
pre-apply state or already matches the archive. Unknown states fail with an
explicit error. Partially applied retries also require the original
`before.tar.gz` checkpoint so rollback remains possible after recovery.

`rollback --release latest` restores the latest successful apply transaction.
It restores replaced files from the transaction checkpoint and deletes files
created by that transaction only when their checksum still matches the applied
artifact. If a deployed file changed after apply, rollback fails instead of
overwriting manual changes silently.

The remote state is stored under `remote.workdir`:

```text
locks/
baselines/
transactions/
pointers/
```

V1 does not support `delete_unknown=true`, forced lock cleanup, automatic
recovery of unknown transaction states, or rollback to an arbitrary historical
id.

## Report Contract

`report` writes `report.json` as the stable machine-readable output. The text
and TSV files written per scope are human convenience artifacts.

Report paths are always relative to `remote.root`, even when the internal
planner compares files relative to a scope target.

`summarize-report --format github` renders this JSON contract as a concise
Markdown table for GitHub Actions job summaries. It does not change the report
schema.

```json
{
  "schema_version": 1,
  "project": "example",
  "release_id": "20260706T120000Z-abcdef123456",
  "strategy": {
    "apply": "overlay",
    "delete_unknown": false
  },
  "summary": {
    "changed_files": 1,
    "artifact_only_files": 1,
    "remote_only_files": 1,
    "text_changed_files": 1,
    "binary_changed_files": 0,
    "insertions": 1,
    "deletions": 1
  },
  "scopes": [
    {
      "name": "static",
      "target": ".",
      "summary": {
        "changed_files": 1,
        "artifact_only_files": 1,
        "remote_only_files": 1,
        "text_changed_files": 1,
        "binary_changed_files": 0,
        "insertions": 1,
        "deletions": 1
      },
      "changed_files": ["index.html"],
      "changed_text_files": ["index.html"],
      "changed_binary_files": [],
      "artifact_only_files": ["assets/app.css"],
      "remote_only_files": ["uploads/manual.txt"],
      "text_line_changes": [
        {
          "path": "index.html",
          "insertions": 1,
          "deletions": 1
        }
      ]
    }
  ]
}
```
