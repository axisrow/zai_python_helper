"""Tests for multi-file PatchPlan execution (ADR-005): lock + recovery.

Covers the two hard guarantees of S3's transaction layer:

- **Concurrency**: two activations serialized on the process lock (flock) —
  they cannot interleave their multi-file commits.
- **Recovery**: an interrupted activation (manifest survives, files partially
  written) is rolled forward to completion by :func:`recover` on the next run.

Plus the staged-commit invariants: a clean commit leaves no manifest behind,
and a NOOP plan writes nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.patchplan import (
    ProcessLock,
    _read_at,
    apply_plan_locked,
    apply_plan_under_lock,
    has_pending_recovery,
    migrate_legacy_state,
    recover,
    state_transaction,
)
from zai_python_helper.paths import Paths

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan(*deltas: FileDelta) -> PatchPlan:
    return PatchPlan(deltas=tuple(deltas))


def _write_json_delta(tag: FileTag, content: dict) -> FileDelta:
    return FileDelta(tag, DeltaKind.WRITE_JSON, content)


def _paths(home: Path) -> Paths:
    return Paths.from_home(home, state_home=home)


@settings(
    max_examples=25,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(payload=st.binary(max_size=128))
def test_read_at_does_not_double_close_reused_fd(tmp_path, payload):
    """A read failure cannot close an unrelated fd that reuses its number."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    value = state / "value"
    value.write_bytes(payload)
    replacement_path = state / "replacement"
    replacement_path.write_bytes(b"replacement")
    real_open = os.open
    real_close = os.close
    root_fd = real_open(state, os.O_RDONLY | os.O_DIRECTORY)
    replacements: list[int] = []

    class FailingStream:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            return self

        def read(self):
            raise OSError("read failed")

        def __exit__(self, *_args):
            real_close(self.fd)
            replacement = real_open(replacement_path, os.O_RDONLY)
            assert replacement == self.fd  # deterministic lowest-fd reuse
            replacements.append(replacement)

    try:
        with mock.patch(
            "zai_python_helper.patchplan.os.fdopen",
            side_effect=lambda fd, *_args, **_kwargs: FailingStream(fd),
        ):
            with pytest.raises(OSError, match="read failed"):
                _read_at(root_fd, "value")
        assert len(replacements) == 1
        os.fstat(replacements[0])  # still alive: no second close after fdopen
    finally:
        for fd in replacements:
            real_close(fd)
        real_close(root_fd)


def test_migrate_legacy_state_moves_journal_and_recovery(tmp_path):
    """An upgrade preserves both ownership and interrupted-run state."""
    paths = _paths(tmp_path)
    legacy = tmp_path / ".zai-python-helper"
    legacy.mkdir()
    (legacy / "ownership.json").write_text('{"legacy": true}\n')
    (legacy / "recovery.json").write_text('{"entries": []}\n')

    assert migrate_legacy_state(paths) == ["ownership.json", "recovery.json"]
    assert paths.ownership_json.read_text() == '{"legacy": true}\n'
    assert paths.recovery_json.read_text() == '{"entries": []}\n'
    assert not (legacy / "ownership.json").exists()
    assert not (legacy / "recovery.json").exists()


def test_migrate_legacy_state_rewrites_recovery_journal_path(tmp_path):
    """Migrated recovery manifests must point at the new journal location."""
    paths = _paths(tmp_path)
    legacy = tmp_path / ".zai-python-helper"
    legacy.mkdir()
    (legacy / "ownership.json").write_text('{"legacy": true}\n')
    (legacy / "recovery.json").write_text(json.dumps({
        "entries": [],
        "journal": {"tag": "ownership", "path": str(legacy / "ownership.json"), "content": "{}\n"},
    }))

    migrate_legacy_state(paths)
    manifest = json.loads(paths.recovery_json.read_text())
    assert manifest["journal"]["path"] == str(paths.ownership_json)


@pytest.mark.parametrize("name", ["ownership.json", "recovery.json"])
def test_migrate_legacy_state_prefers_newer_runtime_tree(tmp_path, name):
    """A stale pre-0.1 HOME copy cannot override newer runtime state."""
    home = tmp_path / "home"
    home.mkdir()
    home_legacy = home / ".zai-python-helper"
    home_legacy.mkdir(mode=0o700)
    runtime_legacy = tmp_path / "legacy-runtime"
    runtime_legacy.mkdir(mode=0o700)
    (home_legacy / name).write_text('{"source": "home"}\n')
    (runtime_legacy / name).write_text('{"source": "runtime"}\n')
    paths = replace(
        Paths.from_home(home, state_home=tmp_path / "new-state"),
        legacy_runtime_dir=runtime_legacy,
    )

    assert migrate_legacy_state(paths) == [name]
    destination = paths.lock_file.parent / name
    assert json.loads(destination.read_text()) == {"source": "runtime"}
    assert not (runtime_legacy / name).exists()
    assert (home_legacy / name).exists()


