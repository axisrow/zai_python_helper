# CLI reference

`zai-python-helper` is a one-shot CLI: argparse subcommands, every option is a
flag, no interactive menu (you only get a prompt if a token is missing and
neither `--api-key` nor `ZAI_API_KEY` is set).

## Global flags

These work **before or after** the subcommand:

| Flag | Effect |
|------|--------|
| `--dry-run` | Preview what would change; write nothing. |
| `--debug` | Show the full Python traceback on error (instead of the one-line message). |
| `-v`, `--version` | Print the bare version string (matches the upstream format, see [Parity](parity.md)). |
| `-h`, `--help` | Help for the command. |

## `list`

Show the available Z.ai model presets.

```bash
zai-python-helper list
zai-python-helper list --format json
```

| Flag | Values | Default |
|------|--------|---------|
| `--format` | `table`, `json` | `table` |

## `use zai`

Make Z.ai the default provider for Claude Code.

```bash
zai-python-helper use zai
zai-python-helper use zai --mode select --model glm-4-plus
zai-python-helper use zai --region china --api-key "$ZAI_API_KEY"
zai-python-helper use zai --dry-run
```

| Flag | Values / meaning | Default |
|------|------------------|---------|
| `--mode` | `original`, `default`, `select`, `custom` | `original` |
| `--model` | model id (for `select` or `custom`) | — |
| `--region` | `global`, `china` | `global` |
| `--api-key` | Z.ai auth token (else `ZAI_API_KEY` env / prompt) | — |
| `--name` | display name (`custom` mode only) | — |
| `--description` | model description (`custom` mode only) | — |
| `--capabilities` | e.g. `effort,thinking` (`custom` mode only) | — |

See [Model modes](guide/modes.md) for what each mode writes.

## `use default`

Revert to the default Anthropic configuration. Restores the prior values
recorded at activation; refuses to clobber a key that changed externally (see
[Architecture → ADR-004](../ARCHITECTURE.md)). Accepts the same flags as
`use zai` (harmless no-ops for revert), so you can pass `--dry-run`.

```bash
zai-python-helper use default
```

## `status`

Read-only observability: the detected provider, active model mode, region, and
the resolved paths the tool touched.

```bash
zai-python-helper status
zai-python-helper status --region china
```

| Flag | Values | Default |
|------|--------|---------|
| `--region` | `global`, `china` | `global` |

## `doctor`

Diagnose the integration end to end. Reports the first failing check with a fix
hint; WARNs alone exit `0`, a FAIL exits non-zero.

```bash
zai-python-helper doctor
```

## Exit codes

The CLI prints `error: <message>` to stderr and exits non-zero on any expected
failure (config error, provider error, validation error). With `--debug` it
re-raises so you get the full traceback. Under normal operation the exit code
is `0` on success.

## See also

- [Quickstart](quickstart.md) — the five-minute path.
- [Importable API](guide/importable.md) — the same power, in-process.
- [API reference](../api/zai_python_helper.md) — the underlying library.
