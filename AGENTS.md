# Agent Guidelines

## Project Shape

`ssh-archive-deploy` is a Python 3.12 CLI and composite GitHub Action for scoped
archive deployment over SSH.

The current implementation supports the full scoped deployment flow:

- `build` packages git-tracked, allowlisted files into a tar archive.
- `validate` verifies the archive and manifest safety contract.
- `report` compares the archive with a remote server over SSH and writes review
  artifacts.
- `apply` writes the archive transactionally with the configured `overlay`
  strategy.
- `rollback` restores the latest successful apply transaction.

Treat `apply` and `rollback` as mutating operations. Do not change their
behavior unless the matching code, tests, and security model are updated
together.

## Repository Map

- `src/ssh_archive_deploy/archive.py`: archive creation, manifest writing, and
  archive validation.
- `src/ssh_archive_deploy/config.py`: deploy YAML parsing and schema validation.
- `src/ssh_archive_deploy/doctor.py`: read-only remote capability inspection and
  versioned doctor results.
- `src/ssh_archive_deploy/planner.py`: drift classification between artifact and
  remote snapshots.
- `src/ssh_archive_deploy/report.py`: SSH remote snapshot collection and report
  generation.
- `src/ssh_archive_deploy/cli.py`: public CLI commands and argument handling.
- `action.yml`: composite GitHub Action wrapper around the CLI.
- `tests/e2e/`: Dockerized OpenSSH end-to-end tests using the public CLI.
- `docs/configuration.md`: deploy YAML and report schema contract.
- `docs/security-model.md`: safety assumptions and path/archive constraints.
- `docs/consumer-workflow.md`: consumer GitHub Actions usage.

## Tooling Contract

This repository uses:

- `mise` for local tool versions used by contributors and CI.
- `uv` for Python dependencies, including Python dev tools.
- `hk check --all` as the main validation entrypoint.

Use the existing tasks:

```bash
mise install
uv sync
mise run check
mise run fix
mise run test
mise run build:pex
mise run test:e2e
mise run test:e2e:pex
```

`mise run check` delegates to `hk check --all`. Do not add a parallel validation
entrypoint unless there is a strong reason to change the project contract.

## Validation Expectations

Prefer the fix-first loop to reduce review churn:

```bash
mise run fix
# manually fix anything hk could not fix
mise run fix
```

`mise run fix` also reports checks that cannot be fixed automatically and runs
`hk` with `--no-stage`, so automatic fixes remain unstaged for review. A
successful second run is the default validation signal. Use `mise run check`
only when you specifically need to reproduce the CI check path without applying
fixes.

Run `mise run test:e2e` when changing SSH behavior, report generation, archive
transport, the composite action, or the Docker test harness. E2E tests require
Docker Compose plus OpenSSH client tools.

Run `mise run test:e2e:pex` when changing release packaging or the composite
action runtime. It builds the Linux x86_64 CPython 3.12 PEX and runs the same
SSH E2E scenarios through that artifact.

Docker and Buildx validations require sandbox permissions that allow writes to
Docker's state directory, including `~/.docker/buildx`. Request the required
elevation before the first run; do not treat a sandbox-denied write there as a
product test failure.

Do not add Docker-dependent tests to the normal `hk check --all` path; keep E2E
coverage explicit.

## Safety Contract

Preserve these guarantees unless a versioned design change updates code, tests,
and docs together:

- Build input comes from `git ls-files`; ignored files are not packaged.
- Local sources and remote targets must be relative where the config requires
  relative paths.
- Archive validation rejects absolute paths, `..`, duplicate paths, symlinks,
  device files, and unsupported tar entry types.
- Manifest file lists and checksums must match archive contents.
- `remote.root` must be absolute and must not be `/`.
- `doctor` and `report` must stay read-only and must not write to the remote server.
- SSH host keys are strict by default; permissive discovery is explicit and
  limited to `doctor` and `report`.
- `apply` and `rollback` must require an explicit known-hosts file.
- Reports must not include secrets.
- Never commit SSH private keys, real host credentials, or consumer secrets.

## Documentation Rules

Keep public docs short and aligned with the implemented behavior:

- Update `README.md` for user-facing command or setup changes.
- Update `docs/configuration.md` for deploy YAML or report schema changes.
- Update `docs/security-model.md` for safety model changes.
- Update `docs/consumer-workflow.md` for GitHub Action usage changes.

Planning notes belong outside public docs unless they describe current behavior
or a committed contract.

## Release Notes

Prepare substantive release notes before pushing an exact release tag. A
one-line placeholder such as `Release vX.Y.Z` is not sufficient. Summarize, as
applicable:

- user-visible features and fixes;
- breaking changes and required migration steps;
- security-relevant changes without disclosing exploit details;
- current platform or operational limitations.

Store the prepared notes in `docs/releases/vX.Y.Z.md` and validate them with
`scripts/validate_release_notes.py` as documented in the README release
procedure before creating the release commit or tag.

Keep consumer-specific and private project context out of public release notes.
Releases are published as immutable, so the notes must be finalized before
publication. If the release workflow cannot consume prepared notes or generate
an adequate equivalent, update it before creating the tag.

## Coding Guidance

- Prefer `pathlib` for filesystem paths.
- Keep subprocess calls explicit and avoid shell interpolation unless there is a
  reviewed reason.
- Keep Pyrefly strict clean; prefer local type improvements over suppressions.
- Keep Ruff formatting and linting clean.
- Add focused tests when changing archive, config, planner, report, CLI, or
  action behavior.
