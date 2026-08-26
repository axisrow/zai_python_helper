# Quickstart

From zero to Claude Code talking to Z.ai in five minutes.

## 1. Install

```bash
pip install zai-python-helper
```

From source, with docs tooling:

```bash
git clone https://github.com/axisrow/zai_python_helper.git
cd zai_python_helper
pip install -e ".[docs]"
```

Verify:

```bash
zai-python-helper --version
```

## 2. Get a Z.ai auth token

Grab a token from your [Z.ai](https://z.ai) account (GLM Coding Plan). Export it
so the CLI finds it, or pass `--api-key` on the command line:

```bash
export ZAI_API_KEY="<your token>"
```

## 3. Switch to Z.ai

```bash
zai-python-helper use zai
```

This patches `~/.claude/settings.json`, `~/.claude.json`, and `~/.zshrc` to point
Claude Code at the Z.ai Anthropic-compatible endpoint. The prior values are
recorded in an ownership journal so you can revert cleanly.

Prefer a specific model? Pick a [mode](guide/modes.md):

```bash
# Choose a preset model
zai-python-helper use zai --mode select --model glm-4-plus

# Or your own model id
zai-python-helper use zai --mode custom --model "my-model" --name "My Model"

# Preview what would change, write nothing
zai-python-helper use zai --dry-run
```

## 4. Verify

```bash
zai-python-helper status
```

`status` shows the detected provider, the active model mode, the region, and the
resolved paths it touched. If something looks off:

```bash
zai-python-helper doctor
```

`doctor` runs a full diagnostic and reports the first failing check with a fix
hint.

## 5. Revert

```bash
zai-python-helper use default
```

Restores the prior Anthropic configuration recorded at activation. If a key was
edited externally since activation, `use default` **refuses to overwrite it** and
points you at the journal — see [Architecture → ADR-004](../ARCHITECTURE.md).

## Regions

Two regions are supported (see [Model modes](guide/modes.md)):

| Region | Endpoint |
|--------|----------|
| `global` (default) | `https://api.z.ai/api/anthropic` |
| `china` | `https://open.bigmodel.cn/api/anthropic` |

```bash
zai-python-helper use zai --region china
```

---

That's it. Next: the [four model selection modes](guide/modes.md), or the
[importable API](guide/importable.md) if you'd rather call the planner directly.
