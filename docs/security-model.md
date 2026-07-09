# Security Model

The tool assumes that the consumer repository is trusted and the remote server
is a production-like target that must be protected from path traversal and
accidental data loss.

## Local Packaging

- Sources are selected from `git ls-files`, so ignored files are not packaged.
- Absolute sources and `..` path segments are rejected.
- Symlinks are rejected in V1.
- Archive validation rejects absolute paths, `..`, and unsupported tar entry
  types.

## Remote Access

- `report` runs read-only commands over SSH.
- `apply` and `rollback` take an exclusive lock under `remote.workdir` before
  mutating the remote document root.
- Each apply creates a checkpoint before writing files under `remote.root`.
- Apply retries for failed transactions only proceed when each target file is
  either still in the pre-apply state or already matches the archive.
- `rollback latest` refuses to restore over files that changed after apply.
- SSH keys and known hosts are provided by the consumer repository secrets.
- `apply` and `rollback` require an explicit known-hosts file and do not use
  `StrictHostKeyChecking=accept-new`.
- Secrets are not written to reports.
- The GitHub Action downloads the released PEX, verifies its SHA-256 checksum,
  and verifies a GitHub Artifact Attestation before execution. The attestation
  must be signed by this repository's release workflow and match the immutable
  version tag commit.
- The action uses the consumer job `GITHUB_TOKEN` for GitHub CLI release and
  attestation API calls. Consumer workflows should keep the token read-only.
- E2E tests generate ephemeral SSH client keys and strict `known_hosts` files in
  a temporary directory; no private key is committed.

## Deployment Strategy

The configured target strategy is `overlay`: files from the archive are written
over matching remote files, new files are created, and unknown remote files are
kept. Destructive deletion of unknown remote files is not supported in V1.

`rollback` is limited to `latest`. It is designed to undo the latest successful
apply transaction, not to restore an arbitrary historical server state.
