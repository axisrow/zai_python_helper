"""Integration tests: seeded files → ``use zai`` → exact merged output.

HOME-isolated end-to-end through the real CLI handlers (invoked in-process
via the parser, not subprocess, so assertions see the parsed file state).
Covers the issue #3 acceptance criteria:

- ``use zai --region global`` splices the exact env block.
- Idempotent (second run = no-op).
- ``use default`` fully reverts settings.json (4 managed keys gone).
- ``.zshrc``: foreign lines untouched; only the owned block added/removed.
- ``--dry-run`` writes nothing; token redacted in output.
- "restart recommended" printed on change.
"""

from __future__ import annotations

import json
from pathlib import Path

from zai_python_helper.cli import build_parser
from zai_python_helper.paths import Paths

GLOBAL_URL = "https://api.z.ai/api/anthropic"
TOKEN = "sk-integration-token"


def _seed(home: Path, *, settings=None, claude_json=None, zshrc=None) -> Paths:
    """Write seeded config files under ``home`` and return resolved Paths."""
    paths = Paths.from_home(home)
    paths.claude_settings.parent.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        paths.claude_settings.write_text(json.dumps(settings))
    if claude_json is not None:
        paths.claude_json.write_text(json.dumps(claude_json))
    if zshrc is not None:
        paths.zshrc.write_text(zshrc)
    return paths