def test_migrate_legacy_state_waits_for_legacy_process_lock(tmp_path):
    """Migration cannot copy/unlink state while an old process is committing."""
    legacy = tmp_path / "legacy-state"
    legacy.mkdir(mode=0o700)
    (legacy / "ownership.json").write_text('{"version": "stale"}\n')
    ready = legacy / "ready"
    paths = replace(
        Paths.from_home(tmp_path / "home", state_home=tmp_path / "new-state"),
        legacy_runtime_dir=legacy,
    )
    script = """
import fcntl, os, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
(root / "ready").write_text("held")
time.sleep(0.35)
(root / "ownership.json").write_text('{"version": "final"}\\n')
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(legacy)])
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()

    started = time.monotonic()
    try:
        assert migrate_legacy_state(paths) == ["ownership.json"]
    finally:
        process.wait(timeout=2)

    assert time.monotonic() - started >= 0.25
    assert paths.ownership_json.read_text() == '{"version": "final"}\n'
    assert not (legacy / "ownership.json").exists()


def test_state_transaction_retains_legacy_lock_through_commit_scope(tmp_path):
    """An old process reaching its lock late stays blocked until commit exits."""
    legacy = tmp_path / "legacy-state"
    started = legacy / "started"
    acquired = legacy / "acquired"
    paths = replace(
        Paths.from_home(tmp_path / "home", state_home=tmp_path / "new-state"),
        legacy_runtime_dir=legacy,
    )
    script = """
import fcntl, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
(root / "started").write_text("waiting")
fcntl.flock(fd, fcntl.LOCK_EX)
(root / "acquired").write_text("entered")
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""

    with state_transaction(paths):
        process = subprocess.Popen([sys.executable, "-c", script, str(legacy)])
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        time.sleep(0.1)
        assert not acquired.exists()

    process.wait(timeout=2)
    assert acquired.read_text() == "entered"


