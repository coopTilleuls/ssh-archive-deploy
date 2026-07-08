# Usage Integration Plan

`usage` could become the source of truth for the public CLI contract in a
future version, after the deployment command surface is stable enough to justify
a generated reference.

The goal is not to replace the deployment logic. The goal is to make command
help, generated documentation, shell completions, and future SDKs derive from a
single CLI specification.

## Target Outcome

- Keep the CLI implementation in Python.
- Keep deployment behavior in `src/ssh_archive_deploy`.
- Add a versioned `usage` specification for the `ssh-archive-deploy` command.
- Generate CLI reference documentation from that specification.
- Generate shell completions from that specification.
- Add CI checks so generated CLI docs stay in sync.

## Why Usage Fits

`usage` is designed as a CLI specification and generator. It can generate shell
completions, Markdown documentation, manpages, and SDKs from one spec.

That maps well to this project because the tool is expected to become a reused
deployment product rather than a single-project script. Consumers will need
stable command documentation, predictable help output, and optionally shell
completion for local operations.

## Current CLI State

The current CLI uses Python `argparse` and exposes these commands:

- `build`
- `validate`
- `report`
- `summarize-report`
- `apply`
- `rollback`

`apply` supports the V1 overlay deployment flow. `rollback` supports
`--release latest`. The first `usage` integration must preserve that behavior
without widening the deployment contract.

## Integration Strategy

Start with `usage` as a development and documentation tool, not as runtime
parsing infrastructure.

Recommended progression:

1. Add `usage` to `mise.toml`.
2. Add a checked-in CLI specification file.
3. Generate `docs/cli.md` from the specification.
4. Generate shell completion artifacts only if there is a clear distribution
   path.
5. Add CI checks that lint the spec and verify generated docs are current.
6. Re-evaluate whether parsing should move from `argparse` to `usage` after the
   CLI contract stabilizes.

This keeps risk low: the V1 command behavior remains controlled by the Python
tests, while `usage` improves the public interface around it.

## Spec Source Options

### Option A: Manual KDL Spec

Create a file such as:

```text
usage/ssh-archive-deploy.usage.kdl
```

Benefits:

- explicit and reviewable CLI contract;
- does not require extra Python runtime dependencies;
- easy to generate Markdown and completions in CI.

Costs:

- command options are duplicated between `argparse` and KDL;
- CI must catch drift between implementation and spec.

This is the recommended first option.

### Option B: Generate Spec From Argparse

Use the Python `argparse-usage` integration to generate a `usage` spec from the
existing parser.

Benefits:

- less duplication with `argparse`;
- the existing parser remains the source of truth.

Costs:

- adds another Python dependency;
- compatibility must be tested against subcommands and environment-backed
  defaults;
- generated output may be less readable as a committed contract.

This is worth testing after Option A proves useful.

### Option C: Runtime Usage Parser

Use `usage` as the parser and dispatch into Python command handlers.

Benefits:

- one source of truth for parse behavior, docs, and completions.

Costs:

- higher integration complexity;
- adds runtime coupling to `usage`;
- less useful until the command surface is stable.

This is not recommended until the Phase 4 command surface has been exercised by
consumer repositories and is unlikely to change.

## Proposed Repository Changes

Add:

```text
usage/
└── ssh-archive-deploy.usage.kdl

docs/
└── cli.md
```

Update:

```toml
[tools]
usage = "latest"

[tasks.cli-docs]
run = "usage generate markdown --file usage/ssh-archive-deploy.usage.kdl --out-file docs/cli.md"

[tasks.cli-docs-check]
run = "usage lint usage/ssh-archive-deploy.usage.kdl && mise run cli-docs && git diff --exit-code -- docs/cli.md"
```

Add an `hk` step for the spec and generated docs:

```text
usage-lint
cli-docs-check
```

## CLI Spec Scope For First Pass

The first spec should document only stable user-facing commands and options:

- global command name and description;
- `build --config --output --release-id`;
- `validate --config --archive`;
- `report --config --archive --output-dir`;
- SSH flags for `report`;
- `summarize-report --report --format`, currently supporting `github`;
- `apply --config --archive` plus SSH flags and `--result-json`;
- `rollback --config --release` plus SSH flags and `--result-json`.

Avoid documenting internal Python modules, manifest internals, or remote shell
implementation details in the generated CLI reference.

## Generated Artifacts

Commit generated Markdown documentation:

```text
docs/cli.md
```

Do not commit shell completion files initially unless consumers ask for packaged
completion artifacts. Prefer documenting how to generate completions locally:

```bash
usage generate completion --file usage/ssh-archive-deploy.usage.kdl bash ssh-archive-deploy
usage generate completion --file usage/ssh-archive-deploy.usage.kdl zsh ssh-archive-deploy
usage generate completion --file usage/ssh-archive-deploy.usage.kdl fish ssh-archive-deploy
```

## Validation

Required checks:

```bash
usage lint usage/ssh-archive-deploy.usage.kdl
mise run cli-docs
git diff --exit-code -- docs/cli.md
mise run check
```

If `usage` is added to `mise.toml`, regenerate and verify `mise.lock`.

## Risks And Guardrails

- Do not let the KDL spec drift from `argparse`.
- Do not introduce `usage` as a runtime dependency until there is a clear reason.
- Do not document future behavior as available behavior.
- Keep generated docs separate from operational deployment docs.
- Keep secrets, environment names, and project-specific credentials out of the
  generated CLI reference.

## Acceptance Criteria

The integration is ready for a future release when:

- `usage` is installed through `mise`;
- `usage lint` passes in CI;
- CLI Markdown docs are generated and checked in CI;
- generated docs match the actual `argparse` help for all implemented commands;
- no deployment behavior changes are introduced by the documentation/completion
  integration.

## Later Evaluation

After the Phase 4 `apply` and `rollback` flows are adopted by at least one
consumer project, re-evaluate whether to:

- keep the manual KDL spec;
- generate KDL from `argparse`;
- migrate from `argparse` to a `usage`-driven parser;
- publish generated completions with releases;
- generate a Python or TypeScript SDK for consumers that want to call the tool
  programmatically.
