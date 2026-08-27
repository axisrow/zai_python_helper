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

import base64
import contextlib
import fcntl
import json
import os
import stat
import tempfile
import threading as _threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.paths import Paths

# Manifest and journal may carry credentials (settings.json content) → 0600,
# same posture as the secrets file. Reuse ownership's secret-grade atomic
# writer so both stay consistent.
_SECURE_FILE_MODE = 0o600
_LEGACY_STATE_NAMES = ("ownership.json", "recovery.json")
_LEGACY_HANDOFF_NAME = "legacy-handoff.json"


@dataclass(frozen=True)
class _LegacyGeneration:
    """Persisted generation; ``journal_path`` is comparison metadata only."""

    source: str
    journal_path: Path
    files: dict[str, bytes | None]


def _open_private_parent(path: Path, *, create: bool, harden: bool = False) -> int:
    """Open the state parent without following attacker-controlled entries."""
    if ".." in Path(path.parent).parts:
        raise ValueError(f"state path must not contain '..': {path.parent}")
    parent = Path(os.path.abspath(path.parent))
    parts = parent.parts
    state_root = parent.parent.parent
    private_paths = {parent}
    if parent.parent.name == "zai-python-helper":
        private_paths.add(parent.parent)
    protected_paths = private_paths
    # The fallback root itself is predictable and therefore must also be
    # protected.  Do not infer this from a basename: XDG_STATE_HOME may
    # legitimately live below a user directory with that name.
    if (
        Path(os.path.realpath(state_root.parent))
        == Path(os.path.realpath("/var/tmp"))
        and state_root.name == f"zai-python-helper-{os.getuid()}"
    ):
        private_paths.add(state_root)
        protected_paths.add(state_root)
    fd = os.open(parts[0] or os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path(parts[0] or os.sep)
    try:
        for part in parts[1:]:
            current /= part
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            # macOS exposes /var as a system symlink.  Permit such trusted
            # ancestors, but never follow symlinks once entering our state
            # directory (the predictable attacker-controlled component).
            protected = current in protected_paths
            private = current in private_paths
            flags = os.O_RDONLY | os.O_DIRECTORY
            if protected:
                flags |= os.O_NOFOLLOW
            next_fd = os.open(part, flags, dir_fd=fd)
            try:
                st = os.fstat(next_fd)
                # Only application state directories are tightened; arbitrary
                # existing ancestors such as /tmp or a user's XDG root are untouched.
                if private and st.st_uid != os.getuid():
                    raise PermissionError(f"insecure state directory: {current}")
                if private and st.st_mode & 0o077:
                    if create or harden:
                        os.fchmod(next_fd, 0o700)
                    else:
                        raise PermissionError(f"insecure state directory: {current}")
            except BaseException:
                os.close(next_fd)
                raise
            previous_fd = fd
            # Transfer ownership before close: if close itself fails, the
            # outer finally owns only next_fd and cannot double-close a reused
            # previous descriptor number.
            fd = next_fd
            os.close(previous_fd)
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _ensure_private_parent(path: Path) -> int:
    """Create and pin the state parent directory."""
    return _open_private_parent(path, create=True)


class PinnedStateDirectory:
    """Capability for descriptor-relative state-directory I/O.

    The descriptor is opened and validated once.  Every journal, lock, and
    recovery-manifest operation below it is then addressed by basename via
    ``dir_fd``.  Holding this object is the authority to touch state; there is
    deliberately no path-based fallback.
    """

    def __init__(self, path: Path, fd: int) -> None:
        self.path = Path(path)
        self._fd: int | None = fd

    @classmethod
    def open(
        cls, path: str | Path, *, create: bool = False, harden: bool = False
    ) -> PinnedStateDirectory | None:
        directory = Path(path)
        try:
            fd = _open_private_parent(
                directory / ".state-probe", create=create, harden=harden
            )
        except FileNotFoundError:
            return None
        return cls(directory, fd)

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("pinned state directory is closed")
        return self._fd

    @staticmethod
    def _name(name: str) -> str:
        if not name or name in {".", ".."} or Path(name).name != name:
            raise ValueError(f"state entry must be a basename: {name!r}")
        return name

    def open_file(self, name: str, flags: int, mode: int = _SECURE_FILE_MODE) -> int:
        return os.open(self._name(name), flags | os.O_NOFOLLOW, mode, dir_fd=self.fd)

    def read_text(self, name: str) -> str:
        return _read_at(self.fd, self._name(name))

    def read_bytes(self, name: str) -> bytes:
        fd = self.open_file(name, os.O_RDONLY)
        try:
            stream = os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            raise
        with stream:
            return stream.read()

    def exists(self, name: str) -> bool:
        try:
            fd = self.open_file(name, os.O_RDONLY)
        except FileNotFoundError:
            return False
        else:
            os.close(fd)
            return True

    def atomic_write(self, name: str, data: bytes, mode: int) -> None:
        _atomic_write_at(self.fd, self._name(name), data, mode)

    def unlink(self, name: str, *, missing_ok: bool = True) -> None:
        try:
            os.unlink(self._name(name), dir_fd=self.fd)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def close(self) -> None:
        if self._fd is not None:
            fd = self._fd
            self._fd = None
            os.close(fd)

    def __enter__(self) -> PinnedStateDirectory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def migrate_legacy_state(paths: Paths) -> list[str]:
    """Reconcile older bookkeeping generations into the active state root.

    Older releases used first ``~/.zai-python-helper`` and then a predictable
    runtime tree. Losing or mixing their journal/recovery generation could make
    ``use default`` restore the wrong values. Both old locks are retained for
    the complete transaction, and state that reappears after a lock handoff is
    imported by the next transaction. Fresh installations remain a no-op.
    """
    with state_transaction(paths) as (_lock, moved):
        return moved


@contextlib.contextmanager
def state_transaction(paths: Paths):
    """Hold new and legacy locks for one complete mutating operation.

    The legacy lock lease intentionally outlives migration. An old-version
    process may have started before us but not reached its lock yet; retaining
    the lease through recovery and commit prevents that process from entering
    its legacy critical section midway through the new-root transaction.
    """
    with ProcessLock(paths) as lock:
        if lock.state is None:
            raise RuntimeError("ProcessLock acquired without pinned state")
        with contextlib.ExitStack() as stack:
            sources: dict[str, tuple[Path, PinnedStateDirectory]] = {}
            pending = _read_legacy_handoff(lock.state)
            # Reserve every old lock namespace even when its tree does not yet
            # exist. An already-started old process may be paused before mkdir
            # and must still serialize with this complete transaction.
            legacy_candidates: list[tuple[str, Path, bool]] = []
            runtime_dir = paths.legacy_runtime_dir
            if pending is not None and pending.source == "runtime":
                runtime_dir = runtime_dir or _expected_legacy_runtime_dir(paths)
                if pending.journal_path != runtime_dir / "ownership.json":
                    raise RuntimeError("invalid legacy state handoff record")
            if runtime_dir is not None:
                legacy_candidates.append(
                    ("runtime", runtime_dir, True)
                )
            # The runtime tree superseded the pre-0.1 HOME tree. Acquire it
            # first and migrate its newer state before considering HOME.
            legacy_candidates.append(
                (
                    "home",
                    paths.claude_settings.parent.parent / ".zai-python-helper",
                    True,
                )
            )
            if pending is not None and pending.source == "home":
                expected = legacy_candidates[-1][1] / "ownership.json"
                if pending.journal_path != expected:
                    raise RuntimeError("invalid legacy state handoff record")
            for label, legacy_dir, create in legacy_candidates:
                if legacy_dir == lock.state.path:
                    continue
                try:
                    source = PinnedStateDirectory.open(
                        legacy_dir, create=create, harden=True
                    )
                except OSError:
                    # A foreign-owned predictable /var/tmp root is an attacker
                    # reservation, not an authority and not a reason to deny
                    # the victim access to the new private state root.
                    continue
                if source is None:
                    continue
                stack.enter_context(source)
                stack.enter_context(_locked_legacy_state(source))
                sources[label] = (legacy_dir, source)
            moved = _migrate_legacy_state_locked(paths, lock.state, sources)
            yield lock, moved


def _expected_legacy_runtime_dir(paths: Paths) -> Path:
    """Reconstruct the fixed pre-#116 runtime namespace independent of env."""
    return (
        Path("/var/tmp")
        / f"zai-python-helper-{os.getuid()}"
        / "zai-python-helper"
        / paths.lock_file.parent.name
    )


def _migrate_legacy_state_locked(
    paths: Paths,
    destination: PinnedStateDirectory,
    sources: dict[str, tuple[Path, PinnedStateDirectory]],
) -> list[str]:
    """Mirror one authoritative legacy state generation into the active root.

    ``ownership.json`` and ``recovery.json`` are one generation, never two
    independent migration candidates. The former runtime generation outranks
    pre-0.1 HOME state. A secure active-root handoff record makes mirroring and
    source cleanup resumable if the process exits between filesystem steps.
    """
    pending = _read_legacy_handoff(destination)
    snapshots = {
        label: _snapshot_legacy_state(source)
        for label, (_legacy_dir, source) in sources.items()
    }

    selected: _LegacyGeneration | None = None
    runtime_snapshot = snapshots.get("runtime")
    if runtime_snapshot is not None and _has_legacy_state(runtime_snapshot):
        if not (
            pending is not None
            and pending.source == "runtime"
            and _is_cleanup_residue(runtime_snapshot, pending.files)
        ):
            selected = _live_legacy_generation(sources, "runtime", runtime_snapshot)

    if selected is None and pending is not None:
        live = snapshots.get(pending.source)
        if (
            pending.source == "home"
            and live is not None
            and _has_legacy_state(live)
            and not _is_cleanup_residue(live, pending.files)
        ):
            selected = _live_legacy_generation(sources, "home", live)
        else:
            selected = pending

    home_snapshot = snapshots.get("home")
    if (
        selected is None
        and home_snapshot is not None
        and _has_legacy_state(home_snapshot)
    ):
        selected = _live_legacy_generation(sources, "home", home_snapshot)

    if selected is None:
        return []

    _write_legacy_handoff(destination, selected)

    for name, data in selected.files.items():
        if data is None:
            destination.unlink(name)
            continue
        if name == "recovery.json":
            data = _rewrite_migrated_manifest(
                data,
                selected.journal_path,
                paths.ownership_json,
            )
        destination.atomic_write(name, data, _SECURE_FILE_MODE)

    if selected.source in sources:
        _unlink_legacy_state(sources[selected.source][1])
    if selected.source == "runtime" and "home" in sources:
        _unlink_legacy_state(sources["home"][1])
    _write_legacy_handoff(destination, None)
    return [name for name, data in selected.files.items() if data is not None]


def _live_legacy_generation(
    sources: dict[str, tuple[Path, PinnedStateDirectory]],
    label: str,
    files: dict[str, bytes | None],
) -> _LegacyGeneration:
    legacy_dir = sources[label][0]
    return _LegacyGeneration(label, legacy_dir / "ownership.json", files)


def _snapshot_legacy_state(
    source: PinnedStateDirectory,
) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for name in _LEGACY_STATE_NAMES:
        try:
            snapshot[name] = source.read_bytes(name)
        except FileNotFoundError:
            snapshot[name] = None
    return snapshot


def _has_legacy_state(files: dict[str, bytes | None]) -> bool:
    return any(data is not None for data in files.values())


def _is_cleanup_residue(
    current: dict[str, bytes | None], pending: dict[str, bytes | None]
) -> bool:
    """Return whether live files are an unchanged subset of a pending copy."""
    return all(
        data is None or data == pending[name]
        for name, data in current.items()
    )


def _unlink_legacy_state(source: PinnedStateDirectory) -> None:
    for name in _LEGACY_STATE_NAMES:
        source.unlink(name)


def _read_legacy_handoff(
    destination: PinnedStateDirectory,
) -> _LegacyGeneration | None:
    try:
        raw = destination.read_text(_LEGACY_HANDOFF_NAME)
    except FileNotFoundError:
        return None
    try:
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError
        progress = document.get("in_progress")
        if progress is None:
            return None
        if not isinstance(progress, dict):
            raise ValueError
        label = progress.get("source")
        journal_path = progress.get("journal_path")
        encoded = progress.get("files")
        if (
            label not in {"runtime", "home"}
            or not isinstance(journal_path, str)
            or Path(journal_path).name != "ownership.json"
            or not isinstance(encoded, dict)
        ):
            raise ValueError
        if set(encoded) != set(_LEGACY_STATE_NAMES):
            raise ValueError
        files: dict[str, bytes | None] = {}
        for name in _LEGACY_STATE_NAMES:
            value = encoded[name]
            if value is None:
                files[name] = None
            elif isinstance(value, str):
                files[name] = base64.b64decode(value, validate=True)
            else:
                raise ValueError
        return _LegacyGeneration(label, Path(journal_path), files)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid legacy state handoff record") from exc


def _write_legacy_handoff(
    destination: PinnedStateDirectory,
    progress: _LegacyGeneration | None,
) -> None:
    if progress is None:
        destination.unlink(_LEGACY_HANDOFF_NAME)
        return
    encoded_progress = {
        "source": progress.source,
        "journal_path": str(progress.journal_path),
        "files": {
            name: (
                None
                if progress.files[name] is None
                else base64.b64encode(progress.files[name]).decode()
            )
            for name in _LEGACY_STATE_NAMES
        },
    }
    document = {
        "version": 1,
        "in_progress": encoded_progress,
    }
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    destination.atomic_write(_LEGACY_HANDOFF_NAME, data, _SECURE_FILE_MODE)


@contextlib.contextmanager
def _locked_legacy_state(state: PinnedStateDirectory):
    """Serialize migration with processes still using the legacy state tree."""
    lock_path = state.path / "lock"
    fd = os_open_at(state.fd, lock_path.name, lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            close_fd(fd)


def _rewrite_migrated_manifest(
    data: bytes, legacy_journal: Path, current_journal: Path
) -> bytes:
    """Update a migrated manifest's stale absolute journal reference."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data
    journal = document.get("journal") if isinstance(document, dict) else None
    if not isinstance(journal, dict) or journal.get("path") != str(legacy_journal):
        return data
    journal["path"] = str(current_journal)
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


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
_LOCK_CONTEXT = _threading.local()


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
    releases both. Nesting is rejected before the second lock is acquired.
    No production caller needs nesting, and failing loudly is safer than
    replacing or clearing the outer lock's pinned state capability.
    """

    def __init__(self, target: Paths | str | Path) -> None:
        self.paths = target if isinstance(target, Paths) else None
        self.path = self.paths.lock_file if self.paths is not None else Path(target)
        self._fd: int | None = None
        self._intra: _threading.Lock | None = None
        self._held_intra = False
        # The validated helper directory is pinned for the whole lock scope.
        # State files must be addressed through this descriptor, never by
        # resolving ``self.path`` again (issue #111).
        self.state: PinnedStateDirectory | None = None

    def acquire(self) -> None:
        """Take the in-process lock, then open the file and take flock."""
        if getattr(_LOCK_CONTEXT, "active_lock", None) is not None:
            raise RuntimeError("nested ProcessLock acquisition is forbidden")
        # 1) In-process serialization (threads).
        intra = _intra_lock(self.path)
        intra.acquire()
        self._intra = intra
        self._held_intra = True
        # 2) Cross-process serialization (flock). Create the file + parent dir.
        try:
            parent_fd = _ensure_private_parent(self.path)
            self.state = PinnedStateDirectory(self.path.parent, parent_fd)
            self._fd = os_open_at(parent_fd, self.path.name, self.path)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            _LOCK_CONTEXT.active_lock = self
        except BaseException:
            # Close the fd we opened (if flock failed) and release the intra
            # lock — never hold one layer without the other, never leak the fd.
            # release() is unreachable here because acquire() is raising, so we
            # clean up explicitly before re-raising.
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    close_fd(self._fd)
                self._fd = None
            if self.state is not None:
                with contextlib.suppress(OSError):
                    self.state.close()
                self.state = None
            if getattr(_LOCK_CONTEXT, "active_lock", None) is self:
                _LOCK_CONTEXT.active_lock = None
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
        if self.state is not None:
            with contextlib.suppress(OSError):
                self.state.close()
            self.state = None
        if getattr(_LOCK_CONTEXT, "active_lock", None) is self:
            _LOCK_CONTEXT.active_lock = None
        self._release_intra()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def os_open_at(parent_fd: int, name: str, path: Path) -> int:
    """Open a lock beneath an already validated parent directory descriptor."""
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        _SECURE_FILE_MODE,
        dir_fd=parent_fd,
    )
    return _validate_lock_fd(fd, path)


def _validate_lock_fd(fd: int, path: Path) -> int:
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"insecure lock file: {path}")
        if st.st_mode & 0o077:
            os.fchmod(fd, _SECURE_FILE_MODE)
        return fd
    except BaseException:
        os.close(fd)
        raise


