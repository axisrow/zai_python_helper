# Parity with `@z_ai/coding-helper`

`zai_python_helper` is an **independent clean-room reimplementation** of the
observable behavior of Z.ai's proprietary `@z_ai/coding-helper` (npm). It shares
**none** of that package's source — only the *observable behavior* of how Claude
Code is configured to talk to the GLM Coding Plan, reimplemented from scratch
under MIT.

We track parity in **two phases**.

## Phase 1 — strict behavioral parity

Goal: a user switching from `@z_ai/coding-helper` to `zai_python_helper` sees
**identical observable behavior**. What we clone exactly:

- **`-v` / `--version` FORMAT.** The bare version string with no program-name
  prefix, matching upstream. The version *number* differs, so the comparison
  normalizes only the semver token; both tools must exit successfully and write
  nothing to stderr.
- **Original-mode Claude Code configuration.** The upstream headless
  `auth …` + `auth reload claude` path and our `use zai --mode original` path
  use a normalized-JSON contract: parse UTF-8, replace only the fixed test
  token, then serialize with sorted object keys. Values, JSON types, array
  order, and key presence must match; whitespace, object-key order, and final
  newlines are intentionally ignored.
- **The complete HOME artifact set** for that flow. The Docker test permits only
  explicit directional exceptions: upstream-only `.chelper/config.yaml` stores
  its plan/token; our `.zshrc`, `.zai-python-helper/ownership.json`, and
  `.zai-python-helper/lock` are Phase-2 artifacts below. Any other missing or
  extra file fails parity.

Phase 1 is **strict**: drift is a bug, not a feature. If the original does X, we
do X.

## Phase 2 — our extensions (drift is allowed)

Once Phase 1 is locked, we layer on extensions the original does not have. Here
**desynchronization is acceptable and intentional** — we are no longer trying to
match the original, we are improving on it. Current Phase 2 extensions:

- **Importable core.** The planning logic is a library (`plan_zai`,
  `plan_default`, `postconditions`), not just a CLI. The original is
  CLI-only. See [Importable API](guide/importable.md).
- **Headless operation.** Every action is a CLI flag; no interactive menu.
  The original drives an interactive prompt.
- **Additional model modes.** `default`, `select`, and `custom` have no
  equivalent upstream 0.0.7 model-selection surface, so they are our
  extensions and are covered by local unit/integration tests. `original` is
  the upstream-compared mode in Phase 1.
- **Shell warning block.** The managed `.zshrc` block supports headless-first
  use; upstream's compared headless path writes no shell rc file.
- **Ownership journal.** Reversible, self-invalidating revert semantics
  (ADR-004), stored in `.zai-python-helper/ownership.json`.
- **Multi-file atomic activation.** A validated `PatchPlan` applied under a
  process lock with atomic renames (ADR-005); its lock file is
  `.zai-python-helper/lock`.

## How we verify Phase 1

A Docker parity image builds **both** tools and CI runs the full upstream parity
suite (see `docker/parity/` and `tests/parity/`). It snapshots every regular
file under fresh `HOME` directories, checks the closed artifact sets above, and
normalized-JSON-diffs the two common Claude Code files after token redaction.
If we drift on a Phase-1 surface, CI goes red.

## Summary

| Surface | Phase 1 (clone) | Phase 2 (extend) |
|---------|-----------------|------------------|
| Version format (except number) | ✅ raw format + empty stderr | — |
| Original-mode files + JSON | ✅ closed HOME set + normalized JSON | — |
| `default` / `select` / `custom` model modes | — | ✅ ours; no upstream equivalent |
| Managed `.zshrc` block | — | ✅ ours; upstream headless writes none |
| Importable API | — | ✅ ours |
| Headless flags | — | ✅ ours |
| Ownership journal + lock | — | ✅ ours |
| Atomic PatchPlan | — | ✅ ours |

See the [Architecture](../ARCHITECTURE.md) for the ADRs behind the Phase 2
extensions.
