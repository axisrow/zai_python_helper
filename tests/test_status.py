"""Tests for the read-only ``status`` detect + render pipeline.

These cover the S4 acceptance criteria from issue #5:
- region detected correctly from the endpoint;
- key is masked (never logged in full);
- a foreign ``export ANTHROPIC_*`` in ``~/.zshrc`` raises a warning;
- the module writes nothing and opens no sockets.

All filesystem state is seeded under the autouse ``_isolate_home`` tmp home
(via :class:`Paths.from_home`), so nothing ever touches the developer's real
files. Rendering is forced to ``use_color=False`` so assertions are stable.
"""

from __future__ import annotations

import json

import pytest

from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.status import (
    ZSHRC_BLOCK_BEGIN,
    ZSHRC_BLOCK_END,
    ClaudeCodeStatus,
    ZshrcState,
    detect_status,
    mask_key,
    render_status,
)

FORCE_PLAIN = {"use_color": False}


# ---------------------------------------------------------------------------
# mask_key
# ---------------------------------------------------------------------------


class TestMaskKey:
    def test_masks_long_key_with_prefix_and_suffix(self):
        # ``zai-`` prefix + 4 bullets + last-4 suffix, per the task spec.
        assert mask_key("zai-abcd3f2a") == "zai-••••3f2a"

    def test_short_key_shown_as_bullets_only(self):
        # Never reveal a short secret in full.
        assert mask_key("abc") == "••••"

    def test_empty_returns_empty(self):
        assert mask_key("") == ""

    def test_token_style_key(self):
        # Z.ai AUTH_TOKEN format: "<id>.<secret>".
        assert mask_key("57a3eeb553bc48d98c70354a758f5af7.wkiny4JhBASee7Np") == (
            "57a3••••e7Np"
        )

    def test_never_leaks_full_value(self):
        secret = "zai-supersecretkeyvalue1234567890"
        masked = mask_key(secret)
        assert secret not in masked
        # Only prefix (<=4) + suffix (4) chars leak.
        leaked = masked.replace("•", "")
        assert len(leaked) <= 8

    @pytest.mark.parametrize("value", ["abcde", "abcdef", "abcdefg", "abcdefgh"])
    def test_short_keys_5_to_8_not_revealed_in_full(self, value):
        """Regression (review cycle 1): for len 5–8 the old prefix+suffix
        overlap reconstructed the whole secret. The masked form must never
        let all of ``value`` be recoverable from its non-bullet chars."""
        masked = mask_key(value)
        non_bullet = masked.replace("•", "")
        assert value != non_bullet
        # The hidden core must be non-empty: prefix + suffix < len(value).
        assert value not in non_bullet

    def test_distinguishes_different_keys(self):
        # Two distinct keys should usually produce distinct masks — the
        # purpose of keeping a visible prefix/suffix.
        a = mask_key("zai-aaa1111122223333")
        b = mask_key("zai-bbb4444455556666")
        assert a != b

    @pytest.mark.parametrize(
        "value", ["", "a", "ab", "abc", "abcd", "abcde", "abcdef", "abcdefgh"]
    )
    def test_value_never_equals_non_bullet_output(self, value):
        """Universal guarantee: stripping bullets never yields the full value."""
        masked = mask_key(value)
        if value == "":
            assert masked == ""
            return
        assert masked.replace("•", "") != value

    @pytest.mark.parametrize("value", ["abcdef", "abcdefg", "abcdefgh"])
    def test_short_keys_6_to_8_fully_hidden(self, value):
        """Regression (review cycle 4): a 6–8 char key must be fully hidden
        (all bullets) — exposing a prefix+suffix that leaves only 1–2 hidden
        chars makes the secret trivially enumerable."""
        assert mask_key(value) == "••••"

    @pytest.mark.parametrize(
        "value,masked",
        [
            ("zai-abcd3f2a", "zai-••••3f2a"),
            ("57a3eeb553bc48d98c70354a758f5af7.wkiny4JhBASee7Np", "57a3••••e7Np"),
        ],
    )
    def test_long_key_keeps_recognizable_ends(self, value, masked):
        """Long keys keep a meaningful prefix/suffix (the task's shape),
        while the hidden core stays >= 4 chars."""
        assert mask_key(value) == masked

    def test_hidden_core_at_least_visible_suffix(self):
        """For every exposed key, the hidden core must be >= the visible
        suffix (default 4) — never a majority disclosed."""
        from zai_python_helper.status import mask_key as mk
        for v in ["abcdefghi", "abcdefghij", "zai-abcd3f2a", "x" * 20]:
            m = mk(v)
            if "••••" not in m or m == "••••":
                continue
            prefix, suffix = m.split("••••")
            hidden = len(v) - len(prefix) - len(suffix)
            assert hidden >= 4, f"{v!r}: hidden core {hidden} < 4"


