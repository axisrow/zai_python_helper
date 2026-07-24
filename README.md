# zai_python_helper

MIT-licensed, clean-room Python CLI that connects **Claude Code** to the **Z.ai GLM Coding Plan** by patching `~/.claude/settings.json`, `~/.claude.json`, and `~/.zshrc` — without running any background service or binary.

## What it does (planned, v0.1)

One command points Claude Code at Z.ai (`ANTHROPIC_BASE_URL` → `https://api.z.ai/api/anthropic`), another restores the default. No Moon Bridge, no LaunchAgent, no Go — just JSON/shell config edits, fully reversible.

```bash
zai-python-helper use zai        # Claude Code → Z.ai GLM Coding Plan
zai-python-helper use default    # restore previous state
```

## Status

🚧 Pre-alpha. See [issue #1](../../issues/1) for the Claude Code port specification.

## License

MIT — see [LICENSE](LICENSE).

## Relationship to `@z_ai/coding-helper`

This is an **independent clean-room reimplementation**, behavior-compatible with Z.ai's proprietary `@z_ai/coding-helper` (npm). It is **not** a fork or translation of that package's source. The Z.ai package is proprietary (Z.ai/Zhipu); this project shares none of its code — only the observable behavior of how Claude Code is configured to talk to the GLM Coding Plan, reimplemented from scratch and released under MIT.
