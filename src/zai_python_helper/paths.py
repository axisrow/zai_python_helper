"""Pure-domain ``Paths`` object: the root configuration object of the project.

Every filesystem path the tool touches is resolved from a single injected
``home`` through ``Paths.from_home`` — ``~/.claude/settings.json``,
``~/.claude.json``, ``~/.zshrc``, ``~/.zai-python-helper/ownership.json``,
lock file, and state directory. This is the single source of truth for
resolved paths: no other module may hard-code ``~/...`` literals.

This module lives in the core layer (pure domain services, no side effects).
``from_home`` is PURE path arithmetic — it performs no IO at all and no
existence checks; directory creation is deferred to the write boundary
in later phases.

Usage contract:
- **Tests** inject a tmp home: ``Paths.from_home(tmp_path)``. This is the
  primary isolation mechanism — a test never resolves the developer's real
  ``$HOME``.
- **Production code** calls ``Paths.default()``, a one-line wrapper over
  ``from_home(Path.home())``. The naming split is what makes test isolation
  provable: tests always inject, never call ``default()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Frozen bundle of every resolved filesystem path the tool touches.

    Frozen so a ``Paths`` instance handed to a handler/backend cannot be
    mutated to silently redirect writes — a tampering guard. All fields are
    ``pathlib.Path``; they are set exclusively by :meth:`from_home`, so an
    instance can never be half-resolved.
    """

    claude_settings: Path
    claude_json: Path
    zshrc: Path
    ownership_json: Path
    lock_file: Path
    state_dir: Path

    @classmethod
    def from_home(cls, home: str | Path) -> Paths:
        """Resolve all paths off ``home`` via pure arithmetic (no IO).

        Accepts ``str | Path`` and coerces to ``pathlib.Path``. Does NOT
        validate existence, create directories, or read anything — it
        succeeds on a non-existent home. Symlinks are left as-is (no
        ``.resolve()``).

        The resolved paths:
        - ``claude_settings``   = ``home / ".claude" / "settings.json"``
        - ``claude_json``       = ``home / ".claude.json"``
        - ``zshrc``             = ``home / ".zshrc"``
        - ``ownership_json``   = ``home / ".zai-python-helper" / "ownership.json"``
        - ``lock_file``         = ``home / ".zai-python-helper" / "lock"``
        - ``state_dir``         = ``home / ".zai-python-helper" / "state"``
        """
        h = Path(home)
        state_dir = h / ".zai-python-helper" / "state"
        helper_dir = h / ".zai-python-helper"
        return cls(
            claude_settings=h / ".claude" / "settings.json",
            claude_json=h / ".claude.json",
            zshrc=h / ".zshrc",
            ownership_json=helper_dir / "ownership.json",
            lock_file=helper_dir / "lock",
            state_dir=state_dir,
        )

    @classmethod
    def default(cls) -> Paths:
        """Production entry point: ``from_home(Path.home())``.

        A one-line thin wrapper with no alternate resolution path.
        Tests never call this — they inject ``tmp_path`` via :meth:`from_home`
        directly; that naming split is what makes test isolation provable.
        """
        return cls.from_home(Path.home())
