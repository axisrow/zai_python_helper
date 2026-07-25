"""Multi-file PatchPlan execution: lock + staged commit + recovery (ADR-005).

Activating Claude Code touches up to three files. A per-file atomic write
(:mod:`zai_python_helper.backends`) makes each file safe on its own, but a
crash *between* files — or two concurrent ``use`` invocations — leaves mixed
state. This module makes the whole multi-file activation a transaction:

1. **Process lock** (:class:`ProcessLock`): an exclusive ``fcntl.flock`` on
   ``~/.zai-python-helper/lock``. Two concurrent ``use`` calls serialize on
   it — one runs to completion before the other starts.
2. **Staged commit** (:func:`apply_plan_under_lock`): before touching any
   managed file, write a ``recovery.json`` manifest of the *final* content
   of every file the plan will write. Then write each file via the existing
   atomic primitive. On success, delete the manifest.
3. **Recovery** (:func:`recover`): on startup, if a manifest survives from
   an interrupted run, replay every file in it (idempotent — atomic writes
   overwrite) and clear the manifest. This is "roll-forward": we never try
   to *undo* a partial activation; we *complete* it to the intended state.

Layering (ADR-001): the planner is pure and emits a :class:`PatchPlan` of
deltas; this module is the IO executor that turns those deltas into a locked,
recoverable, multi-file commit. It imports only the planner data types +
backends — it owns no domain logic.

Both ``recovery.json`` and the journal may carry credentials (settings.json
content), so they are written mode ``0600``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import threading as _threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.paths import Paths

# Manifest and journal may carry credentials (settings.json content) → 0600,
# same posture as the secrets file. Reuse ownership's secret-grade atomic
# writer so both stay consistent.
_SECURE_FILE_MODE = 0o600


# ---------------------------------------------------------------------------
# Process lock
# ---------------------------------------------------------------------------


# On BSD-derived systems (macOS), ``flock`` is associated with the (process,
# file) pair rather than the open file description: a SECOND ``flock`` on a
# freshly-opened fd *within the same process* is treated as a re-acquire and
# does NOT block, even though a different process would block. That means a
# pure-flock lock cannot serialize two THREADS of one process — which is
# exactly what our concurrency tests (and a multi-threaded caller) need.
#
# We therefore layer an in-process ``threading.Lock`` (one per resolved path)
# UNDER the ``flock``: the threading.Lock serializes threads within this
# process, and ``flock`` serializes separate processes. Together they make
# the lock correct both cross-thread and cross-process on every platform.
_INTRA_LOCKS_GUARD = _threading.Lock()
_INTRA_LOCKS: dict[str, _threading.Lock] = {}


def _intra_lock(path: Path) -> _threading.Lock:
    """Return the process-wide threading.Lock keyed by the lock-file path."""
    key = str(path)
    with _INTRA_LOCKS_GUARD:
        lock = _INTRA_LOCKS.get(key)
        if lock is None:
            lock = _threading.Lock()
            _INTRA_LOCKS[key] = lock
        return lock


class ProcessLock:
    """Exclusive lock serializing concurrent ``use`` invocations (ADR-005).

    Two-layer, for correctness on every platform:

    1. An in-process ``threading.Lock`` (one per resolved ``path``) serializes
       THREADS of this process. Needed because BSD ``flock`` does not block a
       second fd opened by the same process (see the module note above).
    2. A blocking ``fcntl.flock(LOCK_EX)`` on ``path`` serializes separate
       PROCESSES (and survives the process if it crashes — the lock is
       released when the holding fd closes / the process exits).

    A context manager: :meth:`__enter__` takes both layers, :meth:`__exit__`
    releases both. Not reentrant: a nested ``with ProcessLock(p)`` in the same
    thread WILL deadlock (the threading.Lock is non-reentrant) — callers
    acquire once per activation.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._intra: _threading.Lock | None = None
        self._held_intra = False

    def acquire(self) -> None:
        """Take the in-process lock, then open the file and take flock."""
        # 1) In-process serialization (threads).
        intra = _intra_lock(self.path)
        intra.acquire()
        self._intra = intra
        self._held_intra = True
        # 2) Cross-process serialization (flock). Create the file + parent dir.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os_open(self.path)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException:
            # Close the fd we opened (if flock failed) and release the intra
            # lock — never hold one layer without the other, never leak the fd.
            # release() is unreachable here because acquire() is raising, so we
            # clean up explicitly before re-raising.
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    close_fd(self._fd)
                self._fd = None
            self._release_intra()
            raise

    def _release_intra(self) -> None:
        if self._held_intra and self._intra is not None:
            self._intra.release()
            self._held_intra = False

    def release(self) -> None:
        """Release flock + close the fd, then release the in-process lock."""
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                close_fd(self._fd)
            self._fd = None
        self._release_intra()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def os_open(path: Path) -> int:
    """Open ``path`` for flock (creating it). Isolated for test monkeypatching."""
    import os

    return os.open(str(path), os.O_RDWR | os.O_CREAT, _SECURE_FILE_MODE)