def _run(argv: list[str]) -> int:
    """Invoke the parser+handler in-process and return the exit code.

    Output is captured via the ``capsys`` fixture at the call site (we read
    ``capsys.readouterr()`` after calling this where assertions need it).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------------------------------
# use zai — exact output
# ---------------------------------------------------------------------------


class TestUseZai:
    def test_use_zai_global_splices_exact_env_block(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={"env": {"SOME_FOREIGN_KEY": "keep", "ANTHROPIC_API_KEY": "sk-old"}},
            claude_json={"theme": "dark"},
            zshrc="export PATH=/bin\n",
        )

        rc = _run(
            ["use", "zai", "--mode", "default", "--region", "global", "--api-key", TOKEN],
        )
        assert rc == 0

        settings = json.loads(
            (Paths.from_home(tmp_path).claude_settings).read_text()
        )
        env = settings["env"]
        # The exact managed block for global + DEFAULT mode.
        assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
        assert env["ANTHROPIC_BASE_URL"] == GLOBAL_URL
        assert env["API_TIMEOUT_MS"] == "3000000"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "zai/glm-4-plus"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "zai/glm-4.7"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "zai/glm-4-flash"
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "zai/glm-4-plus"
        # API_KEY removed; foreign preserved.
        assert "ANTHROPIC_API_KEY" not in env
        assert env["SOME_FOREIGN_KEY"] == "keep"

        out = capsys.readouterr().out
        assert "restart recommended" in out
        # Token never leaks to stdout.
        assert TOKEN not in out

    def test_claude_json_onboarding_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, claude_json={"theme": "dark"})
        _run(["use", "zai", "--region", "global", "--api-key", TOKEN])

        doc = json.loads(Paths.from_home(tmp_path).claude_json.read_text())
        assert doc["hasCompletedOnboarding"] is True
        assert doc["theme"] == "dark"

    def test_zshrc_foreign_survives_block_added(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, zshrc="export PATH=/bin\nalias ll='ls -la'\n")
        _run(["use", "zai", "--region", "global", "--api-key", TOKEN])

        text = Paths.from_home(tmp_path).zshrc.read_text()
        assert "zai-python-helper managed" in text
        assert "export PATH=/bin" in text
        assert "alias ll='ls -la'" in text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_use_zai_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--mode", "default", "--api-key", TOKEN])
        snapshot_settings = Paths.from_home(tmp_path).claude_settings.read_text()
        snapshot_zshrc = Paths.from_home(tmp_path).zshrc.read_text()

        # Second run.
        _run(["use", "zai", "--mode", "default", "--api-key", TOKEN])

        assert Paths.from_home(tmp_path).claude_settings.read_text() == snapshot_settings
        assert Paths.from_home(tmp_path).zshrc.read_text() == snapshot_zshrc

    def test_second_use_zai_reports_no_changes(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()  # drain
        _run(["use", "zai", "--api-key", TOKEN])
        assert "no changes" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# use default — full revert
# ---------------------------------------------------------------------------


class TestUseDefault:
    def test_default_removes_four_managed_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={
                "env": {
                    "SOME_FOREIGN_KEY": "keep",
                    "ANTHROPIC_AUTH_TOKEN": TOKEN,
                    "ANTHROPIC_BASE_URL": GLOBAL_URL,
                    "API_TIMEOUT_MS": "3000000",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                }
            },
        )
        rc = _run(["use", "default", "--mode", "default", "--region", "global"])
        assert rc == 0

        settings = json.loads(Paths.from_home(tmp_path).claude_settings.read_text())
        env = settings["env"]
        # The four always-managed keys gone.
        for key in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "API_TIMEOUT_MS",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        ):
            assert key not in env
        assert env == {"SOME_FOREIGN_KEY": "keep"}

    def test_default_does_not_touch_claude_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, claude_json={"theme": "dark", "hasCompletedOnboarding": True})
        before = Paths.from_home(tmp_path).claude_json.read_text()
        _run(["use", "default", "--region", "global"])
        assert Paths.from_home(tmp_path).claude_json.read_text() == before

    def test_default_removes_zshrc_block_keeps_foreign(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, zshrc="export PATH=/bin\n")
        _run(["use", "zai", "--api-key", TOKEN])
        _run(["use", "default", "--region", "global"])

        text = Paths.from_home(tmp_path).zshrc.read_text()
        assert "zai-python-helper managed" not in text
        assert "export PATH=/bin" in text

    def test_round_trip_zai_then_default_restores_foreign(self, tmp_path, monkeypatch):
        """Full round-trip: original foreign state restored after zai→default."""
        monkeypatch.setenv("HOME", str(tmp_path))
        original_env = {"FOREIGN": "keep", "ANTHROPIC_API_KEY": "sk-old"}
        _seed(tmp_path, settings={"env": dict(original_env)})

        _run(["use", "zai", "--mode", "default", "--api-key", TOKEN])
        _run(["use", "default", "--mode", "default", "--region", "global"])

        settings = json.loads(Paths.from_home(tmp_path).claude_settings.read_text())
        # Foreign key survives; managed keys + API_KEY both gone.
        assert settings["env"] == {"FOREIGN": "keep"}


# ---------------------------------------------------------------------------
# --dry-run — writes nothing, redacts, prints diff
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, settings={"env": {"FOO": "bar"}})

        rc = _run(
            ["use", "zai", "--mode", "default", "--region", "global", "--api-key", TOKEN, "--dry-run"],
        )
        assert rc == 0

        # settings.json untouched.
        assert json.loads(Paths.from_home(tmp_path).claude_settings.read_text()) == {
            "env": {"FOO": "bar"}
        }
        # .zshrc was never created.
        assert not Paths.from_home(tmp_path).zshrc.exists()
        # .claude.json was never created.
        assert not Paths.from_home(tmp_path).claude_json.exists()

        out = capsys.readouterr().out
        assert "no files written" in out

    def test_dry_run_prints_diff_and_redacts_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={"env": {"ANTHROPIC_API_KEY": "sk-real-secret"}},
        )

        _run(
            ["use", "zai", "--region", "global", "--api-key", "sk-real-secret", "--dry-run"],
        )
        out = capsys.readouterr().out

        # A unified_diff is printed (contains the +++/--- headers).
        assert "+++" in out
        assert "---" in out
        # The secret value must never appear; only <redacted>.
        assert "sk-real-secret" not in out
        assert "<redacted>" in out

    def test_dry_run_on_already_desired_prints_no_changes(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])  # real write
        capsys.readouterr()
        _run(["use", "zai", "--api-key", TOKEN, "--dry-run"])
        out = capsys.readouterr().out
        assert "no changes" in out.lower()
        # No diff lines emitted.
        assert "+++" not in out


# ---------------------------------------------------------------------------
# status / postconditions wiring
# ---------------------------------------------------------------------------


class TestStatusWiring:
    def test_status_reports_active_after_use_zai(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()
        _run(["status", "--region", "global"])
        out = capsys.readouterr().out
        assert "zai_active: True" in out

    def test_status_reports_inactive_at_default(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["status", "--region", "global"])
        out = capsys.readouterr().out
        assert "zai_active: False" in out
