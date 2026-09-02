"""Integration tests: seeded files → ``use zai`` → exact merged output.

HOME-isolated end-to-end through the real CLI handlers (invoked in-process
via the parser, not subprocess, so assertions see the parsed file state).
Covers the issue #3 acceptance criteria:

- ``use zai --region global`` splices the exact env block.
- Idempotent (second run = no-op).
- ``use default`` fully reverts settings.json (4 managed keys gone).
- ``.zshrc``: foreign lines untouched; only the owned block added/removed.
- ``--dry-run`` writes nothing; token redacted in output.
- ``use default`` real runs print NOTHING to stdout (issue #125 parity
  contract; informational output returns as opt-in ``--verbose`` in #128).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zai_python_helper.cli import _print_diff, build_parser
from zai_python_helper.core.planner import FileTag
from zai_python_helper.paths import Paths

GLOBAL_URL = "https://api.z.ai/api/anthropic"
TOKEN = "sk-integration-token"


def _seed(home: Path, *, settings=None, claude_json=None, zshrc=None) -> Paths:
    """Write seeded config files under ``home`` and return resolved Paths."""
    paths = Paths.from_home(home, state_home=home)
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
            (Paths.from_home(tmp_path, state_home=tmp_path).claude_settings).read_text()
        )
        env = settings["env"]
        # The exact managed block for global + DEFAULT mode.
        assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
        assert env["ANTHROPIC_BASE_URL"] == GLOBAL_URL
        assert env["API_TIMEOUT_MS"] == "3000000"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == 1
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "zai/glm-4-plus"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "zai/glm-4.7"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "zai/glm-4-flash"
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "zai/glm-4-plus"
        # API_KEY removed; foreign preserved.
        assert "ANTHROPIC_API_KEY" not in env
        assert env["SOME_FOREIGN_KEY"] == "keep"

        out = capsys.readouterr().out
        assert "GLM configuration reloaded to Claude Code successfully" in out
        # Token never leaks to stdout.
        assert TOKEN not in out

    def test_claude_json_onboarding_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, claude_json={"theme": "dark"})
        _run(["use", "zai", "--region", "global", "--api-key", TOKEN])

        doc = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_json.read_text())
        assert doc["hasCompletedOnboarding"] is True
        assert doc["theme"] == "dark"

    def test_zshrc_is_not_modified_in_phase1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, zshrc="export PATH=/bin\nalias ll='ls -la'\n")
        _run(["use", "zai", "--region", "global", "--api-key", TOKEN])

        text = Paths.from_home(tmp_path, state_home=tmp_path).zshrc.read_text()
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
        snapshot_settings = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()

        # Second run.
        _run(["use", "zai", "--mode", "default", "--api-key", TOKEN])

        assert Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text() == snapshot_settings
        assert not Paths.from_home(tmp_path, state_home=tmp_path).zshrc.exists()

    def test_second_use_zai_reports_no_changes(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()  # drain
        _run(["use", "zai", "--api-key", TOKEN])
        assert capsys.readouterr().out == (
            "Reloading GLM configuration to Claude Code...\n"
            "GLM configuration reloaded to Claude Code successfully\n"
        )


# ---------------------------------------------------------------------------
# use default — full revert
# ---------------------------------------------------------------------------


class TestUseDefault:
    def test_default_removes_four_managed_keys(self, tmp_path, monkeypatch):
        """S3: managed keys are removed via OWNERSHIP (RESTORE of an absent prior).

        Previously (S2) ``use default`` blindly deleted managed keys even with no
        provenance. S3 changed that to non-destructive revert (ADR-004): the
        four managed ZAI keys are removed because they were absent before
        activation and ``use zai`` recorded that absence — so ``revert``
        RESTORES the (absent) prior. Foreign keys survive. The keys are NOT
        touched without a prior ``use zai`` (see
        test_use_default_without_prior_activation_refuses).
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, settings={"env": {"SOME_FOREIGN_KEY": "keep"}})
        # Activate (the managed keys were ABSENT before — journaled as such).
        _run(["use", "zai", "--mode", "default", "--region", "global", "--api-key", TOKEN])
        rc = _run(["use", "default", "--mode", "default", "--region", "global"])
        assert rc == 0

        settings = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text())
        env = settings["env"]
        # The four always-managed keys gone (restored to their absent prior).
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
        before = Paths.from_home(tmp_path, state_home=tmp_path).claude_json.read_text()
        _run(["use", "default", "--region", "global"])
        assert Paths.from_home(tmp_path, state_home=tmp_path).claude_json.read_text() == before

    def test_default_removes_zshrc_block_keeps_foreign(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # A block can only be present from a pre-Phase-1 activation: fresh
        # activation intentionally leaves .zshrc untouched.
        _seed(
            tmp_path,
            zshrc=(
                "export PATH=/bin\n\n"
                "# >>> zai-python-helper managed >>>\n"
                "# legacy managed body\n"
                "# <<< zai-python-helper managed <<<\n"
            ),
        )
        _run(["use", "default", "--region", "global"])

        text = Paths.from_home(tmp_path, state_home=tmp_path).zshrc.read_text()
        assert "zai-python-helper managed" not in text
        assert "export PATH=/bin" in text

    def test_round_trip_zai_then_default_restores_foreign(self, tmp_path, monkeypatch):
        """Full round-trip: ORIGINAL state restored after zai→default (S3).

        S3 changed this from S2's blind deletion: ``use zai`` removes the
        user's ``ANTHROPIC_API_KEY`` (Z.ai auths via AUTH_TOKEN), but now
        records that removal in the ownership journal. ``use default`` then
        RESTORES the original ``ANTHROPIC_API_KEY=sk-old`` rather than
        discarding it — non-destructive revert. Foreign keys always survive.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        original_env = {"FOREIGN": "keep", "ANTHROPIC_API_KEY": "sk-old"}
        _seed(tmp_path, settings={"env": dict(original_env)})

        _run(["use", "zai", "--mode", "default", "--api-key", TOKEN])
        _run(["use", "default", "--mode", "default", "--region", "global"])

        settings = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text())
        # Foreign key survives; the managed ZAI keys are gone; AND the original
        # ANTHROPIC_API_KEY is RESTORED (not blindly deleted as in S2).
        assert settings["env"] == {"FOREIGN": "keep", "ANTHROPIC_API_KEY": "sk-old"}

    def test_cross_mode_default_strips_stale_model_keys(self, tmp_path, monkeypatch):
        """Regression (Codex finding): ``use zai --mode default`` then a bare
        ``use default`` (ORIGINAL) must still strip the DEFAULT-mode model
        keys. Revert is mode-agnostic — otherwise stale Z.ai model IDs are
        left behind with no auth/URL.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--mode", "default", "--api-key", TOKEN])
        # Bare revert — defaults to ORIGINAL mode, which contributes no model keys.
        _run(["use", "default"])

        env = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()).get(
            "env", {}
        )
        for stale in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        ):
            assert stale not in env

    def test_use_zai_echo_never_prints_foreign_secrets(self, tmp_path, monkeypatch, capsys):
        """Regression (Codex finding): the post-activation echo must print ONLY
        tool-owned managed keys, never foreign env values (e.g. an unrelated
        OPENAI_API_KEY). Foreign secrets must not reach stdout.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={"env": {"OPENAI_API_KEY": "sk-foreign-secret-xyz"}},
        )
        _run(["use", "zai", "--api-key", TOKEN])

        out = capsys.readouterr().out
        # The foreign secret must NEVER appear in stdout.
        assert "sk-foreign-secret-xyz" not in out
        assert "OPENAI_API_KEY" not in out
        # The public output is the upstream-compatible status only.
        assert TOKEN not in out
        assert out == (
            "Reloading GLM configuration to Claude Code...\n"
            "GLM configuration reloaded to Claude Code successfully\n"
        )


# ---------------------------------------------------------------------------
# S3: ownership journal end-to-end (ADR-004 / ADR-005)
# ---------------------------------------------------------------------------


class TestOwnershipJournalE2E:
    """End-to-end S3 scenarios through the real CLI handlers.

    These exercise the journal wire-up: ``use zai`` records ownership, and
    ``use default`` consults it so the revert is non-destructive.
    """

    def test_use_zai_writes_ownership_json_mode_0600(self, tmp_path, monkeypatch):
        """``use zai`` creates ownership.json at mode 0600 (issue #4 acceptance)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])

        journal = Paths.from_home(tmp_path, state_home=tmp_path).ownership_json
        assert journal.exists()
        mode = journal.stat().st_mode & 0o777
        assert mode == 0o600

    def test_repeat_activation_preserves_original_restore_point(
        self, tmp_path, monkeypatch
    ):
        """P→Z→Z→default restores the ORIGINAL P, not the re-activated Z.

        S3 regression (Codex finding #1): a repeat ``use zai`` (incl. an
        all-NOOP one) must NOT overwrite the journal's prior with the
        now-current value. The original pre-activation value is the restore
        point; re-activating the same value preserves it. Includes the
        ANTHROPIC_API_KEY (which ``use zai`` removes): a repeat activation
        must still restore the user's original API key on revert.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        original_token = "sk-user-original-P"
        original_apikey = "sk-user-apikey-P"
        _seed(
            tmp_path,
            settings={
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": original_token,
                    "ANTHROPIC_API_KEY": original_apikey,
                }
            },
        )
        # Activate Z, then re-activate Z (same value → idempotent).
        _run(["use", "zai", "--api-key", TOKEN])
        _run(["use", "zai", "--api-key", TOKEN])  # repeat — must not clobber prior
        _run(["use", "default", "--region", "global"])

        env = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()).get(
            "env", {}
        )
        # ORIGINAL values restored, not the Z.ai token.
        assert env.get("ANTHROPIC_AUTH_TOKEN") == original_token
        # The API key we removed on activation is restored to the original.
        assert env.get("ANTHROPIC_API_KEY") == original_apikey

    def test_token_rotation_preserves_original_restore_point(
        self, tmp_path, monkeypatch
    ):
        """P→Z1→Z2→default restores the ORIGINAL P, not the previous Z1.

        S3 regression (Codex cycle-3): rotating the Z.ai token between two
        activations must NOT replace the restore point with the PREVIOUS Z.ai
        token. ``use default`` after Z1→Z2 restores the user's original P —
        never a stale Z.ai credential that would auth against the wrong
        (default) endpoint.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        original_token = "sk-user-original-P"
        _seed(
            tmp_path,
            settings={"env": {"ANTHROPIC_AUTH_TOKEN": original_token}},
        )
        _run(["use", "zai", "--api-key", "sk-zai-1"])
        _run(["use", "zai", "--api-key", "sk-zai-2"])  # rotate
        _run(["use", "default", "--region", "global"])

        env = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()).get(
            "env", {}
        )
        # The ORIGINAL user token is restored — NOT the previous Z.ai token.
        assert env.get("ANTHROPIC_AUTH_TOKEN") == original_token
        assert env.get("ANTHROPIC_AUTH_TOKEN") != "sk-zai-1"

    def test_use_default_refuses_when_user_readded_api_key(
        self, tmp_path, monkeypatch, capsys
    ):
        """If the user re-added ANTHROPIC_API_KEY after use zai removed it,
        use default REFUSES (does not clobber the new key).

        S3 regression (Codex finding #2): ownership-by-removal must restore the
        prior ONLY while the key is still absent. A reappeared value is an
        external change → REFUSE. The real run is silent (issue #125); the
        REFUSE warning returns as opt-in ``--verbose`` in #128.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={"env": {"ANTHROPIC_API_KEY": "sk-old-removed-on-activation"}},
        )
        _run(["use", "zai", "--api-key", TOKEN])  # removes the old API key
        capsys.readouterr()

        # User manually adds a NEW API key after activation.
        settings_path = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings
        doc = json.loads(settings_path.read_text())
        doc.setdefault("env", {})["ANTHROPIC_API_KEY"] = "sk-user-new-after-zai"
        settings_path.write_text(json.dumps(doc))

        _run(["use", "default", "--region", "global"])

        env = json.loads(settings_path.read_text()).get("env", {})
        # The user's NEW key is preserved (not replaced by the stale old one).
        assert env.get("ANTHROPIC_API_KEY") == "sk-user-new-after-zai"
        assert capsys.readouterr().out == ""

    def test_use_default_restores_original_auth_token(self, tmp_path, monkeypatch):
        """The headline S3 guarantee: original AUTH_TOKEN restored after zai→default."""
        monkeypatch.setenv("HOME", str(tmp_path))
        original_token = "sk-user-original-token"
        _seed(
            tmp_path,
            settings={"env": {"ANTHROPIC_AUTH_TOKEN": original_token}},
        )
        _run(["use", "zai", "--api-key", TOKEN])
        _run(["use", "default", "--region", "global"])

        env = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()).get(
            "env", {}
        )
        # The user's ORIGINAL token is back — not the Z.ai one, not deleted.
        assert env.get("ANTHROPIC_AUTH_TOKEN") == original_token

    def test_use_default_refuses_on_external_change(self, tmp_path, monkeypatch, capsys):
        """If the user edited AUTH_TOKEN after use zai, use default leaves it.

        ADR-004: the key changed externally → REFUSE (do not overwrite). The
        edited value survives; the real run is silent (issue #125 — the
        warning line returns as opt-in ``--verbose`` in #128; ``--dry-run``
        still previews REFUSE decisions).
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()

        # The user manually edits the token to something we never set.
        settings_path = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings
        doc = json.loads(settings_path.read_text())
        doc["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-edited-by-user"
        settings_path.write_text(json.dumps(doc))

        _run(["use", "default", "--region", "global"])

        env = json.loads(settings_path.read_text()).get("env", {})
        # The edited value is PRESERVED (not overwritten, not deleted).
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-edited-by-user"
        assert capsys.readouterr().out == ""

    def test_use_default_dry_run_previews_refuse_warning(
        self, tmp_path, monkeypatch, capsys
    ):
        """``use default --dry-run`` keeps naming REFUSE keys on stdout.

        With the real run silent (issue #125), the dry-run preview is the only
        surface that tells the user WHICH externally-changed key will be left
        untouched. This pins the contract the silencing change relies on (PR
        #129 docstring / CHANGELOG / PR body): a later refactor (e.g. #128's
        --verbose work) must not drop the preview's REFUSE warnings silently.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()

        # The user manually edits the token to something we never set.
        settings_path = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings
        doc = json.loads(settings_path.read_text())
        doc["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-edited-by-user"
        settings_path.write_text(json.dumps(doc))
        before = settings_path.read_text()

        _run(["use", "default", "--region", "global", "--dry-run"])

        out = capsys.readouterr().out
        # The preview warns and NAMES the key it refuses to touch...
        assert "warning" in out.lower()
        assert "ANTHROPIC_AUTH_TOKEN" in out
        # ...and writes nothing (read-only preview).
        assert settings_path.read_text() == before

    def test_use_default_verbose_restores_feedback_lines(
        self, tmp_path, monkeypatch, capsys
    ):
        """Opt-in ``--verbose`` (issue #128) restores the feedback silenced in
        #125 — header, ``updated:`` lines, restart notice — and changes
        NOTHING else: same exit code, byte-identical files as the silent run.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, settings={"env": {"ANTHROPIC_API_KEY": "sk-original"}})
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()

        # Reference: the silent run's on-disk result (already pinned empty).
        rc = _run(["use", "default", "--region", "global", "--dry-run"])
        assert rc == 0
        capsys.readouterr()

        rc = _run(["use", "default", "--region", "global", "--verbose"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Reverting to default provider (tool: claude_code, region: global)\n" in out
        assert "  updated: " in out
        assert "restart recommended for deterministic switching" in out

    def test_use_default_verbose_names_refused_keys(self, tmp_path, monkeypatch, capsys):
        """``--verbose`` re-surfaces the fail-closed REFUSE explanation that
        the silent default hides (issue #128); the externally-changed value is
        still preserved and the exit code is unchanged.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path)
        _run(["use", "zai", "--api-key", TOKEN])
        capsys.readouterr()

        settings_path = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings
        doc = json.loads(settings_path.read_text())
        doc["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-edited-by-user"
        settings_path.write_text(json.dumps(doc))

        rc = _run(["use", "default", "--region", "global", "--verbose"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "warning" in out.lower()
        assert "ANTHROPIC_AUTH_TOKEN" in out
        # The refusal semantics are unchanged: the user's edit survives.
        env = json.loads(settings_path.read_text()).get("env", {})
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-edited-by-user"

    def test_use_zai_does_not_accept_verbose(self, tmp_path, monkeypatch):
        """``use zai`` is excluded from --verbose (issue #128): its two pinned
        ``chelper auth reload`` lines ARE the parity contract (issue #125), so
        the flag must not exist on that subparser.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(
                ["use", "zai", "--api-key", TOKEN, "--verbose"]
            )
        assert excinfo.value.code == 2

    def test_use_default_idempotent_after_revert(self, tmp_path, monkeypatch, capsys):
        """A second ``use default`` after revert is a no-op (REFUSE/RESTORE settle)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, settings={"env": {"ANTHROPIC_API_KEY": "sk-original"}})
        _run(["use", "zai", "--api-key", TOKEN])
        _run(["use", "default", "--region", "global"])
        snapshot = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()
        capsys.readouterr()

        _run(["use", "default", "--region", "global"])
        # State unchanged by the second revert.
        assert Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text() == snapshot
        # Silent no-op too (issue #125 parity contract).
        assert capsys.readouterr().out == ""

    def test_completed_cycle_does_not_resurrect_deleted_removal_key(
        self, tmp_path, monkeypatch
    ):
        """THE removal-path data-loss fix, end-to-end (issue #48).

        Sequence: user has ANTHROPIC_API_KEY=P1 → ``use zai`` removes it
        (journals prior=P1) → ``use default`` restores P1 AND retires the
        record (cycle completed, ``active=False``) → user DELETES P1 themselves
        → ``use zai`` re-activates (key absent) → ``use default``.

        The key is ABSENT both during the original removal and after the user's
        delete, so absence alone cannot tell those apart. Without cycle-state
        the second ``use default`` would RESTORE the stale prior=P1 — silently
        RESURRECTING a credential the user deliberately removed (data loss).
        With ``active=False`` the completed cycle forces a fresh prior (the key
        really is absent now), so the deleted P1 stays dead.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        p1 = "sk-user-apikey-P1"
        _seed(tmp_path, settings={"env": {"ANTHROPIC_API_KEY": p1}})
        settings_path = Paths.from_home(tmp_path, state_home=tmp_path).claude_settings

        # use zai removes the user's API key (ownership-by-removal).
        _run(["use", "zai", "--api-key", TOKEN])
        assert "ANTHROPIC_API_KEY" not in json.loads(settings_path.read_text()).get("env", {})

        # use default restores P1 (cycle completed → journal retired active=False).
        _run(["use", "default", "--region", "global"])
        assert json.loads(settings_path.read_text())["env"]["ANTHROPIC_API_KEY"] == p1

        # The user deliberately DELETES the restored key themselves.
        doc = json.loads(settings_path.read_text())
        doc["env"].pop("ANTHROPIC_API_KEY")
        settings_path.write_text(json.dumps(doc))

        # Re-activate by removal (key is now absent — a completed cycle).
        _run(["use", "zai", "--api-key", TOKEN])
        assert "ANTHROPIC_API_KEY" not in json.loads(settings_path.read_text()).get("env", {})

        # use default must NOT resurrect P1 — the deleted credential stays dead.
        _run(["use", "default", "--region", "global"])
        assert "ANTHROPIC_API_KEY" not in json.loads(settings_path.read_text()).get("env", {})

    def test_use_default_without_prior_activation_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        """S3: ``use default`` with NO journal REFUSES to touch unproven keys.

        Non-destructive invariant (ADR-004 / Codex finding #3): without a
        prior ``use zai`` there is no provenance, so the tool must NOT delete
        managed-name keys the user may have configured by hand. The real run
        is silent (issue #125) and leaves everything; only the owned .zshrc
        block (if any) is removed.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={
                "env": {
                    "FOREIGN": "keep",
                    "ANTHROPIC_AUTH_TOKEN": "sk-stray",
                    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                }
            },
        )
        _run(["use", "default", "--region", "global"])

        env = json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()).get(
            "env", {}
        )
        # No provenance → keys left untouched (NOT blindly deleted).
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-stray"
        assert env.get("ANTHROPIC_BASE_URL") == "https://api.z.ai/api/anthropic"
        assert env.get("FOREIGN") == "keep"
        assert capsys.readouterr().out == ""

    def test_use_zai_rolls_forward_after_interrupted_prior_run(
        self, tmp_path, monkeypatch, capsys
    ):
        """A surviving recovery manifest is replayed BEFORE a new activation (ADR-005).

        Simulate a hard-killed prior ``use``: a recovery manifest is on disk
        but the managed files were not (fully) written. The next ``use zai``
        rolls the manifest forward (re-applies it, warns) and then proceeds
        with its own activation — leaving a clean state and no manifest.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        # Hand-craft a manifest from a prior interrupted run: settings final
        # state recorded, but the file itself never landed on disk.
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "entries": [
                {
                    "tag": "settings",
                    "path": str(paths.claude_settings),
                    "kind": "json",
                    "content": json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-recovered"}}) + "\n",
                }
            ]
        }
        paths.recovery_json.write_text(json.dumps(manifest))
        assert not paths.claude_settings.exists()

        _run(["use", "zai", "--api-key", TOKEN])

        captured = capsys.readouterr()
        # The recovery warning goes to STDERR so stdout stays a strict
        # process contract (the pinned two-line activate output, issue #125).
        assert "recovered from an interrupted prior run" in captured.err
        assert captured.out == (
            "Reloading GLM configuration to Claude Code...\n"
            "GLM configuration reloaded to Claude Code successfully\n"
        )
        # The manifest is consumed; the new activation completed cleanly.
        assert not paths.recovery_json.exists()
        env = json.loads(paths.claude_settings.read_text()).get("env", {})
        # The NEW activation's token wins (the manifest's settings was the
        # pre-empted run's intent; the new run replaces it).
        assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN

    def test_use_default_recovery_case_is_stdout_silent(
        self, tmp_path, monkeypatch, capsys
    ):
        """A recovery replay during ``use default`` keeps stdout EMPTY.

        Cycle-review round 2 (PR #129): the recovery diagnostic used to print
        to stdout, breaking the silent real-run contract (issue #125) with a
        rc=0 run. It must surface on stderr instead.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        paths.recovery_json.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "entries": [
                {
                    "tag": "settings",
                    "path": str(paths.claude_settings),
                    "kind": "json",
                    "content": json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-recovered"}}) + "\n",
                }
            ]
        }
        paths.recovery_json.write_text(json.dumps(manifest))

        _run(["use", "default", "--region", "global"])

        captured = capsys.readouterr()
        assert "recovered from an interrupted prior run" in captured.err
        assert captured.out == ""
        # The manifest was replayed (roll-forward), not left behind.
        assert not paths.recovery_json.exists()


# ---------------------------------------------------------------------------
# Bug 7 (issue #60): journal retirement is ATOMIC with the revert commit
# ---------------------------------------------------------------------------


class TestRevertJournalAtomicity:
    """A crash mid-revert must never strand the user's prior (issue #60).

    Before the fix, ``use default`` persisted the retired journal
    (``active=False``) BEFORE the recovery manifest and before any config
    write. A kill in that window left ``active=False`` durable while the
    config still held our value — and after the Bug 6 fix (#54/#55) the
    inactive record makes every later ``use default`` REFUSE, so the prior
    became permanently unreachable (data loss).

    The fix folds the journal's final text into the recovery manifest, so the
    retirement and the RESTORE it describes commit together or not at all.
    """

    @staticmethod
    def _crash_on(monkeypatch, tag: str) -> None:
        """Make the commit of the entry tagged ``tag`` raise (simulated kill)."""
        from zai_python_helper import patchplan

        real = patchplan._apply_entry

        def crashing(entry):
            if entry.tag == tag:
                raise RuntimeError("simulated kill mid-commit")
            real(entry)

        monkeypatch.setattr(patchplan, "_apply_entry", crashing)

    def test_crash_mid_revert_leaves_journal_active_and_manifest_pending(
        self, tmp_path, monkeypatch
    ):
        """At the crash point: journal NOT yet retired, manifest carries it.

        This is the state assertion the ordering bug got wrong. The retirement
        must not be durable while the RESTORE is not.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        prior = "sk-user-P"
        _seed(tmp_path, settings={"env": {"ANTHROPIC_AUTH_TOKEN": prior}})
        _run(["use", "zai", "--api-key", TOKEN])

        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        self._crash_on(monkeypatch, "settings")
        with pytest.raises(RuntimeError):
            _run(["use", "default", "--region", "global"])

        record = json.loads(paths.ownership_json.read_text())["claude_code"][
            "ANTHROPIC_AUTH_TOKEN"
        ]
        # The on-disk journal still says the cycle is IN FLIGHT.
        assert record["active"] is True
        assert record["prior_value"] == prior
        # And the pending manifest carries the retirement it would have made.
        manifest = json.loads(paths.recovery_json.read_text())
        assert "journal" in manifest
        assert manifest["journal"]["path"] == str(paths.ownership_json)

    def test_crash_mid_revert_recovers_prior_on_next_run(
        self, tmp_path, monkeypatch, capsys
    ):
        """THE issue #60 regression: a killed revert must not strand the prior.

        Kill ``use default`` between the (pre-fix) journal retirement and the
        config write, then run ``use default`` again. The prior MUST come back
        — not a permanent REFUSE.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        prior = "sk-user-P"
        _seed(tmp_path, settings={"env": {"ANTHROPIC_AUTH_TOKEN": prior}})
        _run(["use", "zai", "--api-key", TOKEN])

        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        self._crash_on(monkeypatch, "settings")
        with pytest.raises(RuntimeError):
            _run(["use", "default", "--region", "global"])
        monkeypatch.undo()
        monkeypatch.setenv("HOME", str(tmp_path))
        capsys.readouterr()

        _run(["use", "default", "--region", "global"])

        env = json.loads(paths.claude_settings.read_text()).get("env", {})
        # The user's ORIGINAL value is back (roll-forward completed the revert).
        assert env.get("ANTHROPIC_AUTH_TOKEN") == prior
        assert "ANTHROPIC_BASE_URL" not in env
        # The cycle is now properly closed and the manifest consumed.
        assert (
            json.loads(paths.ownership_json.read_text())["claude_code"][
                "ANTHROPIC_AUTH_TOKEN"
            ]["active"]
            is False
        )
        assert not paths.recovery_json.exists()

    def test_clean_revert_still_retires_the_journal(self, tmp_path, monkeypatch):
        """No regression on the happy path: a completed revert retires the record.

        The Bug 6 fix (#54/#55) depends on the retirement actually landing —
        moving it into the manifest must not drop it.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(tmp_path, settings={"env": {"ANTHROPIC_AUTH_TOKEN": "sk-user-P"}})
        _run(["use", "zai", "--api-key", TOKEN])
        _run(["use", "default", "--region", "global"])

        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        record = json.loads(paths.ownership_json.read_text())["claude_code"][
            "ANTHROPIC_AUTH_TOKEN"
        ]
        assert record["active"] is False
        assert not paths.recovery_json.exists()
        assert paths.ownership_json.stat().st_mode & 0o777 == 0o600


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
        assert json.loads(Paths.from_home(tmp_path, state_home=tmp_path).claude_settings.read_text()) == {
            "env": {"FOO": "bar"}
        }
        # .zshrc was never created.
        assert not Paths.from_home(tmp_path, state_home=tmp_path).zshrc.exists()
        # .claude.json was never created.
        assert not Paths.from_home(tmp_path, state_home=tmp_path).claude_json.exists()

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

    def test_dry_run_diff_records_are_separated_without_json_newline(
        self, tmp_path, capsys
    ):
        """Adjacent diffs remain readable when JSON has no final newline."""
        _print_diff(tmp_path / "one.json", "", "}", FileTag.SETTINGS)
        _print_diff(tmp_path / "two.json", "", "}", FileTag.CLAUDE_JSON)

        out = capsys.readouterr().out
        assert "+}\n---" in out
        assert "+}---" not in out

    def test_dry_run_redacts_foreign_secret_in_diff(self, tmp_path, monkeypatch, capsys):
        """Regression (Codex F1): a foreign secret in settings.json must be
        redacted in the --dry-run diff context, not just the Anthropic keys.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            settings={"env": {"OPENAI_API_KEY": "sk-foreign-secret-xyz"}},
        )
        _run(
            ["use", "zai", "--mode", "default", "--region", "global", "--api-key", TOKEN, "--dry-run"],
        )
        out = capsys.readouterr().out
        assert "+++" in out  # a diff was printed
        # The foreign secret never appears — it is redacted as diff context.
        assert "sk-foreign-secret-xyz" not in out
        assert "<redacted>" in out

    def test_dry_run_redacts_shell_secret_in_zshrc_diff(self, tmp_path, monkeypatch, capsys):
        """Regression (Codex cycle-3): a shell ``export`` secret in .zshrc must
        be redacted in the --dry-run diff context (redaction covers shell
        syntax, not only JSON).
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        _seed(
            tmp_path,
            zshrc="export PATH=/bin\nexport OPENAI_API_KEY=sk-shell-secret-xyz\n",
        )
        _run(["use", "zai", "--api-key", TOKEN, "--dry-run"])
        out = capsys.readouterr().out
        # The block is appended → a zshrc diff is printed.
        assert "+++" in out
        assert "sk-shell-secret-xyz" not in out
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
