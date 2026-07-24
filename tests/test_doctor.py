"""Tests for the ``doctor`` diagnostic pipeline (S5, issue #6).

The HTTP probe is exercised through the real :func:`urllib_get` seam pointed
at a pytest-httpserver instance — a REAL socket, no monkeypatched network — so
the 200-OK / 401-bad-key / offline acceptance criteria are tested end-to-end.

Conventions:
- ``settings.json`` is written through the injected :class:`Paths` (tmp HOME
  via the autouse ``_isolate_home`` fixture) — doctor reads it read-only.
- ``run_doctor`` is invoked with ``color=False`` so assertions match plain
  ASCII markers, and with an explicit ``environ`` to avoid leaking the real
  ``ZAI_API_KEY`` into the probe.
"""

from __future__ import annotations

import json

from pytest_httpserver import HTTPServer

from zai_python_helper.doctor import (
    CheckResult,
    ProbeResult,
    render_check,
    run_doctor,
    urllib_get,
)
from zai_python_helper.paths import Paths

#: A valid Z.ai base URL (the postcondition must accept this host).
_ZAI_URL = "https://api.z.ai/api/anthropic"
#: A non-Z.ai host (the postcondition must REJECT this).
_WRONG_URL = "https://api.anthropic.com"


# --------------------------------------------------------------------------- #
# Helpers — write a settings.json env block through the injected Paths.
# --------------------------------------------------------------------------- #


def _write_settings(paths: Paths, env: dict[str, str] | None) -> None:
    """Write ``~/.claude/settings.json`` with an ``env`` block (or nothing)."""
    paths.claude_settings.parent.mkdir(parents=True, exist_ok=True)
    doc = {"env": env} if env is not None else {}
    paths.claude_settings.write_text(json.dumps(doc))


def _run(paths: Paths, *, environ: dict[str, str] | None = None, **kwargs) -> tuple[int, str]:
    """Run doctor capturing printed output; returns (exit_code, stdout)."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    kwargs.setdefault("color", False)
    with redirect_stdout(buf):
        code = run_doctor(paths, environ=environ, **kwargs)
    return code, buf.getvalue()


# --------------------------------------------------------------------------- #
# render_check unit (pure, no IO).
# --------------------------------------------------------------------------- #


def test_render_check_pass_no_hint():
    """A PASS renders one line with no Hint, plain marker when color is off."""
    out = render_check(CheckResult("x", "pass", "ok"), color=False)
    assert out == "[✓] x: ok"


def test_render_check_fail_has_hint():
    """A non-PASS appends the indented Hint line."""
    out = render_check(
        CheckResult("x", "fail", "broken", hint="fix it"), color=False
    )
    assert "[✗] x: broken" in out
    assert "Hint: fix it" in out


def test_render_check_color_wraps_marker():
    """Forced color wraps the glyph in ANSI codes."""
    out = render_check(CheckResult("x", "fail", "broken"), color=True)
    assert "\033[31m" in out  # red
    assert "\033[0m" in out  # reset


# --------------------------------------------------------------------------- #
# Postcondition: catches a wrong endpoint.
# --------------------------------------------------------------------------- #


def test_catches_wrong_endpoint(tmp_path):
    """A non-Z.ai ANTHROPIC_BASE_URL → endpoint FAIL → exit 1."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _WRONG_URL})
    code, out = _run(paths, environ={"ZAI_API_KEY": "k"})
    assert code == 1
    assert "Z.ai endpoint" in out
    assert "[✗]" in out
    assert "pointed at Anthropic" in out


def test_accepts_zai_endpoint(tmp_path):
    """An api.z.ai base URL → endpoint PASS."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})
    code, out = _run(paths, environ={})
    # No FAIL: settings ok + endpoint ok + key absent (WARN) + probe skipped
    # (WARN). Exit 0 (WARNs don't fail).
    assert code == 0
    assert "[✓] Z.ai endpoint" in out


# --------------------------------------------------------------------------- #
# settings.json missing / malformed.
# --------------------------------------------------------------------------- #


def test_missing_settings_fails(tmp_path):
    """No settings.json at all → settings FAIL → exit 1."""
    paths = Paths.from_home(tmp_path)
    code, out = _run(paths, environ={})
    assert code == 1
    assert "settings.json env block" in out
    assert "not found" in out


def test_settings_without_env_block_fails(tmp_path):
    """settings.json with no env mapping → settings FAIL."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, None)
    code, out = _run(paths, environ={})
    assert code == 1
    assert "missing or unreadable env block" in out


