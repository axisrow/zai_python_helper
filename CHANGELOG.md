# Changelog

## Unreleased

### Breaking changes

- Automatic migration of state from pre-0.1 `~/.zai-python-helper` and the
  former `/var/tmp/zai-python-helper-<uid>` location has been removed. Only the
  canonical XDG state root is used.
- Before upgrading from a pre-0.1 installation, run `zai-python-helper use
  default` with the old version so it can restore the prior provider and retire
  its old ownership journal. After upgrading, run `zai-python-helper use zai` to
  create fresh ownership tracking. If the old version was upgraded without
  deactivation, the old journal cannot be recovered automatically; manually
  restore the default provider before using `use zai`.
