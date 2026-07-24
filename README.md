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

## Status

🚧 Alpha. Model selection CLI implemented. Full config patching (ownership journal, multi-file PatchPlan) tracked in [epic #1](../../issues/1).

## License

MIT — see [LICENSE](LICENSE).

## Relationship to `@z_ai/coding-helper`

This is an **independent clean-room reimplementation**, behavior-compatible with Z.ai's proprietary `@z_ai/coding-helper` (npm). It is **not** a fork or translation of that package's source. The Z.ai package is proprietary (Z.ai/Zhipu); this project shares none of its code — only the observable behavior of how Claude Code is configured to talk to the GLM Coding Plan, reimplemented from scratch and released under MIT.
