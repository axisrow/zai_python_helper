# Parity with `@z_ai/coding-helper`

`zai_python_helper` is an **independent clean-room reimplementation** of the
observable behavior of Z.ai's proprietary `@z_ai/coding-helper` (npm). It shares
**none** of that package's source — only the *observable behavior* of how Claude
Code is configured to talk to the GLM Coding Plan, reimplemented from scratch
under MIT.

We track parity in **two phases**.

## Phase 1 — byte-for-byte behavioral parity

Goal: a user switching from `@z_ai/coding-helper` to `zai_python_helper` sees
**identical observable behavior**. What we clone exactly:

- **`-v` / `--version` FORMAT.** The bare version string with no program-name
  prefix, matching the upstream `Commander .version()` output. (The version
  *number* differs — the *format* must not.) Verified by a Docker parity test
  that diffs our CLI surface against the upstream tool.
- **The set of files patched** and the **shape** of the changes (which keys in
  `settings.json` / `.claude.json`, which shell env).
- **The four model selection modes** and their env-var semantics.

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
- **Ownership journal.** Reversible, self-invalidating revert semantics
  (ADR-004), replacing the original's permanent `.bak` snapshot.
- **Multi-file atomic activation.** A validated `PatchPlan` applied under a
  process lock with atomic renames (ADR-005).

## How we verify Phase 1

A Docker parity image builds **both** tools and a test diffs their CLI surface
(see `docker/parity/` and `tests/parity/`). The `-v`/`--version` format test
runs in CI. If we drift on a Phase-1 surface, CI goes red.

## Summary

| Surface | Phase 1 (clone) | Phase 2 (extend) |
|---------|-----------------|------------------|
| Version format | ✅ byte-for-byte | — |
| Files patched + key shape | ✅ identical | — |
| Model modes | ✅ identical | — |
| Importable API | — | ✅ ours |
| Headless flags | — | ✅ ours |
| Ownership journal | — | ✅ ours |
| Atomic PatchPlan | — | ✅ ours |

See the [Architecture](../ARCHITECTURE.md) for the ADRs behind the Phase 2
extensions.
