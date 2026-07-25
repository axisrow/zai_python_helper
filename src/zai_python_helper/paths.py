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
    # S6 tool config files (issue #7). Addressed by FileTag.{OPENCODE,CRUSH,
    # FACTORY_DROID}; resolved here so the planner stays path-free.
    opencode: Path
    crush: Path
    factory_droid: Path
    ownership_json: Path
    recovery_json: Path
    lock_file: Path
    state_dir: Path
    # Project-scoped Claude settings (relative to CWD, if any).
    # Added for issue #23: credential egress gap fix.
    project_claude_settings: Path
    local_claude_settings: Path
    # The current working directory at Paths creation (for ancestor discovery).
    # Added for issue #23 ancestor-aware project settings discovery.
    cwd: Path

    @classmethod
    def from_home(cls, home: str | Path, cwd: str | Path | None = None) -> Paths:
        """Resolve all paths off ``home`` via pure arithmetic (no IO).

        Accepts ``str | Path`` and coerces to ``pathlib.Path``. Does NOT
        validate existence, create directories, or read anything — it
        succeeds on a non-existent home. Symlinks are left as-is (no
        ``.resolve()``).

        The resolved paths:
        - ``claude_settings``   = ``home / ".claude" / "settings.json"``
        - ``claude_json``       = ``home / ".claude.json"``
        - ``zshrc``             = ``home / ".zshrc"``
        - ``opencode``          = ``home / ".config" / "opencode" / "opencode.json"``
        - ``crush``             = ``home / ".config" / "crush" / "crush.json"``
        - ``factory_droid``     = ``home / ".factory" / "settings.json"``
        - ``ownership_json``   = ``home / ".zai-python-helper" / "ownership.json"``
        - ``recovery_json``    = ``home / ".zai-python-helper" / "recovery.json"``
        - ``lock_file``         = ``home / ".zai-python-helper" / "lock"``
        - ``state_dir``         = ``home / ".zai-python-helper" / "state"``
        - ``project_claude_settings`` = ``cwd / ".claude" / "settings.json"``
        - ``local_claude_settings`` = ``cwd / ".claude" / "settings.local.json"``

        Args:
            home: The user home directory.
            cwd: Current working directory for project/local settings. Defaults to
                ``Path.cwd()`` if not provided (for production use). Tests inject
                a specific path to simulate running from a project directory.
        """
        h = Path(home)
        state_dir = h / ".zai-python-helper" / "state"
        helper_dir = h / ".zai-python-helper"
        cwd_path = Path(cwd) if cwd is not None else Path.cwd()
        return cls(
            claude_settings=h / ".claude" / "settings.json",
            claude_json=h / ".claude.json",
            zshrc=h / ".zshrc",
            opencode=h / ".config" / "opencode" / "opencode.json",
            crush=h / ".config" / "crush" / "crush.json",
            factory_droid=h / ".factory" / "settings.json",
            ownership_json=helper_dir / "ownership.json",
            recovery_json=helper_dir / "recovery.json",
            lock_file=helper_dir / "lock",
            state_dir=state_dir,
            project_claude_settings=cwd_path / ".claude" / "settings.json",
            local_claude_settings=cwd_path / ".claude" / "settings.local.json",
            cwd=cwd_path,
        )

    @classmethod
    def default(cls) -> Paths:
        """Production entry point: ``from_home(Path.home())``.

        A one-line thin wrapper with no alternate resolution path.
        Tests never call this — they inject ``tmp_path`` via :meth:`from_home`
        directly; that naming split is what makes test isolation provable.
        """
        return cls.from_home(Path.home(), cwd=Path.cwd())