def close_fd(fd: int) -> None:
    """Close an fd. Isolated for test monkeypatching."""
    import os

    os.close(fd)


# ---------------------------------------------------------------------------
# Recovery manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RecoveryEntry:
    """One file's intended final content, persisted in the recovery manifest.

    Attributes:
        tag: The semantic :class:`FileTag` (for human-readable manifest).
        path: Absolute path of the target file (the manifest is bound to the
            ``$HOME`` it was written under).
        kind: ``"json"`` or ``"text"`` — how to interpret ``content``.
        content: The EXACT bytes (as a ``str``; UTF-8) of the final file.
            Recovery replays this verbatim, so it is independent of re-reading
            the current state.
    """

    tag: str
    path: str
    kind: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "path": self.path,
            "kind": self.kind,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _RecoveryEntry:
        return cls(
            tag=str(data.get("tag", "")),
            path=str(data.get("path", "")),
            kind=str(data.get("kind", "text")),
            content=str(data.get("content", "")),
        )


def _delta_to_entry(delta: FileDelta, paths: Paths) -> _RecoveryEntry | None:
    """Render a non-NOOP delta into a recovery entry, or ``None`` for NOOP.

    The entry captures the FINAL content the file should have, so recovery
    does not depend on re-reading live state (which may be partially written
    after a crash). JSON deltas are rendered to the exact on-disk text; text
    deltas (``.zshrc``) are stored as-is.
    """
    if delta.kind == DeltaKind.NOOP:
        return None

    tag = delta.tag
    path = _tag_path(paths, tag)
    if delta.kind == DeltaKind.WRITE_JSON:
        from zai_python_helper.backends import JsonBackend

        content = JsonBackend.render(delta.content)
        kind = "json"
    else:  # WRITE_TEXT
        content = delta.content
        kind = "text"
    return _RecoveryEntry(
        tag=tag.value, path=str(path), kind=kind, content=content
    )


def _tag_path(paths: Paths, tag: FileTag) -> Path:
    """Map a semantic tag to its resolved path (mirrors the CLI's mapping)."""
    if tag == FileTag.SETTINGS:
        return paths.claude_settings
    if tag == FileTag.CLAUDE_JSON:
        return paths.claude_json
    if tag == FileTag.ZSHRC:
        return paths.zshrc
    raise ValueError(f"Unknown FileTag: {tag!r}")  # pragma: no cover - enum-closed


def _write_manifest(path: Path, entries: list[_RecoveryEntry]) -> None:
    """Persist the recovery manifest atomically at 0600 (may carry secrets)."""
    payload = {"entries": [e.to_dict() for e in entries]}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    from zai_python_helper.ownership import _atomic_write_secret

    _atomic_write_secret(path, text.encode("utf-8"))


def _read_manifest(path: Path) -> list[_RecoveryEntry]:
    """Parse a recovery manifest → entries, or ``[]`` if absent/corrupt.

    A corrupt manifest is logged-as-empty rather than fatal: recovery is a
    best-effort roll-forward, and a manifest we cannot parse cannot guide a
    replay. (The caller has already warned the user that the prior run did
    not finish cleanly.)
    """
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_entries = doc.get("entries", []) if isinstance(doc, dict) else []
    return [
        _RecoveryEntry.from_dict(e)
        for e in raw_entries
        if isinstance(e, dict)
    ]


def _remove_manifest(path: Path) -> None:
    """Delete the recovery manifest (commit complete). Best-effort + silent."""
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _apply_entry(entry: _RecoveryEntry) -> None:
    """Write one recovery entry to disk atomically (idempotent replay)."""
    from zai_python_helper.backends import atomic_write_bytes

    atomic_write_bytes(Path(entry.path), entry.content.encode("utf-8"))


