"""Tests for the ``doctor`` diagnostic pipeline (S5, issue #6).

The HTTP probe is exercised through a real-socket test seam: a fake
``http_get`` wired to a pytest-httpserver instance (no monkeypatched network)
so the 200-OK / 401-bad-key / 429-degraded / offline acceptance criteria are
tested end-to-end. The production seam (:func:`urllib_post`) is itself tested
for its security posture (HTTPS-only, no redirect following, never raises).

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
    urllib_post,
)
from zai_python_helper.paths import Paths

#: The auth-enforcing probe path doctor appends to the base URL.
_PROBE_PATH = "/v1/messages"

#: A valid Z.ai base URL (the postcondition must accept this host).
_ZAI_URL = "https://api.z.ai/api/anthropic"
#: A non-Z.ai host (the postcondition must REJECT this).
_WRONG_URL = "https://api.anthropic.com"


def _httpserver_seam(httpserver: HTTPServer):
    """Build a real-socket probe seam pointing at a pytest-httpserver instance.

    Returns ``(seam, base_url, host)``: the seam POSTs whatever doctor hands it
    to the live httpserver (a real local socket, no network), ``base_url`` is
    the value to write into settings.json so doctor's probe URL
    (``base + /v1/messages``) lands on the configured handler, and ``host`` is
    the httpserver host to pass as ``extra_zai_hosts`` so the postcondition
    PASSes and the credentialed probe actually runs (production trusts only the
    canonical Z.ai hosts; tests must explicitly vouch for the httpserver host).

    The test seam is transport-agnostic (it does not enforce HTTPS — that is
    the production seam's job, tested separately in the ``urllib_post`` block),
    so the probe-acceptance tests can exercise a real socket over plain HTTP.
    It records every call on ``seam.calls`` so tests can assert headers/body.
    """
    base_url = httpserver.url_for("").rstrip("/")
    host = httpserver.host  # trusted as a Z.ai origin for this test only
    calls: list[tuple[str, dict[str, str], str]] = []
    import urllib.error
    import urllib.request

    def seam(url: str, headers: dict[str, str], body: str) -> ProbeResult:
        calls.append((url, headers, body))
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return ProbeResult(status=resp.status, error=None)
        except urllib.error.HTTPError as e:
            return ProbeResult(status=e.code, error=None)
        except Exception as e:  # noqa: BLE001 — seam reports, never raises.
            return ProbeResult(status=None, error=f"offline: {e}")

    seam.calls = calls  # type: ignore[attr-defined]
    return seam, base_url, host


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
# HTTP probe via a real-socket test seam over pytest-httpserver.
# The probe targets {base}/v1/messages (the auth-enforcing endpoint).
# --------------------------------------------------------------------------- #


def _probe_setup(httpserver, status: int, tmp_path):
    """Configure httpserver to answer the probe path.

    Returns ``(paths, run_kwargs)`` where run_kwargs carries the seam and the
    httpserver host as extra_zai_hosts so the postcondition PASSes and the
    credentialed probe runs against the real local socket.
    """
    httpserver.expect_request(_PROBE_PATH, method="POST").respond_with_data(
        "ok", status=status
    )
    paths = Paths.from_home(tmp_path)
    seam, base, host = _httpserver_seam(httpserver)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": base})
    return paths, {"http_get": seam, "extra_zai_hosts": frozenset({host})}


def test_http_probe_200_ok(httpserver: HTTPServer, tmp_path):
    """A reachable auth-enforcing endpoint returning 2xx → probe PASS, exit 0."""
    paths, kw = _probe_setup(httpserver, 200, tmp_path)
    code, out = _run(paths, environ={"ZAI_API_KEY": "good-key"}, **kw)
    assert code == 0
    assert "[✓] HTTP probe" in out


def test_http_probe_401_bad_key(httpserver: HTTPServer, tmp_path):
    """A 401 from the auth-enforcing endpoint → probe FAIL (bad key), exit 1.

    Regression for the Codex finding: a bare GET of the base URL returns 200
    even for a bad key, so doctor MUST probe /v1/messages (the only path that
    401s a bad key) to actually catch an invalid credential.
    """
    paths, kw = _probe_setup(httpserver, 401, tmp_path)
    code, out = _run(paths, environ={"ZAI_API_KEY": "bad-key"}, **kw)
    assert code == 1
    assert "[✗] HTTP probe" in out
    assert "key rejected" in out


def test_http_probe_403_bad_key(httpserver: HTTPServer, tmp_path):
    """A 403 → probe FAIL (key rejected), exit 1."""
    paths, kw = _probe_setup(httpserver, 403, tmp_path)
    code, out = _run(paths, environ={"ZAI_API_KEY": "bad-key"}, **kw)
    assert code == 1
    assert "[✗] HTTP probe" in out


def test_http_probe_429_degraded_is_warn(httpserver: HTTPServer, tmp_path):
    """A 429 → probe WARN (degraded), NOT pass — doctor never claims a
    rate-limited endpoint is healthy; exit 0 (WARN doesn't fail)."""
    paths, kw = _probe_setup(httpserver, 429, tmp_path)
    code, out = _run(paths, environ={"ZAI_API_KEY": "k"}, **kw)
    assert code == 0
    assert "[!] HTTP probe" in out
    assert "degraded" in out


def test_http_probe_500_degraded_is_warn(httpserver: HTTPServer, tmp_path):
    """A 5xx → probe WARN (degraded), NOT pass; exit 0."""
    paths, kw = _probe_setup(httpserver, 503, tmp_path)
    code, out = _run(paths, environ={"ZAI_API_KEY": "k"}, **kw)
    assert code == 0
    assert "[!] HTTP probe" in out
    assert "degraded" in out


def test_http_probe_404_unverified_is_warn(httpserver: HTTPServer, tmp_path):
    """An unexpected 4xx → probe WARN (unverified), NOT pass; exit 0."""
    paths, kw = _probe_setup(httpserver, 404, tmp_path)
    code, out = _run(paths, environ={"ZAI_API_KEY": "k"}, **kw)
    assert code == 0
    assert "[!] HTTP probe" in out
    assert "unverified" in out


def test_http_probe_sends_key_in_both_headers(httpserver: HTTPServer, tmp_path):
    """The probe sends the resolved key in BOTH auth header conventions and
    targets /v1/messages (the auth-enforcing endpoint, not the base URL)."""
    paths, kw = _probe_setup(httpserver, 200, tmp_path)
    seam = kw["http_get"]
    _run(paths, environ={"ZAI_API_KEY": "the-real-key"}, **kw)
    assert len(seam.calls) == 1
    url, headers, body = seam.calls[0]
    # Probe URL is the auth-enforcing messages endpoint, not the base URL.
    assert url.endswith(_PROBE_PATH)
    assert headers["x-api-key"] == "the-real-key"
    assert headers["authorization"] == "Bearer the-real-key"
    assert "glm-4.5-flash" in body  # the minimal auth-enforcing payload


def test_http_probe_uses_auth_token_from_settings(httpserver: HTTPServer, tmp_path):
    """ANTHROPIC_AUTH_TOKEN in the env block wins over ZAI_API_KEY env."""
    paths, kw = _probe_setup(httpserver, 200, tmp_path)
    seam = kw["http_get"]
    # Re-write settings to also carry ANTHROPIC_AUTH_TOKEN (base URL unchanged).
    base = httpserver.url_for("").rstrip("/")
    _write_settings(paths, {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": "from-settings"})
    code, out = _run(paths, environ={"ZAI_API_KEY": "from-env"}, **kw)
    assert code == 0
    assert "[✓] API key present" in out
    assert seam.calls[0][1]["x-api-key"] == "from-settings"


# --------------------------------------------------------------------------- #
# Credential-egress gate: probe is SKIPPED for a FAILED endpoint, never sending
# the key to a provably-wrong target (Codex critical finding #1).
# --------------------------------------------------------------------------- #


def test_probe_skipped_when_endpoint_fails_no_key_sent(tmp_path):
    """A FAILED endpoint (pointed at Anthropic) → probe SKIPPED, and the key
    is NEVER transmitted. Regression for credential-disclosure finding."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": _WRONG_URL})

    def seam_must_not_be_called(_url, _headers, _body):
        raise AssertionError("probe must NOT run for a failed endpoint")

    code, out = _run(
        paths, environ={"ZAI_API_KEY": "secret"}, http_get=seam_must_not_be_called
    )
    assert code == 1  # the endpoint FAIL fails the run
    assert "[!] HTTP probe" in out
    assert "skipped" in out
    assert "not verified as Z.ai" in out


def test_probe_skipped_when_no_base_url_no_key_sent(tmp_path):
    """No base URL at all → endpoint FAIL → probe skipped, no key sent."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"OTHER": "x"})  # no ANTHROPIC_BASE_URL → settings FAIL

    def seam_must_not_be_called(_url, _headers, _body):
        raise AssertionError("probe must NOT run with no base URL")

    code, out = _run(
        paths, environ={"ZAI_API_KEY": "secret"}, http_get=seam_must_not_be_called
    )
    assert code == 1
    assert "skipped" in out


def test_probe_skipped_for_unrecognized_host_no_key_sent(tmp_path):
    """An UNRECOGNIZED host (WARN endpoint, e.g. attacker.example) → probe
    SKIPPED, key NEVER sent. Regression for the round-2 Codex finding: the
    gate must be PASS-only, not just "not FAIL", or an attacker controlling
    ANTHROPIC_BASE_URL harvests the API key."""
    paths = Paths.from_home(tmp_path)
    _write_settings(paths, {"ANTHROPIC_BASE_URL": "https://attacker.example/api/anthropic"})

    def seam_must_not_be_called(_url, _headers, _body):
        raise AssertionError("probe must NOT run for an unrecognized host")

    code, out = _run(
        paths, environ={"ZAI_API_KEY": "secret"}, http_get=seam_must_not_be_called
    )
    # Unrecognized host is a WARN, not a FAIL — the run exits 0, but the probe
    # was still skipped (no key sent).
    assert code == 0
    assert "[!] Z.ai endpoint" in out
    assert "[!] HTTP probe" in out
    assert "skipped" in out


# --------------------------------------------------------------------------- #
# Production-seam redirect rejection — a 3xx must NOT be followed (Codex round-2).
# --------------------------------------------------------------------------- #


def test_no_redirect_handler_blocks_redirects():
    """The redirect-rejecting opener must NOT follow a 3xx.

    The production seam installs ``_NoRedirectHandler`` (subclass overriding
    ``redirect_request`` to return None) because ``build_opener`` always adds
    the default ``HTTPRedirectHandler`` otherwise. With the override, a 3xx is
    NOT followed: urllib raises ``HTTPError(302)`` (caught by the production
    seam and surfaced as ``ProbeResult(status=302)`` → WARN), so the credential
    headers never reach the redirect target. This test registers the handler on
    a real opener against a local httpserver that 302-redirects to /capture and
    asserts the capture route never receives a request.
    """
    import urllib.error
    import urllib.request

    from pytest_httpserver import HTTPServer

    from zai_python_helper.doctor import _NoRedirectHandler

    httpserver = HTTPServer()
    httpserver.start()
    try:
        captured: list = []
        base = httpserver.url_for("").rstrip("/")
        httpserver.expect_request("/redirect", method="POST").respond_with_data(
            "", status=302, headers={"Location": base + "/capture"}
        )

        def captor(req):
            captured.append(req.headers.get("x-api-key"))
            from werkzeug.wrappers import Response

            return Response("captured", status=200)

        httpserver.expect_request("/capture", method="POST").respond_with_handler(captor)

        opener = urllib.request.build_opener(_NoRedirectHandler)
        req = urllib.request.Request(base + "/redirect", data=b"{}", method="POST")
        # The handler returns None → urllib raises HTTPError(302) instead of
        # following. (A default opener would follow to /capture, hit the captor,
        # and return 200 — which would fail the HTTPError assertion.)
        try:
            opener.open(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 302
        assert raised, "expected HTTPError(302) — redirect was followed instead"
        assert captured == [], "redirect was followed — credential would leak!"
    finally:
        httpserver.clear()
        httpserver.stop()


def test_default_opener_DOES_follow_redirects_guard():
    """Guard proving the fix is necessary: a DEFAULT opener (no
    ``_NoRedirectHandler``) DOES follow a 302 — which is exactly the credential
    exfil path the production seam must avoid. If this test ever fails (default
    opener stops following), the ``_NoRedirectHandler`` override may be dead."""
    import urllib.request

    from pytest_httpserver import HTTPServer

    httpserver = HTTPServer()
    httpserver.start()
    try:
        captured: list = []
        base = httpserver.url_for("").rstrip("/")
        httpserver.expect_request("/redirect", method="POST").respond_with_data(
            "", status=302, headers={"Location": base + "/capture"}
        )

        def captor(req):
            captured.append(req.headers.get("x-api-key"))
            from werkzeug.wrappers import Response

            return Response("captured", status=200)

        # Match ANY method on /capture: urllib's default opener converts a 302
        # POST → GET (another exfil vector), so the followed request arrives as
        # GET, not POST.
        httpserver.expect_request("/capture").respond_with_handler(captor)
        # The DEFAULT opener follows the redirect → /capture receives the
        # request (credential would leak). This is the baseline the fix removes.
        resp = urllib.request.urlopen(
            urllib.request.Request(base + "/redirect", data=b"{}", method="POST"),
            timeout=5,
        )
        assert resp.status == 200
        assert captured, "default opener did NOT follow — _NoRedirectHandler guard is stale"
    finally:
        httpserver.clear()
        httpserver.stop()


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

    def offline_se(_url, _headers, _body):
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

    def raising_se(_url, _headers, _body):
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
# Production seam (urllib_post) security posture — tested directly.
# --------------------------------------------------------------------------- #


def test_urllib_post_refuses_non_https():
    """A plain-http probe URL → refused (no status), key never sent.

    Regression for credential-egress: the production seam must not POST the
    credential over an unencrypted transport.
    """
    probe = urllib_post("http://api.z.ai/api/anthropic" + _PROBE_PATH, {"x-api-key": "k"}, "{}")
    assert probe.status is None
    assert "non-https" in (probe.error or "")


def test_urllib_post_offline_is_error_result():
    """``urllib_post`` against a dead port → ProbeResult(error=...), no raise."""
    probe = urllib_post("https://127.0.0.1:1" + _PROBE_PATH, {}, "{}")
    assert probe.status is None
    assert probe.error is not None


def test_urllib_post_never_raises_on_bad_url():
    """A garbage URL → ProbeResult(error=...), never an exception."""
    probe = urllib_post("not a url at all", {}, "{}")
    assert probe.error is not None
    assert probe.status is None
