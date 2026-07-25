# zai_python_helper

> MIT-licensed, **clean-room** Python helper that connects **Claude Code** to the
> **Z.ai GLM Coding Plan** by patching config files — no background service, no
> binary. Designed **importable** (use the planning core as a library) and
> **headless** (every action is a CLI flag).

## What it is

`zai-python-helper` re-points Claude Code (and other coding agents) at the
[Z.ai](https://z.ai) GLM Coding Plan endpoint by editing three files for you:

- `~/.claude/settings.json` — the model + base URL Claude Code reads
- `~/.claude.json` — Claude Code's internal state
- `~/.zshrc` — shell env (managed inside an owned, marker-fenced block — your
  own lines are never deleted, see [Architecture → ADR-003](../ARCHITECTURE.md))

It is **not** a proxy, not a daemon, and not a fork of Z.ai's proprietary
`@z_ai/coding-helper` (npm). It is an independent reimplementation that matches
the *observable behavior* of that tool — reimplemented from scratch and released
under MIT. See [Parity](parity.md) for what is cloned byte-for-byte and what is
our own extension.

## Why

- **No daemon.** A one-shot CLI. Run it, exit, forget it.
- **Reversible.** An [ownership journal](../ARCHITECTURE.md) records the prior
  value of every key before we touch it, so `use default` restores exactly what
  was there — and refuses to clobber a key that changed externally.
- **Atomic.** A multi-file `PatchPlan` is validated, then applied under a
  process lock with atomic renames (ADR-005). Two concurrent `use` calls
  serialize; a crashed run rolls forward on the next invocation.
- **Importable.** The planning core (`plan_zai`, `plan_default`,
  `postconditions`) and domain types (`ProviderSpec`, `ModelMode`, `Region`) are
  pure functions you can call from your own Python. The public surface is the
  versioned [`__all__`](../api/zai_python_helper.md) contract.

## 30-second install

```bash
pip install zai-python-helper
```

Then switch Claude Code to Z.ai:

```bash
# Mode 1 (original): only ANTHROPIC_BASE_URL → Z.ai, let the server pick the model
zai-python-helper use zai

# Provide your Z.ai auth token (or export ZAI_API_KEY)
zai-python-helper use zai --api-key "$ZAI_API_KEY"
```

Back to default Anthropic config:

```bash
zai-python-helper use default
```

Read-only observability and diagnostics:

```bash
zai-python-helper status    # what's currently active, where, and the resolved paths
zai-python-helper doctor    # diagnose the integration end to end
zai-python-helper list      # available Z.ai model presets
```

!!! tip "Want the library, not the CLI?"
    Everything the CLI does, you can do in-process. See
    [Importable API → guide](guide/importable.md).

## Where to go next

- **[Quickstart](quickstart.md)** — `use zai` / `use default` in 5 minutes.
- **[Model modes](guide/modes.md)** — the four ways to select a model.
- **[Importable API](guide/importable.md)** — the value proposition: plan +
  apply from Python.
- **[CLI reference](cli-reference.md)** — every command and flag.
- **[API reference](../api/zai_python_helper.md)** — auto-generated from the code.
- **[Parity](parity.md)** — what mirrors the original, what's ours.
