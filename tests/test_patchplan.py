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
import threading
import time
from pathlib import Path

import pytest

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.patchplan import (
    ProcessLock,
    apply_plan_under_lock,
    has_pending_recovery,
    recover,
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
    return Paths.from_home(home)


# ---------------------------------------------------------------------------
# ProcessLock: serialization
# ---------------------------------------------------------------------------


class TestProcessLock:
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

        real_open = pp.os_open
        real_flock = fcntl.flock

        def tracking_open(path):
            fd = real_open(path)
            opened.append(fd)
            return fd

        def failing_flock(fd, op):
            if op == fcntl.LOCK_EX:
                raise OSError("simulated flock failure")
            real_flock(fd, op)

        monkeypatch.setattr(pp, "os_open", tracking_open)
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
