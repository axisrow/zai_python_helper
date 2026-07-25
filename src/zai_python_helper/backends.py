"""IO backends: turn a :class:`~zai_python_helper.core.planner.FileDelta`
into an on-disk write (ADR-001 IO layer).

Two backends, one per delta kind:

- :class:`JsonBackend` — serializes a ``dict`` to pretty JSON and writes it
  **atomically** (temp file in the same dir → ``fsync`` → ``os.replace``).
  The *merge* of foreign-vs-managed keys is computed upstream by the planner
  (pure); the backend is deliberately dumb — it writes exactly the document
  it is given. Atomicity is per-file (ADR-005 notes multi-file atomicity is a
  transaction concern of the caller; this backend provides the safe
  per-file primitive).

- :class:`ShellBackend` — applies the owned marker-fenced block add/remove
  using the PURE transforms in :mod:`zai_python_helper.shell_block`, then
  writes the result atomically. Foreign lines round-trip untouched because
  the block transforms never touch them.

Both backends are thin: they know HOW to write a file safely, nothing about
WHAT keys we manage. That separation is what lets the planner stay pure.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from zai_python_helper.errors import ConfigurationError
from zai_python_helper.shell_block import (
    install_owned_block,
    owns_owned_block,
    remove_owned_block,
)

# Default mode for newly-created config files. ``0o600`` because settings.json
# may carry an auth token and ``.zshrc`` is user-private by convention.
_SECURE_FILE_MODE = 0o600


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp + fsync + os.replace).

    The temp file is created in the SAME directory as ``path`` so
    ``os.replace`` is a same-filesystem rename (atomic on POSIX). The temp is
    ``fsync``-ed before the rename so a crash after replace never leaves a
    truncated file; the directory is ``fsync``-ed after so the rename itself
    is durable. The parent directory is created if missing.

    Raises:
        ConfigurationError: If the write or replace fails.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp in the same dir → guaranteed same filesystem → atomic rename.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, _SECURE_FILE_MODE)
            os.replace(tmp_path, path)
            # Durability of the rename itself on POSIX needs a dir fsync.
            _fsync_dir(path.parent)
        except Exception:
            # Best-effort cleanup of the temp on any failure path.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except OSError as e:
        raise ConfigurationError(f"Failed to write {path}: {e}") from e


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort ``fsync`` of a directory (ignores unsupported filesystems)."""
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Not all filesystems (network FS, tmpfs in some CI) support dir
        # fsync — durability is best-effort, never a hard failure.
        pass


class JsonBackend:
    """Atomic JSON document writer/reader.

    Stateless: each call takes an explicit path. The merge semantics live in
    the planner; this backend only reads (leniently) and writes (atomically).
    """

    @staticmethod
    def read(path: Path) -> dict[str, Any] | None:
        """Parse ``path`` as JSON → dict, or ``None`` if it does not exist.

        An empty file is treated as ``None`` (no document). A malformed file
        raises :class:`ConfigurationError` rather than crashing with a bare
        ``JSONDecodeError``.
        """
        p = Path(path)
        if not p.exists():
            return None
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigurationError(f"Failed to read {p}: {e}") from e
        if not text.strip():
            return None
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON in {p}: {e}") from e
        if not isinstance(doc, dict):
            raise ConfigurationError(
                f"{p}: expected a JSON object at top level, got {type(doc).__name__}"
            )
        return doc

    @staticmethod
    def write(path: Path, doc: dict[str, Any]) -> None:
        """Serialize ``doc`` to pretty JSON and write it atomically to ``path``.

        ``indent=2`` + ``sort_keys=False`` (preserve insertion order) + a
        trailing newline. Sorted keys would shuffle user-owned keys on every
        write, producing noisy diffs, so we keep insertion order.
        """
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        atomic_write_bytes(Path(path), text.encode("utf-8"))

    @staticmethod
    def render(doc: dict[str, Any]) -> str:
        """Render ``doc`` to the exact on-disk text (for diffing in --dry-run).

        Pure: the bytes this backend WOULD write, without touching the FS.
        """
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


class ShellBackend:
    """Owned marker-fenced block writer for shell rc files (ADR-003).

    Applies the PURE block transforms from :mod:`zai_python_helper.shell_block`
    and writes the result atomically. Foreign lines round-trip untouched.
    """

    @staticmethod
    def read(path: Path) -> str:
        """Return the raw text of ``path``, or ``""`` if it does not exist."""
        p = Path(path)
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigurationError(f"Failed to read {p}: {e}") from e

    @staticmethod
    def install_block(path: Path) -> bool:
        """Install the owned block in ``path``. Returns True if it was added.

        Idempotent: if the block is already present, writes nothing and
        returns False. Foreign lines are never modified. Creates the file if
        absent.
        """
        text = ShellBackend.read(path)
        if owns_owned_block(text):
            return False
        new_text = install_owned_block(text)
        atomic_write_bytes(Path(path), new_text.encode("utf-8"))
        return True

    @staticmethod
    def remove_block(path: Path) -> bool:
        """Remove the owned block from ``path``. Returns True if it was removed.

        Idempotent: if the block is absent, writes nothing and returns False.
        Foreign lines are never modified. A no-op if the file does not exist.
        """
        text = ShellBackend.read(path)
        if not owns_owned_block(text):
            return False
        new_text = remove_owned_block(text)
        atomic_write_bytes(Path(path), new_text.encode("utf-8"))
        return True

    @staticmethod
    def render_with_block(text: str) -> str:
        """Pure: the text this backend WOULD write to install the block."""
        return install_owned_block(text)

    @staticmethod
    def render_without_block(text: str) -> str:
        """Pure: the text this backend WOULD write to remove the block."""
        return remove_owned_block(text)
