# zai_python_helper

MIT-licensed, clean-room Python CLI that connects **Claude Code** to the **Z.ai GLM Coding Plan** by patching `~/.claude/settings.json`, `~/.claude.json`, and `~/.zshrc` — without running any background service or binary.

## What it does (v0.1)

Connect Claude Code to Z.ai with **4 model selection modes**:

```bash
# Mode 1: Original — only ANTHROPIC_BASE_URL (server decides)
zai-python-helper use zai

# Mode 2: Default — use preset models
zai-python-helper use zai --mode default

# Mode 3: Select — choose from available presets
zai-python-helper use zai --mode select --model glm-4-plus

# Mode 4: Custom — provide your own model ID
zai-python-helper use zai --mode custom --model "my-custom-model" --name "My Model"

# List available models
zai-python-helper list
```

Restore default Anthropic configuration:

```bash
zai-python-helper use default
```

## Model Selection Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **original** | Only `ANTHROPIC_BASE_URL` → Z.ai endpoint. Let server decide. | Like original `@z_ai/coding-helper` |
| **default** | Use preset models via `ANTHROPIC_DEFAULT_*_MODEL` | Automatic Z.ai model selection |
| **select** | Choose specific model from presets | Explicit model control |
| **custom** | Provide custom model ID | Beta models, custom deployments |

See [issue #10](../../issues/10) for detailed architecture.

## Documentation

This package is **importable** — the pure planning core (`plan_zai`, `plan_default`,
`postconditions`) and domain types (`ProviderSpec`, `ModelMode`, `Region`) can be
used as a library. The public surface is the versioned `__all__` contract (issue #18).

- **[`llms.txt`](llms.txt)** — LLM/agent entry file ([llmstxt.org](https://llmstxt.org)):
  what this is, install, a 30-second headless example, and the full public API as
  `name → signature → one-line`.
- **[`docs/api/zai_python_helper.md`](docs/api/zai_python_helper.md)** — auto-generated
  API reference (signatures + docstrings + enum members), one file for the whole
  public surface.
- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — design, ADRs, module layout.

Both API docs are **auto-generated from the source** (FastAPI-style: the code is the
source of truth, docs are a derivative). Regenerate with `make docs`; CI fails if the
checked-in docs drift from the code.

```python
from zai_python_helper import (
    ProviderSpec, ModelMode, Region, plan_zai,
    JsonBackend, ShellBackend, Paths, base_url_for_region,
)

spec = ProviderSpec(
    base_url=base_url_for_region(Region.GLOBAL),
    model_mode=ModelMode.ORIGINAL,
)
plan = plan_zai(
    spec, Region.GLOBAL,
    settings_doc=JsonBackend.read(Paths.default().claude_settings),
    claude_json_doc=JsonBackend.read(Paths.default().claude_json),
    zshrc_text=ShellBackend.read(Paths.default().zshrc),
    auth_token="<your Z.ai auth token>",
)
```

## Status

🚧 Alpha. Model selection CLI implemented. Full config patching (ownership journal, multi-file PatchPlan) tracked in [epic #1](../../issues/1).

## License

MIT — see [LICENSE](LICENSE).

## Relationship to `@z_ai/coding-helper`

This is an **independent clean-room reimplementation**, behavior-compatible with Z.ai's proprietary `@z_ai/coding-helper` (npm). It is **not** a fork or translation of that package's source. The Z.ai package is proprietary (Z.ai/Zhipu); this project shares none of its code — only the observable behavior of how Claude Code is configured to talk to the GLM Coding Plan, reimplemented from scratch and released under MIT.