def test_state_transaction_reserves_missing_home_legacy_lock_tree(tmp_path):
    """A pre-0.1 process cannot create and lock HOME state during commit."""
    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".zai-python-helper"
    started = legacy / "started"
    acquired = legacy / "acquired"
    paths = Paths.from_home(home, state_home=tmp_path / "new-state")
    script = """
import fcntl, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
root.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
(root / "started").write_text("waiting")
fcntl.flock(fd, fcntl.LOCK_EX)
(root / "acquired").write_text("entered")
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""

    assert not legacy.exists()
    with state_transaction(paths):
        process = subprocess.Popen([sys.executable, "-c", script, str(legacy)])
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        time.sleep(0.1)
        assert not acquired.exists()

    process.wait(timeout=2)
    assert acquired.read_text() == "entered"


# ---------------------------------------------------------------------------
# ProcessLock: serialization
# ---------------------------------------------------------------------------


class TestProcessLock:
    def test_precreated_lock_symlink_is_rejected(self, tmp_path):
        """The lock itself must also be opened without following symlinks."""
        lock_path = tmp_path / "lock"
        target = tmp_path / "target"
        target.write_text("")
        lock_path.symlink_to(target)

        with pytest.raises(OSError):
            with ProcessLock(lock_path):
                pass

    def test_unrelated_xdg_ancestor_is_not_rehardened(self, tmp_path):
        """State setup must not chmod a user-owned XDG ancestor by its name."""
        state_home = tmp_path / "zai-python-helper-user"
        state_home.mkdir(mode=0o755)
        paths = Paths.from_home(tmp_path, state_home=state_home)

        with ProcessLock(paths.lock_file):
            pass
        assert state_home.stat().st_mode & 0o777 == 0o755

    def test_symlinked_xdg_state_root_is_supported(self, tmp_path):
        """A user-configured state root may safely point to another volume."""
        target = tmp_path / "actual-state"
        target.mkdir()
        state_home = tmp_path / "state-link"
        state_home.symlink_to(target, target_is_directory=True)
        paths = Paths.from_home(tmp_path, state_home=state_home)

        with ProcessLock(paths.lock_file):
            pass
        assert target in paths.lock_file.parents
        assert (target / "zai-python-helper").is_dir()

    @settings(
        max_examples=12,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(attacker_kind=st.sampled_from(["foreign-owner", "symlink", "file"]))
    def test_attacker_owned_legacy_runtime_root_does_not_deny_service(
        self, tmp_path, attacker_kind
    ):
        """A foreign /var/tmp reservation is ignored, not raised to the victim."""
        import zai_python_helper.patchplan as patchplan
        attacker_root = tmp_path / f"attacker-{attacker_kind}"
        attacker_target = tmp_path / f"target-{attacker_kind}"
        if attacker_root.is_symlink():
            attacker_root.unlink()
        if attacker_kind == "foreign-owner":
            attacker_root.mkdir(exist_ok=True)
            marker = attacker_root / "attacker-marker"
            marker.write_text("foreign uid")
        elif attacker_kind == "symlink":
            attacker_target.mkdir(exist_ok=True)
            marker = attacker_target / "attacker-marker"
            marker.write_text("symlink target")
            attacker_root.symlink_to(attacker_target, target_is_directory=True)
        else:
            attacker_root.write_text("attacker file")
            marker = attacker_root
        preferred = tmp_path / ".local" / "state"
        paths = replace(
            Paths.from_home(tmp_path, state_home=preferred),
            legacy_runtime_dir=attacker_root,
        )

        with ExitStack() as patches:
            if attacker_kind == "foreign-owner":
                real_fstat = patchplan.os.fstat
                attacker_fd: list[int] = []
                real_open = patchplan.os.open

                def track_open(path, flags, *args, dir_fd=None, **kwargs):
                    fd = real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
                    if Path(path).name == attacker_root.name:
                        attacker_fd.append(fd)
                    return fd

                def foreign_fstat(fd):
                    result = real_fstat(fd)
                    if fd in attacker_fd:
                        return SimpleNamespace(
                            st_uid=result.st_uid + 1,
                            st_mode=result.st_mode,
                        )
                    return result

                patches.enter_context(mock.patch.object(patchplan.os, "open", track_open))
                patches.enter_context(
                    mock.patch.object(patchplan.os, "fstat", foreign_fstat)
                )
            assert migrate_legacy_state(paths) == []
        with ProcessLock(paths) as lock:
            assert lock.state is not None
            lock.state.atomic_write("ownership.json", b"{}\n", 0o600)
        assert paths.ownership_json.read_text() == "{}\n"
        assert marker.exists()

    @settings(
        max_examples=12,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(same_root=st.booleans())
    def test_nested_lock_fails_loudly_without_corrupting_outer_pinned_root(
        self, tmp_path, same_root
    ):
        """Rejected nesting cannot redirect outer state after a directory swap."""
        case = Path(tempfile.mkdtemp(dir=tmp_path))
        outer = Paths.from_home(case / "home-a", state_home=case / "state-a")
        inner = (
            outer
            if same_root
            else Paths.from_home(case / "home-b", state_home=case / "state-b")
        )
        plan = _plan(FileDelta(FileTag.SETTINGS, DeltaKind.NOOP, {}))
        moved = outer.lock_file.parent.with_name(
            f"{outer.lock_file.parent.name}-pinned-original"
        )
        nested_errors: list[str] = []

        def try_nested_then_swap():
            try:
                with ProcessLock(inner):
                    pass
            except RuntimeError as exc:
                nested_errors.append(str(exc))
            outer.lock_file.parent.rename(moved)
            outer.lock_file.parent.mkdir(mode=0o700)

        apply_plan_under_lock(
            outer,
            plan,
            on_locked=try_nested_then_swap,
            journal_content=lambda: '{"pinned": true}\n',
        )

        assert nested_errors == ["nested ProcessLock acquisition is forbidden"]
        assert (moved / "ownership.json").read_text() == '{"pinned": true}\n'
        assert not outer.ownership_json.exists()

    def test_lock_rejects_lexical_parent_traversal(self, tmp_path):
        """Validation and later bookkeeping must use identical path semantics."""
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(ValueError, match="must not contain"):
            with ProcessLock(link / ".." / "lock"):
                pass

    def test_lock_validation_closes_fd_when_chmod_fails(self, tmp_path, monkeypatch):
        """A failed lock hardening operation must not leak its descriptor."""
        from zai_python_helper import patchplan

        lock_path = tmp_path / "lock"
        lock_path.write_text("")
        lock_path.chmod(0o644)
        closed: list[int] = []
        real_close = os.close

        def fail_chmod(fd, mode):
            raise OSError("filesystem refuses chmod")

        def record_close(fd):
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(patchplan.os, "fchmod", fail_chmod)
        monkeypatch.setattr(patchplan.os, "close", record_close)
        root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(OSError, match="refuses chmod"):
                patchplan.os_open_at(root_fd, lock_path.name, lock_path)
        finally:
            os.close(root_fd)
        assert closed

    def test_lock_is_exclusive_across_threads(self, tmp_path):
        """Two threads acquiring the same lock serialize (flock LOCK_EX).

        Thread A holds the lock for a short window; thread B must WAIT until A
        releases before its critical section runs. We assert the ORDER: A's
        critical section fully precedes B's.
        """
        lock_path = tmp_path / "lock"
        order: list[str] = []
        a_holds = threading.Event()

        def worker(name: str, hold: float, *, signal_held: bool = False) -> None:
            with ProcessLock(lock_path):
                if signal_held:
                    a_holds.set()
                order.append(f"{name}-enter")
                time.sleep(hold)
                order.append(f"{name}-exit")

        def a() -> None:
            worker("A", 0.05, signal_held=True)

        def b() -> None:
            # Block until A is DEFINITELY holding the lock, then try to
            # acquire (must wait for A to release).
            assert a_holds.wait(timeout=2.0)
            worker("B", 0.0)

        ta = threading.Thread(target=a)
        tb = threading.Thread(target=b)
        ta.start()
        tb.start()
        ta.join(timeout=2.0)
        tb.join(timeout=2.0)

        # Serialization: each thread's enter/exit are contiguous — no
        # interleaving like A-enter, B-enter, A-exit.
        assert order[0] == "A-enter"
        assert order[1] == "A-exit"
        assert order[2] == "B-enter"
        assert order[3] == "B-exit"

    def test_concurrent_apply_plans_serialize(self, tmp_path):
        """Two concurrent apply_plan_under_lock calls do not interleave writes.

        Each call takes the process lock for its whole commit window, so the
        per-file writes of two plans cannot mix mid-flight: we observe them as
        two non-overlapping contiguous runs. A real interleave would show
        alternating settings/claude_json tags.
        """
        paths = _paths(tmp_path)
        plan_a = _plan(_write_json_delta(FileTag.SETTINGS, {"mark": "A"}))
        plan_b = _plan(_write_json_delta(FileTag.CLAUDE_JSON, {"mark": "B"}))

        trace: list[str] = []
        lock = threading.Lock()

        def run(plan: PatchPlan, name: str) -> None:
            # Wrap apply with explicit lock-enter/exit markers around the call
            # by recording via the on_locked hook (fires inside the held lock).
            def on_locked() -> None:
                with lock:
                    trace.append(f"{name}-in")
                    time.sleep(0.03)
                    trace.append(f"{name}-out")

            apply_plan_under_lock(paths, plan, on_locked=on_locked)

        ta = threading.Thread(target=run, args=(plan_a, "A"))
        tb = threading.Thread(target=run, args=(plan_b, "B"))
        ta.start()
        tb.start()
        ta.join(timeout=2.0)
        tb.join(timeout=2.0)

        # Each call's in/out are contiguous — no A-in, B-in, A-out, B-out.
        for i in range(0, len(trace), 2):
            assert trace[i].split("-")[0] == trace[i + 1].split("-")[0], trace

    def test_lock_release_is_idempotent(self, tmp_path):
        """Releasing a lock that isn't held is a safe no-op."""
        lock = ProcessLock(tmp_path / "lock")
        lock.release()  # no error
        lock.acquire()
        lock.release()
        lock.release()  # double release — no error

    def test_lock_context_manager_acquires_and_releases(self, tmp_path):
        """The context manager acquires on enter, releases on exit."""
        lock_path = tmp_path / "lock"
        with ProcessLock(lock_path):
            assert lock_path.exists()
        # A second acquisition immediately after release succeeds (no deadlock).
        with ProcessLock(lock_path):
            pass

    def test_acquire_closes_fd_if_flock_fails(self, tmp_path, monkeypatch):
        """If flock fails after os_open, the opened fd is closed (no leak).

        Regression for the fd-leak on the flock-failure path: acquire() opens
        the lock file then takes flock; if flock raises, the fd it opened must
        be closed (release() is unreachable because acquire() is raising).
        """
        import zai_python_helper.patchplan as pp

        opened: list[int] = []

        real_open = pp.os_open_at
        real_flock = fcntl.flock

        def tracking_open(parent_fd, name, path):
            fd = real_open(parent_fd, name, path)
            opened.append(fd)
            return fd

        def failing_flock(fd, op):
            if op == fcntl.LOCK_EX:
                raise OSError("simulated flock failure")
            real_flock(fd, op)

        monkeypatch.setattr(pp, "os_open_at", tracking_open)
        monkeypatch.setattr(pp.fcntl, "flock", failing_flock)

        lock = ProcessLock(tmp_path / "lock")
        with pytest.raises(OSError):
            lock.acquire()

        # The fd we opened is now closed (not leaked).
        import os

        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)
        # And the lock is not left half-held.
        assert lock._held_intra is False