# ---------------------------------------------------------------------------
# Public transaction API
# ---------------------------------------------------------------------------


def has_pending_recovery(paths: Paths) -> bool:
    """True iff a recovery manifest from a prior interrupted run exists.

    Callers call :func:`recover` first on every invocation: if this is True,
    the previous ``use`` did not finish cleanly and its manifest must be
    replayed before a new run proceeds.
    """
    return paths.recovery_json.exists()


def recover(paths: Paths) -> list[str]:
    """Replay a surviving recovery manifest; return the tags that were applied.

    Roll-forward semantics (ADR-005): we do NOT try to undo a partial
    activation — we COMPLETE it to the intended state by writing every file's
    recorded final content. Each write is atomic and idempotent, so replaying
    a fully-applied manifest is harmless, and replaying a half-applied one
    finishes the job. The manifest is deleted once replayed.

    Returns:
        The list of tags (e.g. ``["settings", "zshrc"]``) that recovery
        wrote, in manifest order. Empty if no manifest existed.
    """
    entries = _read_manifest(paths.recovery_json)
    if not entries:
        # An absent/empty manifest means nothing to recover. Ensure no stale
        # (e.g. zero-byte) manifest lingers.
        _remove_manifest(paths.recovery_json)
        return []
    applied = []
    for entry in entries:
        _apply_entry(entry)
        applied.append(entry.tag)
    _remove_manifest(paths.recovery_json)
    return applied


def apply_plan_under_lock(
    paths: Paths,
    plan: PatchPlan,
    *,
    on_locked: Any = None,
) -> list[FileTag]:
    """Apply ``plan`` as a locked, recoverable multi-file transaction.

    Contract (ADR-005):

    1. Build recovery entries for every non-NOOP delta (the FINAL content).
    2. If there is nothing to write, short-circuit (no manifest, no lock) —
       UNLESS ``on_locked`` is given, in which case the lock is still taken
       so the side-effect (e.g. writing the ownership journal) is serialized.
    3. Acquire the process lock (serializes concurrent activations).
    4. Invoke ``on_locked()`` (if given) — under the lock, BEFORE the manifest
       is written. Used by the CLI to persist the ownership journal so the
       journal and the file commit are consistent under one lock.
    5. Write the recovery manifest (so an interrupted run can roll forward).
    6. Write each file via its atomic primitive (the actual commit).
    7. Delete the manifest — commit complete.

    Args:
        paths: Resolved paths (lock file + recovery manifest location).
        plan: The fully-validated PatchPlan to commit.
        on_locked: Optional zero-arg callable invoked while the lock is held
            and before any file write. If it raises, the lock is released and
            no manifest is written (the transaction aborts cleanly).

    Returns the tags actually written, in plan order. ``--dry-run`` callers
    do NOT call this (they preview only); this function always writes.

    The lock is held for the whole on_locked→manifest→write→un-manifest
    window, so two concurrent ``use`` calls cannot interleave their commits
    or leave a manifest from one run to be replayed against the other's
    partial state.
    """
    entries: list[_RecoveryEntry] = []
    written: list[FileTag] = []
    for delta in plan.deltas:
        entry = _delta_to_entry(delta, paths)
        if entry is not None:
            entries.append(entry)
            written.append(delta.tag)

    if not entries and on_locked is None:
        # All-NOOP plan with no side-effect to serialize: nothing to commit.
        return []

    with ProcessLock(paths.lock_file):
        if on_locked is not None:
            on_locked()
        if not entries:
            # No file writes, but a side-effect ran under the lock (e.g. an
            # idempotent activation that still refreshed the journal).
            return []
        # Persist the manifest BEFORE any managed-file write so a crash at
        # any later point is recoverable. The manifest holds final content,
        # so recovery is a pure replay (no re-read of live state).
        _write_manifest(paths.recovery_json, entries)
        try:
            for entry in entries:
                _apply_entry(entry)
        finally:
            # Whether the writes fully succeeded or partially failed, the
            # manifest has served its purpose for THIS run: either commit is
            # complete (delete it) or a partial failure left mixed state that
            # the next invocation's recover() will roll forward. In both
            # cases the manifest should not survive a clean exit.
            #
            # NOTE: on a hard crash (SIGKILL) the `finally` does not run, so
            # the manifest survives and the next invocation replays it —
            # exactly the roll-forward guarantee ADR-005 requires.
            _remove_manifest(paths.recovery_json)

    return written
