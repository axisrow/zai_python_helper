# Architecture — zai_python_helper

> Living document. Describes the **shape** of the code, the decisions that shape is built on, and where it's going.
> v1 scope = config-patching CLI (no proxy). v2/v3 are future, but v1 is laid out so they slot in without rewrites.

## TL;DR roadmap

| Phase | What | Status |
|-------|------|--------|
| **v1** | Config-patching CLI. `use zai` / `use default` edit tool config files directly (Claude Code, OpenCode, Crush, Factory Droid). No background process. **Pure Python.** | 🚧 in progress (epic #1) |
| **v2** | Optional **Python proxy-router** (local `127.0.0.1:PORT`, multi-provider, policy: round-robin / failover-on-429 / balance-aware). Real seamless failover. Same `core` logic, new `network` backend. **Pure Python.** | planned |
| **v3** | Native **desktop app** (Tauri) — status dashboard, switches via the v2 proxy API (or CLI subprocess in v1 mode). | planned |

> **Language policy:** v1 and v2 are **pure Python** — one stack, shared `core/`. Ports to other languages (Go, Rust, a Tauri/JS desktop shell) are *explicitly deferred* and only reconsidered per-phase when a concrete need appears (v3 desktop is the first place a non-Python component is even on the table). Don't introduce a second language to satisfy a hypothetical — YAGNI.

The order matters: **config-patch first (proven, shippable), proxy second (unlocks real seamless), desktop third (needs the proxy to be worth it).** Each phase makes the next possible; none requires rewriting the previous.

---

## ADR-001: Core / IO split (HARD)

**Context.** v1 patches config files. v2 will route traffic through a live proxy. v3 will drive it from a desktop UI. If switching logic lives next to file-writing, every later phase rewrites it.

**Decision.** All switching **logic** is pure and IO-free. The only thing that varies is **how a computed change gets applied** (file write in v1, network call in v2).

```
cli.py            argparse + output only. Thin.
─────────────────────────────────────────────
core/             PURE logic, no IO. Reused by every phase.
  apply_zai(doc) / apply_default(doc)   → desired config
  resolve_key()                         → key from flag/env/getpass/.bak
  regions.py                            → endpoint constants
  postconditions(doc)                   → predicate
─────────────────────────────────────────────
backends.py        IO. v1: write files (JsonBackend, ShellBackend).
proxy.py           v2 stub — same apply() surface, network backend.
```

**Consequence (the rule that protects v2).**
The core functions never open a socket or a file. They take a parsed document and return a parsed document. A **backend** is the thing that turns a desired-doc into reality:

- v1 backend: `apply(desired, path)` → atomic file write.
- v2 backend: `apply(desired)` → tell the running proxy which provider is active.

Because core is shared, switching policy (which provider, which region, failover rules) lives in **one** place and is identical whether the user typed `use zai` in a shell or clicked it in the desktop app.

**Status:** Accepted. Enforced by review — no `open()`, no `requests`/`httpx` in `core/`.

---

## ADR-002: v2 proxy-router will be Python

**Context.** The v2 proxy is a long-running local service. Two honest options: Python (same stack as the CLI, shares `core/` directly) or Go (fast, light daemon — what the sibling project's Moon Bridge is).

**Decision.** Python. We accept the daemon-overhead tradeoff to keep a single language and to let the proxy import `core/` directly instead of re-implementing policy in a second language. If profiling later shows the proxy is a bottleneck (it almost certainly won't for a localhost LLM-traffic router), revisit then.

**Status:** Accepted for v2 planning. Does **not** affect v1.

---

## Why not FastAPI now

FastAPI (or any always-on HTTP layer) only earns its keep when there's a long-running daemon to wrap. v1 has none — it's a one-shot CLI. Adding an HTTP server now would be speculative surface area with no caller.

FastAPI arrives **with the v2 proxy**, as its control plane: the desktop app and any automation talk to the proxy's API, the proxy holds the live provider state and routing policy. The CLI keeps working standalone (v1 file mode) and gains a `--proxy` mode that talks to the same API. One daemon, many faces.

---

## Constraints carried over (proven in sibling projects)

- **Idempotent** — `use zai` twice = identical result, second `--dry-run` diff empty.
- **JSON deep-merge discipline** — never clobber keys we don't own.
- **Secrets never logged** — API key / `ANTHROPIC_AUTH_TOKEN` redacted in all output, diffs, errors.
- **`--dry-run` writes nothing** — `difflib.unified_diff` only.
- **Error contract** — `ZaiPythonHelperError` → one-line `error: <msg>` + exit 1; full traceback only with `--debug`.
- **HOME isolation in tests** — `monkeypatch` HOME, `Paths(injected_home)` seam.

---

## Open architecture questions (defer to the phase that needs them)

- v2 proxy: where does live usage/balance state live (in-memory vs sqlite)? → decide at v2.
- v2 policy engine: pluggable strategies vs fixed enum? → decide at v2.
- v3 desktop: Tauri (Rust+web) confirmed; IPC = subprocess-to-CLI vs HTTP-to-proxy? → decide at v3, leans HTTP once v2 exists.
