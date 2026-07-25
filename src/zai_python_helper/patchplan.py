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

    **Held under the same :class:`ProcessLock` as a commit** (ADR-005): an
    in-flight transaction's manifest must not be mistaken for crash residue
    by a concurrent invocation. By taking the lock here, a second process
    cannot replay/unlink a manifest that belongs to an active commit. A
    manifest that exists while the lock is free genuinely belongs to a
    crashed prior run and is safe to roll forward.

    Returns:
        The list of tags (e.g. ``["settings", "zshrc"]``) that recovery
        wrote, in manifest order. Empty if no manifest existed.
    """
    with ProcessLock(paths.lock_file):
        entries = _read_manifest(paths.recovery_json)
        if not entries:
            # An absent/empty manifest means nothing to recover. Ensure no
            # stale (e.g. zero-byte) manifest lingers.
            _remove_manifest(paths.recovery_json)
            return []
        applied = []
        for entry in entries:
            _apply_entry(entry)
            applied.append(entry.tag)
        _remove_manifest(paths.recovery_json)
        return applied


def apply_plan_locked(
    paths: Paths,
    plan: PatchPlan,
    *,
    on_locked: Any = None,
) -> list[FileTag]:
    """Commit ``plan`` assuming the caller already holds :class:`ProcessLock`.

    The lock-scoped core of :func:`apply_plan_under_lock`, factored out so the
    CLI can read config, plan, and capture ownership **all inside one held
    lock** (ADR-005 / S3 finding #6: the lock must serialize the state used to
    plan, not only the writes). The caller is responsible for acquiring the
    process lock (and for running :func:`recover` first, also under that lock).

    Contract inside the held lock:
    1. Invoke ``on_locked()`` (if given) — BEFORE the manifest is written. The
       CLI uses this to persist the ownership journal under the same lock.
    2. Write the recovery manifest (so an interrupted run can roll forward).
    3. Write each file via its atomic primitive (the actual commit).
    4. Delete the manifest ONLY after every write succeeds. On a partial
       failure, LEAVE it so the next :func:`recover` rolls forward.

    Returns the tags actually written, in plan order. An all-NOOP plan still
    runs ``on_locked`` (e.g. an idempotent activation that refreshes the
    journal) and returns ``[]``.
    """
    entries: list[_RecoveryEntry] = []
    written: list[FileTag] = []
    for delta in plan.deltas:
        entry = _delta_to_entry(delta, paths)
        if entry is not None:
            entries.append(entry)
            written.append(delta.tag)

    if on_locked is not None:
        on_locked()
    if not entries:
        # No file writes, but a side-effect may have run under the lock (e.g.
        # an idempotent activation that still refreshed the journal).
        return written
    # Persist the manifest BEFORE any managed-file write so a crash at any
    # later point is recoverable. The manifest holds final content, so
    # recovery is a pure replay (no re-read of live state).
    _write_manifest(paths.recovery_json, entries)
    # Commit every file. On FULL success, drop the manifest (commit complete).
    # On a PARTIAL failure, LEAVE the manifest so the next invocation rolls
    # forward — deleting it here would strand mixed state with no recovery
    # path (S3 regression fix, Codex finding #4).
    for entry in entries:
        _apply_entry(entry)
    _remove_manifest(paths.recovery_json)
    return written


def apply_plan_under_lock(
    paths: Paths,
    plan: PatchPlan,
    *,
    on_locked: Any = None,
) -> list[FileTag]:
    """Apply ``plan`` as a locked, recoverable multi-file transaction.

    Convenience wrapper: acquire :class:`ProcessLock`, then call
    :func:`apply_plan_locked`. Use this when the caller does NOT need to read
    config / capture ownership inside the lock. When the planning inputs MUST
    be lock-scoped (the CLI's ``use zai`` — see S3 finding #6), the caller
    acquires ``ProcessLock`` itself and calls :func:`apply_plan_locked`
    directly so config-read → plan → ownership-capture → commit are one
    atomic transaction.

    ``on_locked`` (if given) is invoked while the lock is held and before any
    file write; if it raises, the lock is released and no manifest is written.
    """
    with ProcessLock(paths.lock_file):
        return apply_plan_locked(paths, plan, on_locked=on_locked)