def test_settings_without_base_url_fails(tmp_path):
    """env block present but no ANTHROPIC_BASE_URL → settings FAIL."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"OTHER": "x"})
    code, out = _run(paths, environ={})
    assert code == 1
    assert "no ANTHROPIC_BASE_URL" in out


# --------------------------------------------------------------------------- #
# HTTP probe via the real urllib_get seam over a real socket (pytest-httpserver).
# --------------------------------------------------------------------------- #


def test_http_probe_200_ok(httpserver: HTTPServer, tmp_path):
    """A reachable endpoint returning 2xx → probe PASS, exit 0."""
    httpserver.expect_request("/").respond_with_data("ok", status=200)
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": httpserver.url_for("/")})
    code, out = _run(paths, environ={"ZAI_API_KEY": "good-key"})
    assert code == 0
    assert "[✓] HTTP probe" in out


def test_http_probe_401_bad_key(httpserver: HTTPServer, tmp_path):
    """A 401 → probe FAIL (bad key), exit 1."""
    httpserver.expect_request("/").respond_with_data("nope", status=401)
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": httpserver.url_for("/")})
    code, out = _run(paths, environ={"ZAI_API_KEY": "bad-key"})
    assert code == 1
    assert "[✗] HTTP probe" in out
    assert "key rejected" in out


def test_http_probe_403_bad_key(httpserver: HTTPServer, tmp_path):
    """A 403 → probe FAIL (key rejected), exit 1."""
    httpserver.expect_request("/").respond_with_data("forbidden", status=403)
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": httpserver.url_for("/")})
    code, out = _run(paths, environ={"ZAI_API_KEY": "bad-key"})
    assert code == 1
    assert "[✗] HTTP probe" in out


def test_http_probe_sends_configured_key(httpserver: HTTPServer, tmp_path):
    """The probe sends the resolved key in both auth header conventions."""
    httpserver.expect_request("/").respond_with_data("ok", status=200)
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": httpserver.url_for("/")})
    _run(paths, environ={"ZAI_API_KEY": "the-real-key"})
    # pytest-httpserver records every handled request — assert the probe sent
    # the key in BOTH auth header conventions Claude Code / Z.ai accept.
    assert len(httpserver.log) == 1
    sent_headers = httpserver.log[0][0].headers
    assert sent_headers.get("x-api-key") == "the-real-key"
    assert sent_headers.get("authorization") == "Bearer the-real-key"


def test_http_probe_uses_auth_token_from_settings(httpserver: HTTPServer, tmp_path):
    """ANTHROPIC_AUTH_TOKEN in the env block wins over ZAI_API_KEY env."""
    httpserver.expect_request("/").respond_with_data("ok", status=200)
    paths = Paths.from_home(tmp_path)
    _write_settings(
        paths,
        {
            "ANTHROPIC_BASE_URL": httpserver.url_for("/"),
            "ANTHROPIC_AUTH_TOKEN": "from-settings",
        },
    )
    code, out = _run(paths, environ={"ZAI_API_KEY": "from-env"})
    assert code == 0
    assert "[✓] API key present" in out


# --------------------------------------------------------------------------- #
# Graceful offline — WARN, exit 0.
# --------------------------------------------------------------------------- #


def test_offline_is_warn_not_fail(tmp_path):
    """An unreachable endpoint → probe WARN, NOT fail; exit 0.

    The base URL points at a Z.ai host (so the postcondition PASSes) but the
    ``http_get`` seam returns an offline error — emulating no network. doctor
    must degrade to WARN, not FAIL the run.
    """
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})

    def offline_se(_url, _headers):
        return ProbeResult(status=None, error="offline: connection refused")

    code, out = _run(paths, environ={"ZAI_API_KEY": "k"}, http_get=offline_se)
    assert code == 0
    assert "[!] HTTP probe" in out
    assert "offline" in out


def test_no_key_warns_and_skips_probe(tmp_path):
    """No key anywhere → key WARN + probe WARN (skipped); exit 0."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})
    code, out = _run(paths, environ={})
    assert code == 0
    assert "[!] API key present" in out
    assert "[!] HTTP probe" in out
    assert "skipped" in out


def test_http_seam_raises_is_warn(tmp_path):
    """A seam that raises (contract violation) → WARN, not a crash."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})

    def raising_se(_url, _headers):
        raise RuntimeError("boom")

    code, out = _run(paths, environ={"ZAI_API_KEY": "k"}, http_get=raising_se)
    assert code == 0
    assert "probe error" in out


# --------------------------------------------------------------------------- #
# Mixed pass+warn → exit 0; any fail → exit 1.
# --------------------------------------------------------------------------- #


def test_mixed_pass_and_warn_exits_zero(tmp_path):
    """PASS + WARN only (no FAIL) → exit 0.

    settings ok + endpoint ok + no key (WARN) + probe skipped (WARN): the
    canonical "configured but not keyed / offline" state must NOT fail.
    """
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})
    code, _ = _run(paths, environ={})
    assert code == 0


def test_one_fail_exits_one(tmp_path):
    """Any single FAIL → exit 1, even alongside PASSes/WARNs."""
    paths = Paths.from_home(tmp_path)
    # Wrong endpoint → endpoint FAIL; everything else may pass/warn.
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _WRONG_URL})
    code, _ = _run(paths, environ={"ZAI_API_KEY": "k"})
    assert code == 1


# --------------------------------------------------------------------------- #
# Shell override WARN (ADR-003).
# --------------------------------------------------------------------------- #


def test_shell_export_warns(tmp_path):
    """An ``export ANTHROPIC_*`` in ~/.zshrc → WARN (override risk), exit 0."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})
    paths.zshrc.write_text("export ANTHROPIC_BASE_URL=https://example.com\n")
    code, out = _run(paths, environ={})
    assert code == 0
    assert "shell env override" in out
    assert "[!]" in out


def test_no_zshrc_no_shell_check(tmp_path):
    """No ~/.zshrc → no shell-override check emitted at all."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _ZAI_URL})
    code, out = _run(paths, environ={})
    assert code == 0
    assert "shell env override" not in out


# --------------------------------------------------------------------------- #
# urllib_get seam directly (offline path, real network attempt).
# --------------------------------------------------------------------------- #


def test_urllib_get_offline_is_error_result():
    """``urllib_get`` against a dead port → ProbeResult(error=...), no raise."""
    # A port that is almost certainly closed. The seam must NOT raise and must
    # report an offline-style error (WARN upstream), not a status.
    probe = urllib_get("http://127.0.0.1:1/", {})
    assert probe.status is None
    assert probe.error is not None


def test_urllib_get_never_raises_on_bad_url():
    """A garbage URL → ProbeResult(error=...), never an exception."""
    probe = urllib_get("not a url at all", {})
    assert probe.error is not None
    assert probe.status is None
