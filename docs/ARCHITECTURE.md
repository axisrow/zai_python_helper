# Architecture — zai_python_helper

> Living document. Describes the **shape** of the code, the decisions that shape is built on, and where it's going.
> v1 scope = config-patching CLI (no proxy). v2/v3 are future, but v1 is laid out so they slot in without rewrites.

## TL;DR roadmap

| Phase | What | Status |
|-------|------|--------|
| **v1** | Config-patching CLI. `use zai` / `use default` edit tool config files directly (Claude Code, OpenCode, Crush, Factory Droid). No background process. **Pure Python.** | 🚧 in progress (epic #1) |
| **v2** | Optional **Python proxy-router** (local `127.0.0.1:PORT`, multi-provider, policy: round-robin / failover-on-429 / balance-aware). Real seamless failover. Shares domain types with v1 planner; adds a separate routing core + network data plane. **Pure Python.** | planned |
| **v3** | Native **desktop app** (Tauri) — status dashboard, switches via the v2 proxy API (or CLI subprocess in v1 mode). | planned |

> **Language policy:** v1 and v2 are **pure Python** — one stack, shared `core/`. Ports to other languages (Go, Rust, a Tauri/JS desktop shell) are *explicitly deferred* and only reconsidered per-phase when a concrete need appears (v3 desktop is the first place a non-Python component is even on the table). Don't introduce a second language to satisfy a hypothetical — YAGNI.

The order matters: **config-patch first (proven, shippable), proxy second (unlocks real seamless), desktop third (needs the proxy to be worth it).** Each phase makes the next possible; none requires rewriting the previous.

---

## ADR-001: Core / IO split, with TWO pure cores (HARD)

> Amended after architecture review (Codex, 2026-07-24). The original "one `apply()` spans file patching and live routing" was a false substitution — corrected here.

**Context.** v1 patches config files. v2 will route live traffic through a proxy. v3 drives it from a desktop UI. If switching logic lives next to file-writing, every later phase rewrites it — **but** "switching logic" is not one thing.

**Decision.** There are **two distinct pure domains**, and only honest shared types cross between them. Do not collapse them into one `apply()`.

```
cli.py            argparse + output only. Thin.
─────────────────────────────────────────────────────────────
SHARED DOMAIN TYPES (pure, IO-free, used by BOTH cores):
  ProviderSpec   Region   CredentialRef   RoutingPolicy
─────────────────────────────────────────────────────────────
core/planner/     PURE — ToolConfigPlanner (v1 domain)
  plan_zai(doc) / plan_default(doc)   → PatchPlan (desired doc deltas)
  postconditions(doc)                 → predicate
  ◆ Never reads env, never prompts, never opens files.
core/router/      PURE — RouterPolicy (v2 domain, stubbed in v1)
  choose_attempt(request, policy, health) → AttemptPlan
  ◆ Per-request provider selection from capability/quota/breaker/retry.
─────────────────────────────────────────────────────────────
io/               IO layer (outside core)
  resolve_key()   → CredentialRef   (reads env/getpass/ownership journal — NOT pure)
  backends.py     JsonBackend, ShellBackend — atomic file writes
  proxy.py        v2 data plane + control client (network)
```

**Why two cores, not one `apply()`.** A file backend applies a *document* to desired state. A proxy control API changes *durable policy*. A proxy data plane *selects and executes requests*. These have different inputs, postconditions, and failure modes — calling all three `apply()` creates false substitutability. The Moon Bridge sibling already shows the correct shape: one pure Codex transform points the client at a stable endpoint, a separate model owns providers and routing.

**The rule that protects v2.** `core/planner/` never opens a file or socket; it turns a parsed doc into a `PatchPlan` of deltas. `core/router/` never touches a document; it turns a request + policy + health into an `AttemptPlan`. `resolve_key()` is **not** in core — it does IO (env/getpass/ownership journal) and lives in `io/`. In v2, the planner is reused **once**, at onboarding, to point tools at the stable proxy endpoint; thereafter the router owns live selection.

**Status:** Accepted. Enforced by review — no `open()`, no `requests`/`httpx`, no `os.environ`/`getpass` in `core/`.

**Config file mode parity:** v1 config files intentionally use upstream's
`0644` mode for strict Phase-1 byte-for-byte parity. This is not a file-mode
security guarantee for credentials in those configs; the ownership journal,
recovery manifest, and secrets file remain separately protected at `0600`.

---

## ADR-002: v2 proxy-router will be Python

**Context.** The v2 proxy is a long-running local service. Two honest options: Python (same stack as the CLI, shares `core/` directly) or Go (fast, light daemon — what the sibling project's Moon Bridge is).

**Decision.** Python. We accept the daemon-overhead tradeoff to keep a single language and to let the proxy import `core/` directly instead of re-implementing policy in a second language. If profiling later shows the proxy is a bottleneck (it almost certainly won't for a localhost LLM-traffic router), revisit then.

**Status:** Accepted for v2 planning. Does **not** affect v1.

---

## Why not FastAPI now

FastAPI (or any always-on HTTP layer) only earns its keep when there's a long-running daemon to wrap. v1 has none — it's a one-shot CLI. Adding an HTTP server now would be speculative surface area with no caller.

The v2 daemon **must already accept HTTP** from Claude/OpenCode (the data plane). Reusing that same process and listener for a handful of `/control/v1/*` JSON endpoints is lighter than bolting on a second transport. **Start with Starlette/Uvicorn and four versioned routes** (health, state, policy-update, metrics-snapshot); reach for FastAPI only if generated OpenAPI clients become an actual v3 deliverable. **Do not** introduce Unix-socket JSON-RPC — the data plane still needs TCP HTTP, a second transport buys nothing and complicates Windows + the Tauri bridge.

**Local-control security (v2, non-negotiable):** bind **loopback only**, no permissive CORS, require a random control token stored `0600`, validate `Origin`, and the **Tauri Rust backend** (not arbitrary webview JS) holds the token.

---

## ADR-003: `.zshrc` via owned marker-fenced block (HARD)

> Corrected after architecture review. The original epic spec deleted user-owned `ANTHROPIC_*` export lines and left `use default` a no-op there — that is **destructive and not reversible**. Replaced.

**Context.** Shell exports of `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` can override `settings.json`. v1 must neutralize that conflict **without deleting lines the user wrote**, and must be able to undo it.

**Decision.** Never delete foreign shell lines. Write an **owned, marker-fenced block** (exactly the proven pattern from the Moon Bridge sibling's `ShellBackend`):

```sh
# >>> zai-python-helper managed >>>
# (we do NOT export ANTHROPIC_* here; presence is tracked so status/doctor
#  can warn if the user re-exports them elsewhere)
# <<< zai-python-helper managed <<<
```

`use zai` installs the block (idempotent); `use default` removes **only that block**. Unrelated lines round-trip untouched. If conflicting `export ANTHROPIC_*` lines exist outside our block, we **do not remove them** — `status`/`doctor` surfaces a warning ("shell env may override settings.json; remove the export or it will win") and the user decides.

**Status:** Accepted. Enforced: the only `.zshrc` mutation is add/remove of our own fenced block.

---

## ADR-004: Ownership journal, not a permanent `.bak`

> Redesigned after architecture review. A permanent first-mutation `.bak` goes stale after later legitimate user edits and can resurrect obsolete credentials on revert.

**Context.** `use default` must restore prior values without guessing. But a snapshot taken at first mutation is wrong months later if the user legitimately changed their key in between.

**Decision.** Replace the `.bak` + sentinel with an **ownership journal**: a `0600` record of, per tool + per key we manage, the **prior value and its presence** at the moment we took ownership, **plus a hash of the current value we set**. On `use default`:

1. if the key's current value still matches what we last set → restore the journaled prior (and its presence);
2. if it **changed since** (user edited it, or another tool did) → **do not overwrite**; surface "key changed externally since activation, not reverting — inspect `<journal>`" and leave it.

This is per-transition and self-invalidating rather than a frozen snapshot. Lives at e.g. `~/.zai-python-helper/ownership.json`, mode `0600`.

**Status:** Accepted for v1. (Note: `CredentialRef` resolution in `io/resolve_key()` may read this journal on revert — IO, not core, consistent with ADR-001.)

---

## ADR-005: Multi-file PatchPlan + process lock + restart notice (HARD)

> Added after architecture review. Atomic rename is safe per-file but not across the multi-file activation; and "seamless" switching is impossible without a proxy.

**Context.** Activating Claude Code touches up to three files (`settings.json`, `.claude.json`, `.zshrc`). Per-file atomic write doesn't make the *operation* atomic: a crash after file two, or two concurrent invocations, leaves mixed state. Separately, claiming "seamless" switching for v1 is false — env can't mutate a running parent process, and Claude hooks affect launched Bash commands, not Claude's own API transport.

**Decision (transactions).** `core/planner/` emits a `PatchPlan`: a fully-validated, ordered list of file deltas **before any write**. Execution then: acquire a **process lock** (flock on a state file), stage all writes, commit via atomic renames, keep a **recovery journal** so an interrupted run can roll forward on next invocation. Two concurrent `use` calls serialize on the lock.

**Decision (restart honesty).** v1 says "**new session / restart recommended for deterministic switching**" whenever it changes files. Seamless (between-request, pre-body-only failover) is explicitly a **v2** property of the proxy, never a v1 claim.

**Status:** Accepted. v1 ships the locked multi-file PatchPlan; v1 copy is honest about restart.

---

## v2 live state (decided, deferred implementation)

> Resolved by architecture review; recorded so v1 doesn't preclude it.

- **Durable** (policy, usage events, completed-request accounting): **SQLite, WAL mode**, owned **exclusively by the daemon** — CLI/Tauri never write it directly.
- **Transient** (per-request selected provider, circuit-breaker, active streams, latency EWMA, balance-with-TTL): **in-memory** in the proxy.
- **Credentials**: OS keychain or a separate `0600` secrets file — **not** ordinary SQLite rows.
- **Discovery**: daemon publishes `instance.json` (port, PID, protocol version, token-file location).
- **No bidirectional sync** of v1 files and v2 state. CLI mode is explicit — `standalone` (patches files), `proxy` (calls control API), or safe auto-detect; `status` surfaces split-brain ("proxy running, but Claude bypasses it").
- **Protocol conformance (v2 prereq, not in v1)**: Claude = Anthropic-shaped traffic; OpenCode/Crush = OpenAI-compatible; Factory Droid = both. Model mapping, tool-call/SSE translation, usage fields, error normalization, and **429 classification** (transient rate-limit vs exhausted-plan — no blind failover) must be specified per front door before v2 ships.

---

## Constraints carried over (proven in sibling projects)

- **Idempotent** — `use zai` twice = identical result, second `--dry-run` diff empty.
- **JSON deep-merge discipline** — never clobber keys we don't own.
- **Secrets never logged** — API key / `ANTHROPIC_AUTH_TOKEN` redacted in all output, diffs, errors.
- **`--dry-run` writes nothing** — `difflib.unified_diff` only.
- **Error contract** — `ZaiPythonHelperError` → one-line `error: <msg>` + exit 1; full traceback only with `--debug`.
- **HOME isolation in tests** — `monkeypatch` HOME, `Paths(injected_home)` seam.

---

## Model Selection Modes (issue #10)

Per issue #10, zai_python_helper supports 4 model selection modes for Z.ai:

### Mode Definitions

1. **ORIGINAL** — Only `ANTHROPIC_BASE_URL`, let server decide
2. **DEFAULT** — Use preset models via `ANTHROPIC_DEFAULT_*_MODEL`
3. **SELECT** — User selects from predefined list of models
4. **CUSTOM** — User provides custom model ID

### Implementation

- **Domain types**: `ModelMode` enum, `ProviderSpec` dataclass in `core/domain.py`
- **Presets**: `ZAI_MODEL_PRESETS` in `constants.py`
- **Planning**: `plan_model_config()`, `generate_model_overrides()` in `core/planner/models.py`
- **CLI**: `--mode`, `--model`, `--list` flags in `cli.py`

### Architecture Compliance

Per ADR-001:
- `core/domain.py` — shared domain types (IO-free)
- `core/planner/models.py` — pure planning functions
- `constants.py` — static configuration
- `cli.py` — thin argparse wrapper

See [issue #10](../../issues/10) for full specification and CLI examples.

## Open architecture questions (defer to the phase that needs them)

- v2 policy engine: pluggable strategies vs fixed enum? → decide at v2.
- v3 desktop: Tauri (Rust+web) confirmed; IPC = subprocess-to-CLI vs HTTP-to-proxy? → decide at v3, leans HTTP once v2 exists.
- (Resolved by architecture review, recorded above: two pure cores — ADR-001; `.zshrc` owned block — ADR-003; ownership journal — ADR-004; multi-file PatchPlan + lock + restart notice — ADR-005; v2 live state = SQLite-WAL durable + in-memory transient + `instance.json` + no bidirectional sync; control plane = loopback Starlette/Uvicorn, not JSON-RPC.)

## Phase-1 parity contract (issue #78)

The Docker parity job compares the pinned `@z_ai/coding-helper@0.0.7` against
this package for every `tool × region × action` cell: four tools, global and
China, and activate/revert/MCP install/MCP uninstall. The verdict is raw
byte-for-byte process and HOME parity: exit code, stdout bytes, stderr bytes,
complete regular-file set, file bytes, and POSIX mode. JSON is not parsed or
canonicalized; formatting and final newlines are part of the contract.

`--dry-run` and `status`, plus the Python ownership journal/backup bookkeeping,
are explicit Python-port extensions without an upstream 0.0.7 analogue. They
are not parity matrix actions and must not be used as parity expectations.