# ---------------------------------------------------------------------------
# detect_status — Claude Code
# ---------------------------------------------------------------------------


def _write_settings(home, env: dict | None = None) -> None:
    """Seed ``~/.claude/settings.json`` with the given env block."""
    settings = Paths.from_home(home).claude_settings
    settings.parent.mkdir(parents=True, exist_ok=True)
    payload = {"env": env or {}} if env is not None else {}
    settings.write_text(json.dumps(payload), encoding="utf-8")


def _cc(home) -> ClaudeCodeStatus:
    """Detect Claude Code status under ``home`` (asserts it is present).

    A narrow helper so each test reads ``.claude_code`` once and the type
    narrows to non-None for the assertions that follow.
    """
    cc = detect_status(Paths.from_home(home)).claude_code
    assert cc is not None
    return cc


def _zsh(home) -> ZshrcState:
    """Detect the ``.zshrc`` state under ``home`` (read via Claude Code)."""
    return _cc(home).zshrc


class TestDetectClaudeCode:
    def test_no_settings_reports_inactive_and_absent_block(self, tmp_path):
        cc = _cc(tmp_path)

        assert cc.settings_present is False
        assert cc.zai_active is False
        assert cc.region is None
        assert cc.base_url is None
        assert cc.key_masked is None
        assert cc.zshrc.managed_block_present is False
        assert cc.zshrc.foreign_exports == []

    @pytest.mark.parametrize(
        "url,region",
        [
            ("https://api.z.ai/api/anthropic", Region.GLOBAL),
            ("https://api.z.ai", Region.GLOBAL),
            ("https://api.z.cn/api/anthropic", Region.CHINA),
            ("https://api.z.cn", Region.CHINA),
        ],
    )
    def test_region_detected_from_endpoint(self, tmp_path, url, region):
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": url})
        cc = _cc(tmp_path)

        assert cc.zai_active is True
        assert cc.region is region
        assert cc.base_url == url

    def test_non_zai_endpoint_is_inactive(self, tmp_path):
        # The real Anthropic endpoint is "not Z.ai" → inactive, no region.
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
        cc = _cc(tmp_path)

        assert cc.settings_present is True
        assert cc.zai_active is False
        assert cc.region is None
        assert cc.base_url == "https://api.anthropic.com"

    def test_auth_token_key_masked(self, tmp_path):
        _write_settings(
            tmp_path,
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "57a3eeb553bc48d98c70354a758f5af7.wkiny4JhBASee7Np",
            },
        )
        cc = _cc(tmp_path)

        assert cc.key_var == "ANTHROPIC_AUTH_TOKEN"
        assert cc.key_masked == "57a3••••e7Np"
        # Full secret never leaks into the report.
        assert "wkiny4JhBASee7Np" not in (cc.key_masked or "")

    def test_api_key_var_also_supported(self, tmp_path):
        _write_settings(
            tmp_path,
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                "ANTHROPIC_API_KEY": "zai-abcd3f2a",
            },
        )
        cc = _cc(tmp_path)

        assert cc.key_var == "ANTHROPIC_API_KEY"
        assert cc.key_masked == "zai-••••3f2a"

    def test_no_key_when_absent(self, tmp_path):
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        cc = _cc(tmp_path)

        assert cc.key_var is None
        assert cc.key_masked is None

    @pytest.mark.parametrize(
        "bad_value", [123, ["https://api.z.ai"], {"x": 1}, True]
    )
    def test_non_string_base_url_does_not_crash(self, tmp_path, bad_value):
        """Regression (review cycle 3): schema drift may put a non-string
        in ANTHROPIC_BASE_URL. Status must degrade to inactive, not crash."""
        settings = Paths.from_home(tmp_path).claude_settings
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": bad_value}}), encoding="utf-8"
        )
        cc = _cc(tmp_path)

        assert cc.zai_active is False
        assert cc.region is None
        # base_url is never the raw non-string value.
        assert not isinstance(cc.base_url, (int, list, dict, bool))

    def test_non_string_key_value_does_not_crash(self, tmp_path):
        """A non-string ANTHROPIC_API_KEY must not crash mask_key."""
        settings = Paths.from_home(tmp_path).claude_settings
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                        "ANTHROPIC_API_KEY": 12345,
                    }
                }
            ),
            encoding="utf-8",
        )
        cc = _cc(tmp_path)
        # Non-string key is ignored, not crashed on.
        assert cc.key_var is None
        assert cc.key_masked is None

    def test_secret_bearing_endpoint_sanitized(self, tmp_path):
        """Regression (review cycle 3): a URL with credentials in userinfo
        or query must be sanitized before reaching the report."""
        secret_url = (
            "https://user:secretPass@api.z.ai/api/anthropic?key=sk-hiddenkey#frag"
        )
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": secret_url})
        cc = _cc(tmp_path)

        # Still classified as active Z.ai (host is api.z.ai)...
        assert cc.zai_active is True
        assert cc.region is Region.GLOBAL
        # ...but the stored/returned endpoint never carries the secret parts.
        assert "secretPass" not in (cc.base_url or "")
        assert "sk-hiddenkey" not in (cc.base_url or "")
        assert "?key=" not in (cc.base_url or "")
        assert "@api.z.ai" not in (cc.base_url or "")

    @pytest.mark.parametrize(
        "malformed",
        [
            # No "//" before the authority → urlsplit puts userinfo in path.
            "https:user:CREDENTIAL@api.z.ai/path?x=1",
            # Leading non-breaking space breaks authority parsing.
            "\xa0https://user:secret@api.z.ai/p",
        ],
    )
    def test_malformed_endpoint_string_fail_closed(self, tmp_path, malformed):
        """Regression (review cycle 4): malformed endpoint strings that
        defeat urlsplit must NOT leak an embedded credential — fail closed
        to a placeholder rather than echoing the raw value."""
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": malformed})
        cc = _cc(tmp_path)

        # Malformed → not classified as Z.ai (no parseable host) and the raw
        # credential never reaches the stored endpoint or any render.
        assert "CREDENTIAL" not in (cc.base_url or "")
        assert "secret" not in (cc.base_url or "")
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)
        assert "CREDENTIAL" not in out
        assert "secret" not in out

    def test_malformed_settings_json_treated_as_no_env(self, tmp_path):
        # A corrupt settings.json must not crash status — degrade to inactive.
        settings = Paths.from_home(tmp_path).claude_settings
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{ not valid json", encoding="utf-8")

        cc = _cc(tmp_path)
        assert cc.settings_present is True
        assert cc.zai_active is False
        assert cc.base_url is None

    def test_env_must_be_a_dict(self, tmp_path):
        # ``env`` is a list (wrong shape) → treated as no env, no crash.
        _write_settings(tmp_path, env=None)  # writes {} (no env key)
        settings = Paths.from_home(tmp_path).claude_settings
        payload = json.loads(settings.read_text())
        payload["env"] = ["ANTHROPIC_BASE_URL", "https://api.z.ai"]
        settings.write_text(json.dumps(payload), encoding="utf-8")

        cc = _cc(tmp_path)
        assert cc.zai_active is False
        assert cc.base_url is None


# ---------------------------------------------------------------------------
# detect_status — .zshrc
# ---------------------------------------------------------------------------


class TestDetectZshrc:
    def test_managed_block_detected(self, tmp_path):
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            "# user stuff\n"
            f"{ZSHRC_BLOCK_BEGIN}\n"
            "# (our managed block)\n"
            f"{ZSHRC_BLOCK_END}\n",
            encoding="utf-8",
        )
        zsh = _zsh(tmp_path)

        assert zsh.exists is True
        assert zsh.managed_block_present is True
        assert zsh.foreign_exports == []

    def test_foreign_export_outside_block_flagged(self, tmp_path):
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            f"{ZSHRC_BLOCK_BEGIN}\n"
            "# (our managed block)\n"
            f"{ZSHRC_BLOCK_END}\n"
            "export ANTHROPIC_BASE_URL=https://evil.example\n",
            encoding="utf-8",
        )
        zsh = _zsh(tmp_path)

        assert zsh.managed_block_present is True
        # Only the variable NAME is collected — never the assigned value.
        assert zsh.foreign_exports == ["ANTHROPIC_BASE_URL"]

    def test_export_value_never_carried(self, tmp_path):
        """Regression (review cycle 1): the assigned secret value must never
        appear in the detected state — only the variable name."""
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            "export ANTHROPIC_API_KEY=sk-secret-LEAKED-value-12345\n"
            "export ANTHROPIC_AUTH_TOKEN=abc.def-LEAKED\n",
            encoding="utf-8",
        )
        zsh = _zsh(tmp_path)

        assert zsh.foreign_exports == ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]
        # The values are gone from the state entirely.
        assert "sk-secret-LEAKED-value-12345" not in str(zsh.foreign_exports)
        assert "abc.def-LEAKED" not in str(zsh.foreign_exports)

    def test_export_inside_managed_block_not_flagged(self, tmp_path):
        # Per ADR-003 the block is ours; even a stray export inside it is
        # not a "foreign" export and must not warn.
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            f"{ZSHRC_BLOCK_BEGIN}\n"
            "export ANTHROPIC_API_KEY=zai-inside\n"
            f"{ZSHRC_BLOCK_END}\n",
            encoding="utf-8",
        )
        zsh = _zsh(tmp_path)

        assert zsh.managed_block_present is True
        assert zsh.foreign_exports == []

    def test_multiple_foreign_exports_all_flagged(self, tmp_path):
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            "export ANTHROPIC_BASE_URL=https://evil.example\n"
            "export ANTHROPIC_API_KEY=sk-foreign\n",
            encoding="utf-8",
        )
        zsh = _zsh(tmp_path)

        assert zsh.managed_block_present is False
        assert zsh.foreign_exports == ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"]

    def test_inverted_markers_treated_as_no_block(self, tmp_path):
        """Regression (review cycle 1): END before BEGIN (or duplicated
        markers) is a malformed pair — treat as no block so every export
        is flagged rather than the wrong span being sliced out."""
        zshrc = tmp_path / "inverted.zshrc"
        zshrc.write_text(
            f"{ZSHRC_BLOCK_END}\n"
            "export ANTHROPIC_API_KEY=sk-x\n"
            f"{ZSHRC_BLOCK_BEGIN}\n",
            encoding="utf-8",
        )
        from zai_python_helper.status import _read_zshrc

        zsh = _read_zshrc(zshrc)
        assert zsh.managed_block_present is False
        assert zsh.foreign_exports == ["ANTHROPIC_API_KEY"]

    def test_non_anthropic_exports_ignored(self, tmp_path):
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            "export PATH=/usr/local/bin\n"
            "export EDITOR=vim\n",
            encoding="utf-8",
        )
        zsh = _zsh(tmp_path)

        assert zsh.foreign_exports == []

    def test_no_zshrc(self, tmp_path):
        # No .zshrc at all → exists=False, no crash.
        zsh = _zsh(tmp_path)
        assert zsh.exists is False
        assert zsh.managed_block_present is False


