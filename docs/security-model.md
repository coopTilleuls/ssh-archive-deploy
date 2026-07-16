# Security Model

The tool assumes that the consumer repository is trusted and the remote server
is a production-like target that must be protected from path traversal and
accidental data loss.

## Local Packaging

- Sources are selected from `git ls-files`. Ignored or otherwise untracked
  build products are packaged only when a version 2 scope explicitly declares
  them under `generated`.
- Absolute sources and `..` path segments are rejected.
- Symlinks are rejected for both tracked files and generated inputs.
- Generated inputs must stay inside their scope source, be non-empty after
  exclusions, and must not overlap Git-tracked files, gitlinks, or another
  input. The check occurs before exclusions.
- Manifest validation confines every generated file to its declared target path
  and verifies that declared required paths contribute content.
- Final package content and existing archives are checked for unresolved Git
  LFS v1 pointers without requiring the `git-lfs` executable. Failures expose
  affected deployment paths, not file contents.
- Archive validation rejects absolute paths, `..`, and unsupported tar entry
  types.

## Remote Access

- `doctor` only observes command availability, normalized tar capabilities, and
  shell permission tests. It does not create the configured workdir or a write
  probe; actual writability is verified before a future mutation.
- `report` runs read-only commands over SSH.
- `apply` and `rollback` take an exclusive lock under `remote.workdir` before
  mutating the remote document root.
- Each apply creates a checkpoint before writing files under `remote.root`.
- Mutating writes use GNU tar extraction primitives instead of direct `cp` to
  avoid following raced destination symlinks outside `remote.root`.
- Apply retries for failed transactions only proceed when each target file is
  either still in the pre-apply state or already matches the archive.
- `rollback latest` refuses to restore over files that changed after apply.
- SSH keys and known hosts are provided by the consumer repository secrets.
- Every SSH mode requires an explicit known-hosts file by default. Only
  `doctor` and `report` expose an explicit permissive discovery option; it uses
  `StrictHostKeyChecking=accept-new` without persisting the observed key.
- `apply` and `rollback` never accept permissive host-key discovery.
- Doctor results contain a caller-provided non-secret target label, not the raw
  SSH host, user, private-key path, or raw remote tool output.
- Secrets are not written to reports.
- The GitHub Action downloads the released PEX, verifies its SHA-256 checksum,
  and verifies a GitHub Artifact Attestation before execution. The attestation
  must be signed by this repository's release workflow and match the immutable
  version tag commit.
- Pull-request CI may exercise the local Action with a PEX built from that same
  checkout. This source-build path is restricted to local root Action calls on
  `pull_request`, verifies the generated checksum, and is unavailable to remote
  consumers. It does not replace release checksum or attestation verification.
- Exact release tags such as `v0.3.0` are protected by immutable releases. Major
  tags such as `v0` are mutable experimental pointers and are moved only after a
  new exact release is published successfully.
- The action uses the consumer job `GITHUB_TOKEN` for GitHub CLI release and
  attestation API calls. Consumer workflows should keep the token read-only.
- E2E tests generate ephemeral SSH client keys and strict `known_hosts` files in
  a temporary directory; no private key is committed.

## Deployment Strategy

The configured target strategy is `overlay`: files from the archive are written
over matching remote files, new files are created, and unknown remote files are
kept. Destructive deletion of unknown remote files is not supported by the
current experimental contract.

`rollback` is limited to `latest`. It is designed to undo the latest successful
apply transaction, not to restore an arbitrary historical server state.
