# Consumer Workflow

Use the GitHub Action to inspect a remote target, generate a deployment report,
apply a reviewed archive, or rollback the latest successful apply transaction
from a consumer repository.

Start with `workflow_dispatch`. Run `doctor` before the first report or mutation,
and do not wire automatic deploys until the capabilities are understood, the
report is clean, and the repository has been reconciled with the server state.

```yaml
- uses: coopTilleuls/ssh-archive-deploy@v0
  with:
    mode: doctor
    config: deploy.yml
    target-name: production
    report-dir: dist/deploy-doctor
    ssh-host: ${{ secrets.SSH_HOST }}
    ssh-user: ${{ secrets.SSH_USER }}
    ssh-private-key: ${{ secrets.SSH_PRIVATE_KEY }}
    ssh-known-hosts: ${{ secrets.SSH_KNOWN_HOSTS }}

- uses: actions/upload-artifact@v7
  if: always()
  with:
    name: deploy-doctor
    path: dist/deploy-doctor
```

Use a non-secret `target-name`, such as a GitHub Environment name. The doctor
result deliberately does not contain the raw SSH host or credentials.

After doctor, generate the drift report:

```yaml
name: Deployment Report

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  report:
    runs-on: ubuntu-latest
    environment: production
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

## What The Action Does

For `mode: report`, the action:

1. configures Python 3.12;
2. downloads the released Linux x86_64 PEX matching the action version;
3. verifies the PEX SHA-256 checksum and GitHub Artifact Attestation;
4. requires the attestation to come from this repository's release workflow and
   match the immutable version tag commit;
5. builds the deployment archive from the checked-out repository;
6. compares the archive with the remote server over SSH;
7. writes `report.json`, per-scope text files, and a GitHub Actions job summary.

For `mode: doctor`, the action performs steps 1 through 4, skips archive
construction, then inventories the remote portable command profile, tar
implementation/version/options, and observable root/workdir permissions. It
writes `${report-dir}/doctor.json`, exposes that path as `doctor-report`, and
adds a concise job summary. The JSON verdict can be `compatible`,
`incompatible`, or `undetermined`; untested tar versions are never assumed to
be compatible.

Consumers do not install `uv` or Python dependencies at workflow runtime.

For `mode: apply`, the action builds the archive, creates a remote checkpoint
under `remote.workdir`, applies files with the configured `overlay` strategy,
and preserves remote-only files. It exposes these outputs:

- `release-id`
- `transaction-id`
- `checkpoint-path`
- `rollback-command`

For `mode: rollback`, set `rollback-release: latest`. Historical rollback to an
arbitrary transaction id is not part of the current experimental contract.

Use the moving major tag `@v0` to receive the latest compatible 0.x release
without changing consumer workflows. Pin an exact release such as `@v0.2.1`
when a project needs fully immutable action resolution.

The release workflow publishes immutable releases for exact tags such as
`v0.2.1`, then moves the major tag, such as `v0`, to the same tested commit.
The major tag is intentionally mutable and should not be used when a consumer
requires fully reproducible action resolution.

## Secrets

Required SSH inputs:

- `ssh-host`
- `ssh-user`
- `ssh-private-key`
- `ssh-known-hosts`

Use repository or environment secrets for these values. The private key should
be a deployment key dedicated to the target server. `ssh-known-hosts` should
contain the expected host key. It is required by default for every SSH mode,
which makes `doctor` and `report` reproducible as well as safe. Read-only first
contact may explicitly set `ssh-allow-host-key-discovery: "true"`; this option
is rejected for `apply` and `rollback`, does not persist the observed key, and
should be replaced with pinned known-hosts material before normal use.

The action uses the job `GITHUB_TOKEN` to download its published PEX release
asset and verify the artifact attestation. Keep `permissions: contents: read`
in the consumer workflow; no dedicated release-download secret is required.

## Report Review

The job summary shows aggregate drift counters. The uploaded `deploy-report`
artifact remains the detailed file-level source.

Interpret the main counters as:

- `changed_files`: files present on both sides with different content;
- `artifact_only_files`: files present in the archive but absent remotely;
- `remote_only_files`: files present remotely but absent from the archive.

`remote_only_files` are reported as drift. With the configured overlay
strategy, they stay in place during `apply`.

## Platform Support

Initial release support is Linux x86_64 on GitHub-hosted or compatible runners.