# ---------------------------------------------------------------------------
# render_status
# ---------------------------------------------------------------------------


class TestRender:
    def test_active_render_has_block_and_region(self, tmp_path):
        _write_settings(
            tmp_path,
            {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "57a3eeb553bc48d98c70354a758f5af7.wkiny4JhBASee7Np",
            },
        )
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)

        assert "Claude Code" in out
        assert "Z.ai: active" in out
        assert "global" in out
        assert "57a3••••e7Np" in out
        # Full secret never appears in rendered output.
        assert "wkiny4JhBASee7Np" not in out

    def test_inactive_render(self, tmp_path):
        # No settings → inactive, no region.
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)
        assert "Claude Code" in out
        assert "no settings.json found" in out

    def test_foreign_export_warning_rendered(self, tmp_path):
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            "export ANTHROPIC_API_KEY=sk-secret-LEAKED-value-12345\n",
            encoding="utf-8",
        )
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)

        assert "shell env may override settings.json" in out
        # The variable name is shown, but the value is redacted away.
        assert "export ANTHROPIC_API_KEY=<redacted>" in out
        assert "sk-secret-LEAKED-value-12345" not in out

    def test_render_never_discloses_credential(self, tmp_path):
        """Regression (review cycle 1): status output must never carry a
        secret from a foreign shell export."""
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        zshrc = Paths.from_home(tmp_path).zshrc
        zshrc.write_text(
            "export ANTHROPIC_API_KEY=sk-secret-LEAKED-value-12345\n"
            "export ANTHROPIC_AUTH_TOKEN=abc.def-LEAKED\n",
            encoding="utf-8",
        )
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)
        assert "sk-secret-LEAKED-value-12345" not in out
        assert "abc.def-LEAKED" not in out

    def test_render_never_discloses_endpoint_secret(self, tmp_path):
        """Regression (review cycle 3): a credential embedded in the
        endpoint URL (userinfo / query) must never reach the report."""
        secret_url = (
            "https://user:secretPass@api.z.ai/api/anthropic?key=sk-hiddenkey"
        )
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": secret_url})
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)
        assert "secretPass" not in out
        assert "sk-hiddenkey" not in out
        assert "?key=" not in out
        # The sanitized host/path still shown so the endpoint is recognizable.
        assert "api.z.ai/api/anthropic" in out

    def test_no_warning_when_clean(self, tmp_path):
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        out = render_status(detect_status(Paths.from_home(tmp_path)), **FORCE_PLAIN)
        assert "override settings.json" not in out

    def test_color_only_when_tty(self, tmp_path):
        # use_color=True → ANSI escapes present; False → absent.
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        report = detect_status(Paths.from_home(tmp_path))

        colored = render_status(report, use_color=True)
        plain = render_status(report, use_color=False)

        assert "\033[" in colored
        assert "\033[" not in plain

    def test_color_auto_off_for_non_tty_stream(self, tmp_path):
        # A BytesIO/StringIO has no isatty → defaults to plain (test reality).
        import io

        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        report = detect_status(Paths.from_home(tmp_path))

        out = render_status(report, stream=io.StringIO())
        assert "\033[" not in out


