# ssh-archive-deploy

`ssh-archive-deploy` builds a scoped deployment archive, compares it with a
remote server over SSH, and can apply it with remote checkpoints and rollback
support.

## Project Status

> [!WARNING]
> This project is experimental. Its configuration, remote requirements, and
> mutating deployment guarantees may change while it is evaluated against real
> managed-hosting environments. Start with the read-only `report` mode, verify
> the target server capabilities, and do not use `apply` or `rollback` for
> unattended production deployments without project-specific validation.

- `build` creates an archive from git-tracked, allowlisted files.
- `validate` checks the archive and manifest safety contract.
- `report` compares the archive with the remote server over SSH.
- `summarize-report` renders the JSON report as Markdown for GitHub Actions.
- `apply` writes the archive with the configured `overlay` strategy.
- `rollback` supports `latest` to undo the latest successful `apply`.

## GitHub Action

Start with a manual report workflow. Do not wire automatic deploys until the
repository has been reconciled with the server state.

```yaml
name: Deployment Report

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: coopTilleuls/ssh-archive-deploy@v0
        with:
          mode: report
          config: deploy.yml
          archive: dist/site.tar.gz
          report-dir: dist/deploy-report
          ssh-host: ${{ secrets.SSH_HOST }}
          ssh-user: ${{ secrets.SSH_USER }}
          ssh-private-key: ${{ secrets.SSH_PRIVATE_KEY }}
          ssh-known-hosts: ${{ secrets.SSH_KNOWN_HOSTS }}

      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: deploy-report
          path: dist/deploy-report
```

The action configures Python 3.12, downloads the matching PEX from the GitHub
Release, verifies its SHA-256 checksum and GitHub Artifact Attestation, builds
the archive, and runs the CLI. The attestation must come from this repository's
release workflow and match the immutable version tag commit. It does not
install `uv` or resolve Python dependencies at workflow runtime.

In `report` mode, the job summary shows the global and per-scope drift counters
directly in the GitHub Actions UI. The `deploy-report` artifact remains the
detailed file-level output.

Use `mode: apply` only after a clean report has been reviewed for the target
environment. Apply writes a remote checkpoint under `remote.workdir`, preserves
remote-only files, and exposes `transaction-id`, `checkpoint-path`, and
`rollback-command` outputs. Use `mode: rollback` with `rollback-release: latest`
to undo the latest successful apply transaction. `ssh-known-hosts` is required
for `apply` and `rollback`; these modes do not accept new host keys
automatically.

Initial release support is Linux x86_64 only.

Use the moving major tag `@v0` to receive the latest compatible 0.x release
without changing consumer workflows. Pin an exact release such as `@v0.2.1`
when a project needs fully immutable action resolution.

The release workflow publishes immutable releases for exact tags such as
`v0.2.1`, then moves the major tag, such as `v0`, to the same tested commit.
The major tag is intentionally mutable and must not be associated with a GitHub
Release. The action uses the consumer workflow `GITHUB_TOKEN` to download the
published PEX and verify its attestation; no dedicated token input is required.

## Deploy Configuration

Consumers define the deployment scope in `deploy.yml`.

```yaml
version: 1

project: example

remote:
  root: /absolute/document/root
  workdir: /.deploy/example

strategy:
  apply: overlay
  delete_unknown: false

scope:
  - name: theme
    source: wp-content/themes/example
    target: wp-content/themes/example

exclude:
  - .git/**
  - .env
  - wp-config.php
```

See [docs/configuration.md](docs/configuration.md) for the full configuration
and report schema contract.

## Report Output

`report` writes:

- `report.json`: stable machine-readable output;
- per-scope text and TSV files for human review;
- a GitHub Actions job summary when the action runs in `mode: report`.

The main counters are:

- `changed_files`: files present on both sides with different content;
- `artifact_only_files`: files present in the archive but missing remotely;
- `remote_only_files`: files present remotely but not in the archive.

