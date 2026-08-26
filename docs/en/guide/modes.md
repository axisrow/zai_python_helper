# Model selection modes

`use zai` can configure Claude Code to talk to Z.ai in **four** ways. The mode
controls *which* environment variables get written and *how much* you constrain
the model.

| Mode | What gets written | Use when |
|------|-------------------|----------|
| **original** | only `ANTHROPIC_BASE_URL` → Z.ai | you want the server to decide the model (matches the original `@z_ai/coding-helper`) |
| **default** | `ANTHROPIC_BASE_URL` + `ANTHROPIC_DEFAULT_*_MODEL` vars | you want Z.ai's preset model chosen automatically |
| **select** | base URL + a specific preset model | you want explicit control over a known preset |
| **custom** | base URL + your own model id + name | beta models, custom deployments |

## original

Only `ANTHROPIC_BASE_URL` is set to the Z.ai endpoint. The server picks the
model.

```bash
zai-python-helper use zai --mode original
```

This is the closest match to the upstream tool's behavior and the **default**
mode when you omit `--mode`.

## default

Z.ai preset models are wired in via the `ANTHROPIC_DEFAULT_*_MODEL` variables,
so Claude Code's built-in model aliasing (opus/sonnet/haiku) maps onto Z.ai
presets automatically.

```bash
zai-python-helper use zai --mode default
```

## select

Pick one specific preset by name. Run `list` to see the available presets:

```bash
zai-python-helper list
```

```bash
zai-python-helper use zai --mode select --model glm-4-plus
```

## custom

Provide your own model id and a display name (and optionally a description and
capabilities). Useful for beta models or a self-hosted endpoint behind the Z.ai
base URL.

```bash
zai-python-helper use zai \
  --mode custom \
  --model "my-custom-model" \
  --name "My Model" \
  --capabilities "effort,thinking"
```

## Upstream reference (`@z_ai/coding-helper` 0.0.7)

Black-box testing of the upstream package confirms that Claude Code does **not**
read a `customModels` array. The upstream tool configures Claude Code with
environment variables in `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<token>",
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "API_TIMEOUT_MS": "3000000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1
  }
}
```

It also merges `"hasCompletedOnboarding": true` into `~/.claude.json` and
preserves unrelated settings. `customModels` is a Factory Droid format, not a
Claude Code format. For a headless upstream setup, use:

```bash
chelper auth glm_coding_plan_global "$ZAI_API_KEY"
chelper auth reload claude
```

The first command validates and stores the token in `~/.chelper/config.yaml`;
the second applies it to Claude Code. `<token>` is an angle-bracket placeholder in
prose, not literal shell syntax — a shell parses an unquoted `<token>` as input
redirection, so pass the real value through an environment variable
(`ZAI_API_KEY` above) rather than pasting the token verbatim. `chelper enter
claude-code` is interactive-first and is not a substitute in a non-TTY script.

## Regions

Every mode accepts `--region`. The region selects the Z.ai endpoint:

- `global` (default) — `https://api.z.ai/api/anthropic`
- `china` — `https://open.bigmodel.cn/api/anthropic`

```bash
zai-python-helper use zai --mode select --model glm-4-plus --region china
```

## Token resolution

The auth token is resolved in this order:

1. `--api-key` on the command line
2. `ZAI_API_KEY` environment variable
3. interactive prompt (only if neither is present)

If you're scripting, always pass `--api-key` or export `ZAI_API_KEY` so the CLI
never blocks on a prompt.

## See also

- [CLI reference](../cli-reference.md) — every flag.
- [Importable API](importable.md) — drive `plan_zai` from Python instead.