# ---------------------------------------------------------------------------
# Acceptance: read-only — never writes, never opens a socket
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_detect_writes_no_files(self, tmp_path):
        paths = Paths.from_home(tmp_path)
        # Snapshot every existing path's content; assert unchanged after.
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        zshrc = paths.zshrc
        zshrc.write_text(
            f"{ZSHRC_BLOCK_BEGIN}\n{ZSHRC_BLOCK_END}\n", encoding="utf-8"
        )

        before_settings = paths.claude_settings.read_text()
        before_zshrc = zshrc.read_text()
        before_mtime_settings = paths.claude_settings.stat().st_mtime_ns
        before_mtime_zshrc = zshrc.stat().st_mtime_ns

        detect_status(paths)

        assert paths.claude_settings.read_text() == before_settings
        assert zshrc.read_text() == before_zshrc
        assert paths.claude_settings.stat().st_mtime_ns == before_mtime_settings
        assert zshrc.stat().st_mtime_ns == before_mtime_zshrc

    def test_detect_makes_no_network_calls(self, tmp_path, monkeypatch):
        # Fail loudly if status tries to open a socket.
        import socket

        def _no_network(*_args, **_kwargs):
            raise AssertionError("status must not open a socket")

        monkeypatch.setattr(socket, "socket", _no_network)
        _write_settings(tmp_path, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        # Must not raise.
        detect_status(Paths.from_home(tmp_path))
