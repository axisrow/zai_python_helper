"""Pure-domain ``Paths`` object: the root configuration object of the project.

Every user configuration path is resolved from a single injected ``home``
through ``Paths.from_home``. Runtime bookkeeping (journal, lock, recovery)
is resolved in an external state root so it does not mutate HOME.

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

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def _state_home_from_env() -> tuple[str, bool]:
    """Return the configured absolute state root, or the secure fallback."""
    override = os.environ.get("ZAI_PYTHON_HELPER_STATE_HOME", "")
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if override and Path(override).is_absolute():
        return override, False
    if xdg and Path(xdg).is_absolute():
        return xdg, False
    return f"/var/tmp/zai-python-helper-{os.getuid()}", True


def _canonical_configured_state_root(path: Path) -> Path:
    """Resolve the existing prefix strictly and allow a nonexistent suffix."""
    missing: list[str] = []
    probe = path
    while not os.path.lexists(probe):
        if probe == probe.parent:
            break
        missing.append(probe.name)
        probe = probe.parent
    try:
        resolved = probe.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"configured state root has a dangling symlink: {path}") from exc
    for part in reversed(missing):
        resolved /= part
    return resolved


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
    def from_home(
        cls,
        home: str | Path,
        cwd: str | Path | None = None,
        *,
        state_home: str | Path | None = None,
    ) -> Paths:
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
        - ``ownership_json``   = external state root / ``ownership.json``
        - ``recovery_json``    = external state root / ``recovery.json``
        - ``lock_file``         = external state root / ``lock``
        - ``state_dir``         = external state root / ``state``
        - ``project_claude_settings`` = ``cwd / ".claude" / "settings.json"``
        - ``local_claude_settings`` = ``cwd / ".claude" / "settings.local.json"``

        Args:
            home: The user home directory.
            cwd: Current working directory for project/local settings. Defaults to
                ``Path.cwd()`` if not provided (for production use). Tests inject
                a specific path to simulate running from a project directory.
        """
        h = Path(home)
        is_fallback = False
        # Runtime bookkeeping is deliberately not part of HOME for the
        # production entry point. ``state_home`` is an injection seam;
        # omitted here it preserves the hermetic legacy layout for tests and
        # library callers that explicitly inject a HOME.
        if state_home is None:
            # /var/tmp is durable across reboots, unlike /tmp.  The directory
            # is created and ownership-checked by ProcessLock before use.
            state_home, is_fallback = _state_home_from_env()
        configured_state_home = Path(state_home)
        home_id = hashlib.sha256(str(h).encode()).hexdigest()[:16]
        # Pin the state root's current symlink target.  All transaction files
        # then use the same canonical tree as the lock, even if the user-level
        # XDG symlink is retargeted while a transaction is running.
        # Preserve the fallback leaf for descriptor validation: resolving it
        # first would follow a pre-created attacker symlink. User-configured
        # roots are canonicalized so all bookkeeping remains pinned together.
        state_root = (
            configured_state_home
            if is_fallback
            else _canonical_configured_state_root(configured_state_home)
        )
        helper_dir = state_root / "zai-python-helper" / home_id
        state_dir = helper_dir / "state"
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