Remote-only files are reported as drift. With the configured overlay strategy,
they stay in place during `apply`.

## CLI

Local CLI usage assumes the project development environment is installed:

```bash
mise install
uv sync
```

Build and validate an archive:

```bash
uv run ssh-archive-deploy build \
  --config deploy.yml \
  --output dist/site.tar.gz

uv run ssh-archive-deploy validate \
  --config deploy.yml \
  --archive dist/site.tar.gz
```

Generate a remote report:

```bash
uv run ssh-archive-deploy report \
  --config deploy.yml \
  --archive dist/site.tar.gz \
  --output-dir dist/deploy-report \
  --ssh-host "$SSH_HOST" \
  --ssh-user "$SSH_USER" \
  --ssh-private-key-file "$SSH_PRIVATE_KEY_FILE" \
  --ssh-known-hosts-file "$SSH_KNOWN_HOSTS_FILE"
```

Render the report as GitHub-flavored Markdown:

```bash
uv run ssh-archive-deploy summarize-report \
  --report dist/deploy-report/report.json \
  --format github
```

Apply the archive and rollback the latest successful apply:

```bash
uv run ssh-archive-deploy apply \
  --config deploy.yml \
  --archive dist/site.tar.gz \
  --ssh-host "$SSH_HOST" \
  --ssh-user "$SSH_USER" \
  --ssh-private-key-file "$SSH_PRIVATE_KEY_FILE" \
  --ssh-known-hosts-file "$SSH_KNOWN_HOSTS_FILE"

uv run ssh-archive-deploy rollback \
  --config deploy.yml \
  --release latest \
  --ssh-host "$SSH_HOST" \
  --ssh-user "$SSH_USER" \
  --ssh-private-key-file "$SSH_PRIVATE_KEY_FILE" \
  --ssh-known-hosts-file "$SSH_KNOWN_HOSTS_FILE"
```

SSH commands also read settings from these environment variables:

- `SSH_HOST`
- `SSH_USER`
- `SSH_PORT`
- `SSH_PRIVATE_KEY_FILE`
- `SSH_KNOWN_HOSTS_FILE`
- `SSH_ALIAS`

`SSH_KNOWN_HOSTS_FILE` is required for `apply` and `rollback`. Without it,
mutating commands fail before opening an SSH session.

Remote `apply` and `rollback` require GNU tar with `--keep-old-files` support.
No Python runtime is required on the remote server.

## Security Model

The tool is designed for SSH-only hosting where the repository is trusted but
the remote target must be protected from path traversal and accidental data
loss.

Key guarantees:

- build input comes from `git ls-files`, so ignored files are not packaged;
- archive validation rejects unsafe paths, symlinks, device files, and
  unsupported tar entries;
- `report` is read-only and does not modify the remote server;
- secrets are not written to reports.

See [docs/security-model.md](docs/security-model.md) for details.

## Contributor Checks

Contributor and agent guidance lives in [AGENTS.md](AGENTS.md).

Useful checks:

```bash
mise run fix
mise run test
mise run build:pex
mise run test:e2e:pex
```

`mise run fix` runs `hk fix --all --no-stage`, so automatic fixes are left for
review instead of being staged.

## Release

Normal releases are created by pushing an exact SemVer tag from `main`:

```bash
uv version 0.2.4
git add pyproject.toml uv.lock
git commit -m "chore: release v0.2.4"
git tag v0.2.4
git push origin main v0.2.4
```

The release workflow refuses to publish unless the tag is `vX.Y.Z`, the
`pyproject.toml` version matches `X.Y.Z`, and the tagged commit is included in
`origin/main`. It builds and tests the PEX, creates the GitHub Release as a
draft, uploads the assets, publishes the immutable release, then moves the
mutable major tag such as `v0`.

`workflow_dispatch` remains available as a fallback; it reads the version from
`pyproject.toml` on `main` and creates the matching `vX.Y.Z` tag during release.