def close_fd(fd: int) -> None:
    """Close an fd. Isolated for test monkeypatching."""
    import os

    os.close(fd)


def _read_at(root_fd: int, name: str) -> str:
    """Read a state file relative to a pinned helper-directory fd."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    # ``fdopen`` owns and closes ``fd`` when the context exits. Do not close
    # it again: the descriptor number may already have been reused.
    try:
        stream = os.fdopen(fd, "r", encoding="utf-8")
    except OSError:
        # If construction fails, fdopen did not acquire ownership.
        os.close(fd)
        raise
    with stream:
        return stream.read()


def _atomic_write_at(root_fd: int, name: str, data: bytes, mode: int) -> None:
    """Atomically replace a state file without leaving the pinned directory."""
    # mkstemp has no dir_fd parameter; use O_EXCL with a random tempfile name
    # while keeping both creation and rename descriptor-relative.
    temporary = f".{name}.{next(tempfile._get_candidate_names())}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=root_fd)
    try:
        stream = os.fdopen(fd, "wb")
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=root_fd)
        raise
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode, dir_fd=root_fd)
        os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=root_fd)
        raise


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

        content = JsonBackend.render(
            delta.content, indent=JsonBackend._indent_for_tag(delta.tag)
        )
        kind = "json"
    else:  # WRITE_TEXT
        content = delta.content
        kind = "text"
    return _RecoveryEntry(
        tag=tag.value, path=str(path), kind=kind, content=content
    )


def _tag_path(paths: Paths, tag: FileTag) -> Path:
    """Map a semantic tag to its resolved path (mirrors the tools-layer mapping)."""
    from zai_python_helper.tools.base import resolve_path

    return resolve_path(paths, tag)


def _write_manifest(
    state: PinnedStateDirectory,
    entries: list[_RecoveryEntry],
    journal: _RecoveryEntry | None = None,
) -> None:
    """Persist the recovery manifest atomically at 0600 (may carry secrets).

    ``journal`` (if given) is the ownership journal's intended final content,
    stored ALONGSIDE the config entries so a crash replays both or neither
    (issue #60). It is kept in a separate key rather than appended to
    ``entries`` because the journal is not a managed config file: it must not
    be reported to the user as a recovered ``tag``.
    """
    payload: dict[str, Any] = {"entries": [e.to_dict() for e in entries]}
    if journal is not None:
        payload["journal"] = journal.to_dict()
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    state.atomic_write("recovery.json", text.encode("utf-8"), _SECURE_FILE_MODE)


def _read_manifest(
    state: PinnedStateDirectory, paths: Paths
) -> tuple[list[_RecoveryEntry], _RecoveryEntry | None]:
    """Parse a recovery manifest → ``(entries, journal)``.

    Returns ``([], None)`` if the manifest is absent or corrupt. A corrupt
    manifest is logged-as-empty rather than fatal: recovery is a best-effort
    roll-forward, and a manifest we cannot parse cannot guide a replay. (The
    caller has already warned the user that the prior run did not finish
    cleanly.)

    ``journal`` is the ownership journal's intended final content when the
    interrupted run carried one (issue #60), else ``None``.
    """
    try:
        raw = state.read_text("recovery.json")
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return [], None
    if not isinstance(doc, dict):
        return [], None
    raw_entries = doc.get("entries", [])
    allowed = {
        tag.value: str(_tag_path(paths, tag))
        for tag in FileTag
    }
    entries = [
        _RecoveryEntry.from_dict(e)
        for e in (raw_entries if isinstance(raw_entries, list) else [])
        if isinstance(e, dict)
        and str(e.get("path", "")) == allowed.get(str(e.get("tag", "")))
    ]
    raw_journal = doc.get("journal")
    raw_journal_path = (
        str(raw_journal.get("path", "")) if isinstance(raw_journal, dict) else ""
    )
    # The manifest itself is read from the pinned helper directory.  The
    # stored path is legacy metadata and is deliberately not canonicalized:
    # accept old symlink/relative spellings, but always replay ownership.json
    # in the pinned directory (the transition policy is documented in ADR-006).
    journal_path_matches = (
        isinstance(raw_journal, dict)
        and Path(raw_journal_path).name == paths.ownership_json.name
    )
    journal = (
        _RecoveryEntry.from_dict(raw_journal)
        if isinstance(raw_journal, dict)
        and journal_path_matches
        else None
    )
    if journal is not None:
        # The journal is the only legitimate non-config entry. Treat an
        # unknown/crafted tag as untrusted and force the secret, pinned-root
        # replay path rather than falling through to a path-based write.
        journal = replace(
            journal,
            tag="ownership",
            path=str(paths.ownership_json),
        )
    return entries, journal


def _remove_manifest(state: PinnedStateDirectory) -> None:
    """Delete the recovery manifest (commit complete). Best-effort + silent."""
    with contextlib.suppress(FileNotFoundError, OSError):
        state.unlink("recovery.json")


def _apply_entry(
    entry: _RecoveryEntry, *, state: PinnedStateDirectory | None = None
) -> None:
    """Write one recovery entry to disk atomically (idempotent replay)."""
    data = entry.content.encode("utf-8")
    if entry.tag == "ownership":
        # The journal is credential-bearing state, not a user config file.
        # Keep its 0600 protection when replaying the transaction after a
        # crash; config entries use the upstream-parity 0644 writer.
        if state is None:
            raise RuntimeError("ownership replay requires a pinned state directory")
        state.atomic_write("ownership.json", data, _SECURE_FILE_MODE)
        return
    from zai_python_helper.backends import atomic_write_bytes

    atomic_write_bytes(Path(entry.path), data)


# ---------------------------------------------------------------------------
# Public transaction API
# ---------------------------------------------------------------------------


def has_pending_recovery(paths: Paths) -> bool:
    """True iff a recovery manifest from a prior interrupted run exists.

    Callers call :func:`recover` first on every invocation: if this is True,
    the previous ``use`` did not finish cleanly and its manifest must be
    replayed before a new run proceeds.
    """
    state = PinnedStateDirectory.open(paths.recovery_json.parent, create=False)
    if state is None:
        return False
    with state:
        return state.exists("recovery.json")


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
    with state_transaction(paths) as (lock, _moved):
        if lock.state is None:
            raise RuntimeError("ProcessLock acquired without pinned state")
        return recover_locked(paths, lock.state)


def recover_locked(paths: Paths, state: PinnedStateDirectory) -> list[str]:
    """Replay recovery while the caller retains new and legacy lock leases."""
    entries, journal = _read_manifest(state, paths)
    if not entries and journal is None:
        # An absent/empty manifest means nothing to recover. Ensure no stale
        # (e.g. zero-byte) manifest lingers.
        _remove_manifest(state)
        return []
    applied = []
    for entry in entries:
        _apply_entry(entry)
        applied.append(entry.tag)
    # Replay the ownership journal LAST, mirroring commit order: the journal's
    # ``active=False`` retirement only becomes durable once the RESTORE it
    # describes is durable (issue #60). It is not reported as a recovered tag.
    if journal is not None:
        _apply_entry(journal, state=state)
    _remove_manifest(state)
    return applied


def apply_plan_locked(
    paths: Paths,
    plan: PatchPlan,
    *,
    state: PinnedStateDirectory,
    on_locked: Any = None,
    journal_content: Any = None,
) -> list[FileTag]:
    """Commit ``plan`` assuming the caller already holds :class:`ProcessLock`.

    The lock-scoped core of :func:`apply_plan_under_lock`, factored out so the
    CLI can read config, plan, and capture ownership **all inside one held
    lock** (ADR-005 / S3 finding #6: the lock must serialize the state used to
    plan, not only the writes). The caller is responsible for acquiring the
    process lock (and for running :func:`recover` first, also under that lock).

    Contract inside the held lock:
    1. Invoke ``on_locked()`` (if given) — BEFORE the manifest is written, for
       side effects that are NOT part of the transaction.
    2. Resolve ``journal_content()`` (if given) into the manifest, so the
       ownership journal commits ATOMICALLY with the config files.
    3. Write the recovery manifest (so an interrupted run can roll forward).
    4. Write each file via its atomic primitive, then the journal (the commit).
    5. Delete the manifest ONLY after every write succeeds. On a partial
       failure, LEAVE it so the next :func:`recover` rolls forward.

    ``journal_content`` is a zero-arg callable returning the journal's intended
    final text, or ``None`` for "do not touch the journal". Passing the TEXT
    (rather than letting the caller write the file itself, as ``on_locked``
    does) is what makes the journal crash-atomic with the commit: a kill
    anywhere leaves either the pre-transaction state or — via
    :func:`recover` — the complete post-transaction state, never a retired
    journal over an unrestored config (issue #60, Bug 7). An empty-string
    result is written verbatim; an all-NOOP plan still commits the journal.

    Returns the tags actually written, in plan order (the journal is not a
    managed config file and never appears in the result).
    """
    if state.path != paths.lock_file.parent:
        raise ValueError(
            f"transaction state {state.path} does not match {paths.lock_file.parent}"
        )
    entries: list[_RecoveryEntry] = []
    written: list[FileTag] = []
    for delta in plan.deltas:
        entry = _delta_to_entry(delta, paths)
        if entry is not None:
            entries.append(entry)
            written.append(delta.tag)

    if on_locked is not None:
        on_locked()

    journal_entry: _RecoveryEntry | None = None
    if journal_content is not None:
        text = journal_content()
        if text is not None:
            journal_entry = _RecoveryEntry(
                tag="ownership",
                path=str(paths.ownership_json),
                kind="text",
                content=text,
            )

    if not entries and journal_entry is None:
        # No file writes, but a side-effect may have run under the lock (e.g.
        # an idempotent activation with nothing to journal).
        return written
    # Persist the manifest BEFORE any managed-file write so a crash at any
    # later point is recoverable. The manifest holds final content, so
    # recovery is a pure replay (no re-read of live state).
    _write_manifest(state, entries, journal_entry)
    # Commit every file. On FULL success, drop the manifest (commit complete).
    # On a PARTIAL failure, LEAVE the manifest so the next invocation rolls
    # forward — deleting it here would strand mixed state with no recovery
    # path (S3 regression fix, Codex finding #4).
    for entry in entries:
        # These are user configuration paths, not state-root paths. Keep the
        # original call shape as an intentional test/extension seam.
        _apply_entry(entry)
    # Journal LAST: its ``active=False`` retirement must not become durable
    # before the RESTORE it describes (issue #60). If we die here, the manifest
    # survives and recovery finishes both halves.
    if journal_entry is not None:
        _apply_entry(journal_entry, state=state)
    _remove_manifest(state)
    return written


def apply_plan_under_lock(
    paths: Paths,
    plan: PatchPlan,
    *,
    on_locked: Any = None,
    journal_content: Any = None,
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
    ``journal_content`` is forwarded unchanged (see :func:`apply_plan_locked`).
    """
    with ProcessLock(paths) as lock:
        if lock.state is None:
            raise RuntimeError("ProcessLock acquired without pinned state")
        return apply_plan_locked(
            paths,
            plan,
            state=lock.state,
            on_locked=on_locked,
            journal_content=journal_content,
        )