# ---------------------------------------------------------------------------
# recover(): roll-forward
# ---------------------------------------------------------------------------


class TestRecover:
    def test_recover_accepts_legacy_lexical_journal_path(self, tmp_path):
        """Canonical state roots must preserve pending pre-upgrade journals."""
        target = tmp_path / "state-target"
        target.mkdir()
        state_link = tmp_path / "state-link"
        state_link.symlink_to(target, target_is_directory=True)
        paths = Paths.from_home(tmp_path, state_home=state_link)
        paths.recovery_json.parent.mkdir(parents=True)
        old_journal = state_link / paths.ownership_json.relative_to(target)
        paths.recovery_json.write_text(
            json.dumps(
                {
                    "entries": [],
                    "journal": {
                        "tag": "ownership",
                        "path": str(old_journal),
                        "content": '{"restored": true}\n',
                    },
                }
            )
        )

        assert recover(paths) == []
        assert json.loads(paths.ownership_json.read_text()) == {"restored": True}

    def test_no_manifest_is_noop(self, tmp_path):
        """recover() with no manifest writes nothing and returns []."""
        paths = _paths(tmp_path)
        assert has_pending_recovery(paths) is False
        assert recover(paths) == []
        # Still no manifest, no files created.
        assert not paths.recovery_json.exists()

    def test_recover_replays_manifest_and_clears_it(self, tmp_path):
        """A surviving manifest is replayed: files written, manifest deleted.

        This simulates the kill-mid-run scenario: a manifest was written but
        the process died before/during the file writes. On the next run,
        recover() replays every entry (final content) and removes the manifest.
        """
        paths = _paths(tmp_path)
        # Hand-craft a manifest as if a prior run wrote it then died.
        manifest = {
            "entries": [
                {
                    "tag": "settings",
                    "path": str(paths.claude_settings),
                    "kind": "json",
                    "content": json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-zai"}}) + "\n",
                },
                {
                    "tag": "zshrc",
                    "path": str(paths.zshrc),
                    "kind": "text",
                    "content": "# recovered shell block\n",
                },
            ]
        }
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        paths.recovery_json.parent.parent.chmod(0o700)
        paths.recovery_json.parent.chmod(0o700)
        paths.recovery_json.write_text(json.dumps(manifest))

        assert has_pending_recovery(paths) is True
        applied = recover(paths)

        assert applied == ["settings", "zshrc"]
        # Files now hold the manifest's final content (roll-forward complete).
        assert json.loads(paths.claude_settings.read_text())["env"][
            "ANTHROPIC_AUTH_TOKEN"
        ] == "sk-zai"
        assert paths.zshrc.read_text() == "# recovered shell block\n"
        # Manifest consumed.
        assert not paths.recovery_json.exists()
        assert has_pending_recovery(paths) is False

    def test_recover_completes_partial_three_file_activation(self, tmp_path):
        """Kill BETWEEN file 2 and 3 → recover() finishes all three.

        The acceptance scenario: settings + claude_json were written, but the
        process died before .zshrc. The manifest records all three final
        states; recover() rewrites all three (idempotent for the two already
        done, completes the third).
        """
        paths = _paths(tmp_path)
        paths.claude_settings.parent.mkdir(parents=True, exist_ok=True)
        # Pretend file 1 and 2 were already written before the crash, file 3 not.
        paths.claude_settings.write_text(json.dumps({"env": {"STALE": "x"}}))
        paths.claude_json.write_text(json.dumps({"hasCompletedOnboarding": False}))
        # .zshrc NOT written (the crash point).

        manifest = {
            "entries": [
                {
                    "tag": "settings",
                    "path": str(paths.claude_settings),
                    "kind": "json",
                    "content": json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-final"}}) + "\n",
                },
                {
                    "tag": "claude_json",
                    "path": str(paths.claude_json),
                    "kind": "json",
                    "content": json.dumps({"hasCompletedOnboarding": True}) + "\n",
                },
                {
                    "tag": "zshrc",
                    "path": str(paths.zshrc),
                    "kind": "text",
                    "content": "# final block\n",
                },
            ]
        }
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        paths.recovery_json.write_text(json.dumps(manifest))

        applied = recover(paths)
        assert applied == ["settings", "claude_json", "zshrc"]

        # All three reflect the manifest's FINAL state (the stale settings was
        # overwritten; the half-done zshrc is completed).
        assert json.loads(paths.claude_settings.read_text())["env"][
            "ANTHROPIC_AUTH_TOKEN"
        ] == "sk-final"
        assert json.loads(paths.claude_json.read_text())["hasCompletedOnboarding"] is True
        assert paths.zshrc.read_text() == "# final block\n"
        assert not paths.recovery_json.exists()

    def test_recover_corrupt_manifest_is_best_effort_noop(self, tmp_path):
        """A manifest we can't parse is treated as empty (recover never fatal)."""
        paths = _paths(tmp_path)
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        paths.recovery_json.write_text("{not json")
        assert recover(paths) == []
        # The corrupt manifest is removed so we don't loop forever.
        assert not paths.recovery_json.exists()


