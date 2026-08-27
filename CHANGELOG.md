# Changelog

## Unreleased

### Breaking changes

- Automatic migration of state from pre-0.1 `~/.zai-python-helper` and the
  former `/var/tmp/zai-python-helper-<uid>` location has been removed. Only the
  canonical XDG state root is used. After upgrading, run `zai-python-helper use
  zai` again if ownership tracking from an older installation is needed.
