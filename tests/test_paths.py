"""Tests for the Paths dataclass."""

import pytest

from zai_python_helper.paths import Paths


def test_paths_from_home_resolves_all_fields(_isolate_home):
    """Paths.from_home should resolve all fields off the given home."""
    home = _isolate_home
    paths = Paths.from_home(home)

    assert paths.claude_settings == home / ".claude" / "settings.json"
    assert paths.claude_json == home / ".claude.json"
    assert paths.zshrc == home / ".zshrc"
    assert home not in paths.ownership_json.parents
    assert paths.ownership_json.name == "ownership.json"
    assert paths.recovery_json.name == "recovery.json"
    assert paths.lock_file.name == "lock"
    assert paths.state_dir.name == "state"


def test_paths_from_home_with_string(_isolate_home):
    """Paths.from_home should accept both str and Path."""
    home_str = str(_isolate_home)
    paths = Paths.from_home(home_str)

    assert paths.claude_settings == _isolate_home / ".claude" / "settings.json"


def test_paths_frozen_immutable(_isolate_home):
    """Paths instances should be frozen (immutable)."""
    import dataclasses

    paths = Paths.from_home(_isolate_home)

    with pytest.raises(dataclasses.FrozenInstanceError):  # pragma: no cover
        paths.claude_settings = "/some/other/path"


def test_paths_from_home_no_existence_check(_isolate_home):
    """Paths.from_home should succeed even if paths don't exist."""
    # Use a non-existent home path
    fake_home = _isolate_home / "nonexistent"
    paths = Paths.from_home(fake_home)

    assert fake_home not in paths.state_dir.parents
    # No IO should have occurred
    assert not fake_home.exists()


def test_paths_default_uses_path_home(monkeypatch):
    """Paths.default() should use Path.home()."""
    from pathlib import Path

    # Patch Path.home() to return a predictable value
    fake_home = "/fake/test/home"
    monkeypatch.setenv("HOME", fake_home)

    paths = Paths.default()
    assert paths.state_dir != Path(fake_home) / ".zai-python-helper" / "state"
    assert Path(fake_home) not in paths.state_dir.parents


@pytest.mark.parametrize("value", ["", "relative/state"])
def test_paths_rejects_invalid_xdg_state_home(monkeypatch, tmp_path, value):
    """Invalid XDG roots cannot redirect secrets into the CWD."""
    monkeypatch.setenv("XDG_STATE_HOME", value)
    paths = Paths.from_home(tmp_path)
    assert tmp_path not in paths.state_dir.parents
    assert paths.state_dir.is_absolute()