# ---------------------------------------------------------------------------
# apply_plan_under_lock: staged commit invariants
# ---------------------------------------------------------------------------


class TestApplyPlanUnderLock:
    def test_rejects_pinned_state_from_a_different_transaction(self, tmp_path):
        """An explicit capability cannot be rebound to another Paths bundle."""
        paths = _paths(tmp_path / "outer")
        other = _paths(tmp_path / "other")
        plan = _plan(FileDelta(FileTag.SETTINGS, DeltaKind.NOOP, {}))

        with ProcessLock(other) as lock:
            with pytest.raises(ValueError, match="does not match"):
                apply_plan_locked(paths, plan, state=lock.state)

    def test_writes_all_files_and_leaves_no_manifest(self, tmp_path):
        """A clean commit writes every delta and deletes the recovery manifest."""
        paths = _paths(tmp_path)
        plan = _plan(
            _write_json_delta(FileTag.SETTINGS, {"env": {"ANTHROPIC_AUTH_TOKEN": "sk"}}),
            _write_json_delta(FileTag.CLAUDE_JSON, {"hasCompletedOnboarding": True}),
        )

        written = apply_plan_under_lock(paths, plan)

        assert written == [FileTag.SETTINGS, FileTag.CLAUDE_JSON]
        assert json.loads(paths.claude_settings.read_text())["env"][
            "ANTHROPIC_AUTH_TOKEN"
        ] == "sk"
        assert json.loads(paths.claude_json.read_text())["hasCompletedOnboarding"] is True
        # No manifest lingers after a clean commit.
        assert not paths.recovery_json.exists()
        assert has_pending_recovery(paths) is False

    def test_noop_plan_writes_nothing_and_acquires_no_manifest(self, tmp_path):
        """An all-NOOP plan writes nothing and creates no manifest/lock side effect."""
        paths = _paths(tmp_path)
        plan = PatchPlan(
            deltas=(
                FileDelta(FileTag.SETTINGS, DeltaKind.NOOP, {}),
                FileDelta(FileTag.ZSHRC, DeltaKind.NOOP, ""),
            )
        )
        written = apply_plan_under_lock(paths, plan)
        assert written == []
        assert not paths.recovery_json.exists()
        # No managed file was created.
        assert not paths.claude_settings.exists()

    def test_on_locked_runs_under_lock_before_write(self, tmp_path):
        """on_locked fires while the lock is held and before any file write.

        The CLI uses this to persist the ownership journal so journal + commit
        are consistent under one lock. We assert: (a) on_locked runs, (b) it
        runs BEFORE the files exist on disk, (c) the lock file exists when it
        runs (i.e. we are inside the locked window).
        """
        paths = _paths(tmp_path)
        plan = _plan(_write_json_delta(FileTag.SETTINGS, {"env": {"X": "1"}}))

        observed: dict = {}

        def on_locked() -> None:
            observed["lock_held"] = paths.lock_file.exists()
            observed["settings_written_yet"] = paths.claude_settings.exists()
            observed["called"] = True

        apply_plan_under_lock(paths, plan, on_locked=on_locked)

        assert observed["called"] is True
        assert observed["lock_held"] is True
        # The journal hook runs BEFORE the managed files are written.
        assert observed["settings_written_yet"] is False
        # And the file IS written afterwards.
        assert paths.claude_settings.exists()

    def test_on_locked_aborts_transaction_on_error(self, tmp_path):
        """If on_locked raises, no manifest is written and no file is touched.

        The transaction must abort cleanly: the ownership-journal side-effect
        failing must not leave a half-committed activation behind.
        """
        paths = _paths(tmp_path)
        plan = _plan(_write_json_delta(FileTag.SETTINGS, {"env": {"X": "1"}}))

        def boom() -> None:
            raise RuntimeError("journal write failed")

        with pytest.raises(RuntimeError):
            apply_plan_under_lock(paths, plan, on_locked=boom)

        assert not paths.claude_settings.exists()
        assert not paths.recovery_json.exists()

    def test_journal_content_commits_with_the_files(self, tmp_path):
        """``journal_content`` lands on disk together with the plan's files.

        The journal is passed as TEXT (not written by the caller) so the commit
        layer can fold it into the recovery manifest — issue #60.
        """
        paths = _paths(tmp_path)
        plan = _plan(_write_json_delta(FileTag.SETTINGS, {"env": {"X": "1"}}))

        apply_plan_under_lock(
            paths, plan, journal_content=lambda: '{"tool": {}}\n'
        )

        assert paths.ownership_json.read_text() == '{"tool": {}}\n'
        assert paths.claude_settings.exists()
        assert not paths.recovery_json.exists()

    def test_journal_is_written_after_the_config_files(self, tmp_path):
        """The journal commits LAST — the retirement never precedes the RESTORE.

        This is the ordering half of the issue #60 fix: if the journal landed
        first, a crash between it and the config write would leave
        ``active=False`` over an unreverted config (permanent REFUSE).
        """
        paths = _paths(tmp_path)
        plan = _plan(_write_json_delta(FileTag.SETTINGS, {"env": {"X": "1"}}))

        order: list[str] = []
        import zai_python_helper.patchplan as patchplan

        real = patchplan._apply_entry

        def tracing(entry, **kwargs):
            order.append(entry.tag)
            real(entry, **kwargs)

        patchplan._apply_entry = tracing
        try:
            apply_plan_under_lock(paths, plan, journal_content=lambda: "{}\n")
        finally:
            patchplan._apply_entry = real

        assert order == ["settings", "ownership"]

    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        tag=st.text(max_size=32),
        basename=st.sampled_from(
            ["ownership.json", "settings.json", "recovery.json", "", "../ownership.json"]
        ),
    )
    def test_recover_untrusted_journal_metadata_cannot_bypass_pinned_secret_path(
        self, tmp_path, tag, basename
    ):
        """Fuzzed journal metadata cannot select a path or downgrade mode."""
        paths = _paths(tmp_path)
        attacker_path = tmp_path / "attacker" / basename
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        paths.ownership_json.unlink(missing_ok=True)
        paths.recovery_json.write_text(
            json.dumps(
                {
                    "entries": [],
                    "journal": {
                        "tag": tag,
                        "path": str(attacker_path),
                        "kind": "text",
                        "content": '{"owned": true}\n',
                    },
                }
            )
        )

        assert recover(paths) == []
        if attacker_path.name == "ownership.json":
            assert paths.ownership_json.read_text() == '{"owned": true}\n'
            assert paths.ownership_json.stat().st_mode & 0o777 == 0o600
        else:
            assert not paths.ownership_json.exists()
        assert not attacker_path.exists()

    def test_crash_mid_commit_leaves_journal_in_manifest_not_on_disk(self, tmp_path):
        """A kill during the file writes must NOT have made the journal durable.

        The manifest survives carrying the journal, so the next recover() rolls
        both halves forward atomically (issue #60).
        """
        paths = _paths(tmp_path)
        plan = _plan(_write_json_delta(FileTag.SETTINGS, {"env": {"X": "1"}}))
        import zai_python_helper.patchplan as patchplan

        real = patchplan._apply_entry

        def crashing(_entry):
            raise RuntimeError("killed")

        patchplan._apply_entry = crashing
        try:
            with pytest.raises(RuntimeError):
                apply_plan_under_lock(
                    paths, plan, journal_content=lambda: '{"retired": true}\n'
                )
        finally:
            patchplan._apply_entry = real

        # Nothing durable yet — neither the config file nor the journal.
        assert not paths.claude_settings.exists()
        assert not paths.ownership_json.exists()
        # But the manifest carries BOTH, so recovery completes the transaction.
        assert has_pending_recovery(paths) is True
        assert recover(paths) == ["settings"]
        assert paths.ownership_json.read_text() == '{"retired": true}\n'
        assert json.loads(paths.claude_settings.read_text())["env"]["X"] == "1"

    def test_journal_only_transaction_commits_without_file_deltas(self, tmp_path):
        """An all-NOOP plan with a journal still commits the journal.

        A REFUSE-only ``use default`` writes no config file but may still need
        to persist the (byte-identical) journal; the transaction must not
        short-circuit past it.
        """
        paths = _paths(tmp_path)
        plan = _plan(FileDelta(FileTag.SETTINGS, DeltaKind.NOOP, {}))

        written = apply_plan_under_lock(paths, plan, journal_content=lambda: "{}\n")

        assert written == []
        assert paths.ownership_json.read_text() == "{}\n"
        assert not paths.recovery_json.exists()

    def test_journal_content_returning_none_touches_nothing(self, tmp_path):
        """``journal_content`` may decline (``None``) — no journal file is created."""
        paths = _paths(tmp_path)
        plan = _plan(FileDelta(FileTag.SETTINGS, DeltaKind.NOOP, {}))

        assert apply_plan_under_lock(paths, plan, journal_content=lambda: None) == []
        assert not paths.ownership_json.exists()
        assert not paths.recovery_json.exists()

    def test_recover_replays_manifest_journal_last(self, tmp_path):
        """recover() rolls a manifest journal forward, and not as a reported tag."""
        paths = _paths(tmp_path)
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        paths.recovery_json.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "tag": "settings",
                            "path": str(paths.claude_settings),
                            "kind": "json",
                            "content": json.dumps({"env": {"A": "1"}}) + "\n",
                        }
                    ],
                    "journal": {
                        "tag": "ownership",
                        "path": str(paths.ownership_json),
                        "kind": "text",
                        "content": '{"recovered": true}\n',
                    },
                }
            )
        )

        applied = recover(paths)

        # The journal is bookkeeping, not a managed config file → not reported.
        assert applied == ["settings"]
        assert paths.ownership_json.read_text() == '{"recovered": true}\n'
        assert not paths.recovery_json.exists()

    def test_on_locked_runs_even_for_noop_plan(self, tmp_path):
        """An idempotent (NOOP) activation still refreshes the journal under lock.

        use zai is idempotent: a second run produces an all-NOOP plan, but it
        must STILL record ownership (the journal is the source of truth for a
        later revert). on_locked fires; no file write occurs.
        """
        paths = _paths(tmp_path)
        plan = PatchPlan(
            deltas=(FileDelta(FileTag.SETTINGS, DeltaKind.NOOP, {}),)
        )
        called = {"v": False}

        def on_locked() -> None:
            called["v"] = True

        written = apply_plan_under_lock(paths, plan, on_locked=on_locked)
        assert written == []  # no file writes
        assert called["v"] is True  # but the side-effect ran under the lock

    def test_partial_write_failure_retains_manifest_for_rollforward(
        self, tmp_path, monkeypatch
    ):
        """A write that fails mid-commit LEAVES the manifest (S3 regression, #4).

        If the 2nd file write raises after the 1st committed, the manifest must
        survive so the next invocation's recover() rolls forward. Deleting it
        in a finally would strand mixed state with no recovery path — defeating
        ADR-005 for the only case that produces recoverable mixed state.
        """
        import zai_python_helper.patchplan as pp

        paths = _paths(tmp_path)
        plan = _plan(
            _write_json_delta(FileTag.SETTINGS, {"env": {"A": "1"}}),
            _write_json_delta(FileTag.CLAUDE_JSON, {"B": "2"}),
        )

        real_apply = pp._apply_entry
        call = {"n": 0}

        def failing_apply(entry):
            call["n"] += 1
            if call["n"] == 2:
                raise OSError("simulated mid-commit write failure")
            real_apply(entry)

        monkeypatch.setattr(pp, "_apply_entry", failing_apply)

        with pytest.raises(OSError):
            apply_plan_under_lock(paths, plan)

        # File 1 committed; file 2 did not — mixed state on disk.
        assert json.loads(paths.claude_settings.read_text())["env"]["A"] == "1"
        assert not paths.claude_json.exists()
        # The manifest SURVIVES so the next run can roll forward.
        assert paths.recovery_json.exists()

        # And recover() (now lock-owning) completes the roll-forward: both
        # files end at the manifest's final state, manifest consumed.
        monkeypatch.undo()
        applied = recover(paths)
        assert applied == ["settings", "claude_json"]
        assert json.loads(paths.claude_json.read_text())["B"] == "2"
        assert not paths.recovery_json.exists()
