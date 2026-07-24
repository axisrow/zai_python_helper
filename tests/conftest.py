"""Project-wide pytest configuration and shared fixtures.

The autouse ``_isolate_home`` fixture is the project's "do not corrupt the
developer's real files" ideology made testable: it points ``HOME`` at a
per-test temporary directory. Every test — unit, integration, smoke — gets
this isolation with zero opt-in (``autouse=True``). A buggy test must not
write to the developer's real configuration files.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Isolate EVERY test from the real ``$HOME``.

    Sets ``HOME`` to a per-test ``tmp_path``. Yields the isolated home path
    so tests that want to assert against it may request the fixture explicitly.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path
