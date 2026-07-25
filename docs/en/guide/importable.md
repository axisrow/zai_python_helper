# Importable API

The CLI is a thin shell over a **pure planning core**. That core — the functions
that decide *what* to change — is importable and side-effect-free. You can plan
a provider switch, inspect the exact file deltas, and only then decide whether
to apply them. This is the value proposition of `zai_python_helper`: the
intelligence is a library, the writes are a backend you control.

The public surface is the versioned [`__all__`](../../api/zai_python_helper.md)
contract — anything not in `__all__` is internal and may change.

## Why import it

- **No side effects until you apply.** `plan_zai` reads already-loaded documents
  and returns a `PatchPlan`. Nothing is written.
- **Inspectable.** A `PatchPlan` is an ordered list of `FileDelta`s, each
  addressed by a semantic `FileTag`, not a raw path. You can log them, diff
  them, reject them, or route them to a different backend.
- **Testable.** Pure functions in, `PatchPlan` out. No fixtures of the
  filesystem, no mocking `open()`.
- **Composable.** Build your own UX — a TUI, a web dashboard, a CI gate — on top
  of the same planner the CLI uses.

## Plan a `use zai` switch

```python
from zai_python_helper import (
    ProviderSpec, ModelMode, Region, plan_zai,
    JsonBackend, ShellBackend, Paths, base_url_for_region,
)

spec = ProviderSpec(
    base_url=base_url_for_region(Region.GLOBAL),
    model_mode=ModelMode.ORIGINAL,
)
paths = Paths.default()

plan = plan_zai(
    spec,
    Region.GLOBAL,
    settings_doc=JsonBackend.read(paths.claude_settings),
    claude_json_doc=JsonBackend.read(paths.claude_json),
    zshrc_text=ShellBackend.read(paths.zshrc),
    auth_token="<your Z.ai auth token>",
)

# `plan` is a PatchPlan — an ordered list of FileDeltas. Inspect it:
for delta in plan.deltas:
    print(delta.tag, delta.kind)
```

`plan_default` plans the inverse (`use default`):

```python
from zai_python_helper import plan_default, ProviderSpec

plan = plan_default(spec, settings_doc=..., zshrc_text=...)
```

## Check whether a switch is already active

`postconditions` is a pure predicate — true iff the documents already reflect an
active `use zai`:

```python
from zai_python_helper import postconditions, Region

active = postconditions(Region.GLOBAL, settings_doc=..., zshrc_text=...)
```

## Apply the plan

The plan itself never writes. Apply it through the IO backends — the same ones
the CLI uses — so you keep control of the writes:

```python
# JsonBackend writes atomically (write-temp + rename).
# ShellBackend adds/removes an owned, marker-fenced block (ADR-003).
for delta in plan.deltas:
    delta.apply(...)  # route each delta to the matching backend
```

!!! note
    The exact `FileDelta.apply` signature and the journaling that surrounds it
    live in the [API reference](../../api/zai_python_helper.md). The point here is
    the shape: **plan is pure, apply is IO, you call both.**

## Domain types you'll reach for

| Name | What it is |
|------|------------|
| `ProviderSpec` | The target provider: base URL + model mode. |
| `ModelMode` | Enum: `ORIGINAL`, `DEFAULT`, `SELECT`, `CUSTOM`. |
| `Region` | Enum: `GLOBAL`, `CHINA`. |
| `PatchPlan` | An ordered list of `FileDelta`s describing a full activation. |
| `FileDelta` | One file's intended mutation, addressed by a `FileTag`. |
| `FileTag` | Semantic file id (decoupled from its path). |
| `Paths` | Frozen bundle of every resolved filesystem path the tool touches. |
| `JsonBackend` / `ShellBackend` | Atomic JSON writer / owned-block shell writer. |

For the full list — including ownership journaling (`take_over`, `revert`) and
status detection (`detect_status`, `render_status`) — see the
[API reference](../../api/zai_python_helper.md). It is auto-generated from the
source, so it is never out of sync with the code you're importing.

## See also

- [API reference](../../api/zai_python_helper.md) — every signature, live from
  `__all__`.
- [Architecture](../../ARCHITECTURE.md) — why the core/IO split exists (ADR-001)
  and how it protects the future proxy (ADR-002).
