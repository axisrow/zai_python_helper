# Changelog

## Unreleased

### Breaking changes

- `use default` (revert) and `mcp install` / `mcp uninstall` real runs are
  now silent on success: stdout is empty, matching the pinned upstream
  `@z_ai/coding-helper` 0.0.7 manager surface byte-for-byte (issue #125).
  Scripts that parsed the former status lines (`Reverting to default
  provider …`, `  <id>: installed/removed for <tool>`, REFUSE warnings,
  `updated:` lines, the restart notice) must rely on the exit code and the
  on-disk config instead, or pass the new opt-in `--verbose` flag (issue
  #128) to restore them; `--dry-run` previews are unchanged.

### Added

- Opt-in `--verbose` flag on `use default`, `mcp install`, and `mcp
  uninstall` (issue #128): with the flag the commands print the progress
  lines silenced for parity (issue #125) — the revert header, fail-closed
  REFUSE warnings, `updated: <path>` lines, the restart notice, and the MCP
  `{id}: installed/removed for {tool}` status lines. Without the flag the
  output stays byte-silent permanently. `--verbose` changes stdout only:
  file bytes, modes, and exit codes are identical either way.
- The `warning: recovered from an interrupted prior run` diagnostic (emitted
  when a surviving recovery manifest is replayed before a `use` command) now
  goes to stderr instead of stdout, keeping stdout a strict process contract
  for both `use zai` and `use default`.
- Automatic migration of state from pre-0.1 `~/.zai-python-helper` and the
  former `/var/tmp/zai-python-helper-<uid>` location has been removed. Only the
  canonical XDG state root is used.
- Before upgrading from a pre-0.1 installation, run `zai-python-helper use
  default` with the old version so it can restore the prior provider and retire
  its old ownership journal. After upgrading, run `zai-python-helper use zai` to
  create fresh ownership tracking. If the old version was upgraded without
  deactivation, the old journal cannot be recovered automatically; manually
  restore the default provider before using `use zai`.
