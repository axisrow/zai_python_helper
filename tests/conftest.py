"""Project-wide pytest configuration and shared fixtures.

The autouse ``_isolate_home`` fixture is the project's "do not corrupt the
developer's real files" ideology made testable: it points ``HOME`` at a
per-test temporary directory. Every test — unit, integration, smoke — gets
this isolation with zero opt-in (``autouse=True``). A buggy test must not
write to the developer's real configuration files.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_new_production_state_artifacts():
    """Tests must not create or modify durable production state under /var/tmp."""
    root = Path("/var/tmp")

    def snapshot():
        state = {}
        entry = root / f"zai-python-helper-{os.getuid()}"
        if entry.exists() or entry.is_symlink():
            if entry.is_symlink():
                state[str(entry)] = ("symlink", entry.readlink())
            elif entry.is_dir():
                for path in (entry, *entry.rglob("*")):
                    if path.is_symlink():
                        state[str(path)] = ("symlink", path.readlink())
                    elif path.is_file():
                        stat_result = path.stat()
                        state[str(path)] = (
                            "file",
                            stat_result.st_mode,
                            path.read_bytes(),
                        )
                    elif path.is_dir():
                        state[str(path)] = ("dir", path.stat().st_mode)
            elif entry.is_file():
                state[str(entry)] = ("file", entry.stat().st_mode, entry.read_bytes())
        return state

    before = snapshot()
    yield
    after = snapshot()
    leaked = sorted(set(before) ^ set(after))
    changed = sorted(path for path in set(before) & set(after) if before[path] != after[path])
    leaked.extend(changed)
    assert not leaked, f"test leaked production state into /var/tmp: {leaked}"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Isolate EVERY test from the real ``$HOME``.

    Sets ``HOME`` to a per-test ``tmp_path``. Yields the isolated home path
    so tests that want to assert against it may request the fixture explicitly.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # ``from_home`` is also used extensively by tests; keep its bookkeeping
    # isolated even when a callsite does not spell out state_home.
    previous_state_home = os.environ.get("ZAI_PYTHON_HELPER_STATE_HOME")
    os.environ["ZAI_PYTHON_HELPER_STATE_HOME"] = str(tmp_path)
    yield tmp_path
    if previous_state_home is None:
        os.environ.pop("ZAI_PYTHON_HELPER_STATE_HOME", None)
    else:
        os.environ["ZAI_PYTHON_HELPER_STATE_HOME"] = previous_state_home
