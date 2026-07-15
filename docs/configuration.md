# Configuration

Consumers define deployment scope in a versioned YAML file.

> [!IMPORTANT]
> Configuration version 2 is currently available on the unreleased `main`
> branch. Release `v0.2.5` still expects version 1.

```yaml
version: 2

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
    generated:
      - path: vendor
        required_paths:
          - autoload.php

exclude:
  - .env
  - .git/**
```

Rules:

- `version` must be the integer `2`. Version 1 configurations are rejected.
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
- `scope[].generated` is the explicit allowlist for build products that are not
  tracked by Git. Each `path` is relative to `scope[].source` and can name a
  regular file or directory.
- Every generated input is mandatory and must contribute at least one regular
  file after exclusions. Generated inputs cannot overlap each other or a
  Git-tracked file.
- `required_paths` is optional. Each entry is relative to its generated input
  and must exist and contribute packaged content; for example, `autoload.php`
  above checks `public/vendor/autoload.php`.
- Generated files use the same global and scope exclusions as tracked files.
  Scope `include` patterns do not restrict them because each generated path is
  already explicitly allowlisted.
- Generated symlinks, special files, path escapes, and target collisions are
  rejected. The version 2 manifest records which files each generated input
  contributed.
- `strategy.apply` only accepts `overlay`: files from the archive are written
  over matching remote files, new files are created, and unknown remote files
  are preserved.
- `strategy.delete_unknown` must stay `false`; destructive replacement is not
  part of the current experimental contract.
- Mutating modes require GNU tar with `--keep-old-files` support on the remote
  server. They do not require Python on the remote server.
- Scalar values are parsed strictly. For example, `delete_unknown: "false"` and
  `retention: "3"` are rejected because they are strings, not booleans or
  integers.
- Unknown keys are rejected so the versioned contract can evolve explicitly.
- `backup.retention` controls how many transaction records are retained.
- `backup.baseline_id` names the protected initial server baseline. The baseline
  is created before the first apply when absent and is not removed by retention.

## Archive Manifest Contract

Every archive contains `DEPLOYMENT_MANIFEST.json`. Manifest version 2 records
the scope of every deployed file and the contribution of each generated input:

```json
{
  "version": 2,
  "tool_version": "0.x.y",
  "project": "example",
  "release_id": "release-123",
  "commit_sha": "<git-sha>",
  "commit_ref": "main",
  "build_time": "2026-07-15T12:00:00Z",
  "scopes": [
    {
      "name": "static",
      "source": "public",
      "target": ".",
      "files": ["vendor/autoload.php"],
      "generated": [
        {
          "path": "vendor",
          "required_paths": ["autoload.php"],
          "files": ["vendor/autoload.php"]
        }
      ]
    }
  ],
  "files": [
    {
      "path": "vendor/autoload.php",
      "scope": "static",
      "size": 123,
      "sha256": "<sha256>"
    }
  ]
}
```

Paths in scope and generated file lists are relative to `remote.root`.
Generated `path` and `required_paths` remain relative to the scope source and
generated input, respectively. Validation rejects empty generated
contributions, files outside the declared generated target, required paths that
contribute no content, and manifest scopes that differ from the supplied deploy
configuration. Manifest version 1 is not accepted by the version 2 tool.

## Doctor Contract

`doctor` reads the deployment configuration but does not require an archive. It
executes observation-only checks over SSH and writes a versioned JSON result. A
non-secret `--target-name` distinguishes environments without persisting the SSH
host, user, key path, or raw tool output.

The portable command profile is `sh`, `tar`, `cp`, `mkdir`, and `rm`. Missing
optional tools are reported but do not reduce compatibility. `tar` compatibility
uses both its normalized implementation/version and the options found in its
runtime help. Only implementation/version pairs exercised by SSH E2E tests are
`compatible`; an otherwise plausible but untested pair is `undetermined`.
The initial tested matrix contains GNU tar 1.34, as provided by the Debian 12.11
SSH E2E image.

`remote.root` must be an existing readable directory. `remote.workdir` is
observed with shell permission tests only. Its `writable_hint` is not proof that
a real write will succeed, and `doctor` never creates it or writes a probe file.
An absent workdir therefore makes the overall result `undetermined`; actual
write access remains an `apply` preflight check.

```json
{
  "schema_version": 1,
  "operation": "doctor",
  "checked_at": "2026-07-10T12:30:00Z",
  "project": "example",
  "target": {"name": "production"},
  "ssh": {"host_key_policy": "strict"},
  "compatibility": "compatible",
  "commands": {
    "required": [{"name": "sh", "available": true}],
    "optional": [{"name": "rsync", "available": false}],
    "absent": ["rsync"]
  },
  "tar": {
    "available": true,
    "implementation": "gnu",
    "version": "1.34",
    "version_status": "tested",
    "options": {"--create": true},
    "compatibility": "compatible"
  },
  "remote": {
    "root": {
      "exists": true,
      "directory": true,
      "readable": true,
      "writable_hint": true
    },
    "workdir": {
      "exists": true,
      "directory": true,
      "readable": true,
      "writable_hint": true
    }
  },
  "diagnostics": []
}
```

The abbreviated arrays and tar option mapping above illustrate the schema; real
results contain every probed command and required tar option. Compatibility
uses this precedence: `incompatible`, then `undetermined`, then `compatible`.
The command exits successfully when inspection and serialization succeed, so
automation that needs a compatible server must inspect the JSON verdict.

`summarize-doctor --format github` renders the result without changing the
schema.

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

The current contract does not support `delete_unknown=true`, forced lock cleanup, automatic
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
