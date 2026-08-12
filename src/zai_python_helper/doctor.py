"""READ-ONLY diagnostic pipeline (S5, issue #6).

Verifies that the configured Claude Code ⇄ Z.ai chain actually works,
link-by-link, and prints a verdict per check (``[✓]`` / ``[!]`` / ``[✗]``).
Exits ``0`` unless at least one check FAILs; WARNs alone → exit ``0``.
Offline / timeout → WARN, never FAIL — the doctor must be runnable with
no network, and one tool being down must not abort the rest of the run.

Per tool (Claude Code first — the v1 front door), the chain is:

  1. **settings.json env block** — present + carries ``ANTHROPIC_BASE_URL``
     (the postcondition source). Missing/unreadable → FAIL.
  2. **Z.ai endpoint postcondition** — ``ANTHROPIC_BASE_URL`` host matches a
     known Z.ai region host (``api.z.ai`` / ``api.zai.cn``). A non-Z.ai host
     (e.g. left pointing at the real Anthropic, or a typo) → FAIL: the chain
     is misconfigured and the HTTP probe would be meaningless.
  3. **API key present** — ``ANTHROPIC_AUTH_TOKEN`` in the env block or
     ``ZAI_API_KEY`` in the environment. Missing → WARN (no point probing,
     but absence is not a broken link — the user may key interactively).
  4. **HTTP probe** — a ``POST`` to ``{base_url}/v1/messages`` (the only
     auth-enforcing Z.ai endpoint — a bare GET of the base URL returns 200
     even with a bad key, so it cannot detect one) with the resolved key and a
     minimal 1-token request. The probe is SKIPPED when the postcondition
     FAILED (the endpoint is provably wrong — e.g. still pointed at the real
     Anthropic — so we never send the key there). ``401``/``403`` → FAIL
     (bad key); ``2xx`` → PASS; ``429``/``5xx`` → WARN (degraded, not PASS);
     offline/timeout → WARN (graceful). Redirects are disabled and only HTTPS
     is probed, so the key is not leaked across origins or downgraded to HTTP.

**MCP probe** (S7): for each Z.ai preset MCP and each cross-tool target, the
probe reads the tool's MCP config (READ-ONLY) and reports which preset MCPs are
installed. An installed preset is a PASS; an UN-installed one is a PASS too
(installing a preset is an explicit opt-in, never a broken link) — doctor never
FAILs on an absent MCP. The probe does NOT stdio-launch ``npx -y @z_ai/mcp-server``
nor ping the http MCP endpoints: those would require network/a subprocess and
would break the "doctor runs offline" contract. The check is purely a config
read, so it stays cheap, offline-safe, and never fails doctor.

READ-ONLY CONTRACT: doctor performs zero writes and no mutation of any
config file. It reads ``settings.json`` and the environment through the
injected :class:`Paths` and ``environ``. The only network call is the HTTP
probe, issued through the injectable ``http_get`` seam (a test seam for
pytest-httpserver: production uses :mod:`urllib` from the stdlib — no
``httpx`` dependency). The MCP probe does no network at all.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from zai_python_helper import regions
from zai_python_helper.io.settings import resolve_effective_env
from zai_python_helper.paths import Paths

__all__ = ["CheckResult", "HttpProbe", "ProbeResult", "render_check", "run_doctor"]


def _host_of(url: str) -> str:
    """Extract the lowercase host (network location) of ``url``.

    Used by the postcondition check: compare the configured endpoint's host
    against the known Z.ai region hosts, ignoring scheme/path. Falls back to
    the raw string lowercased if parsing fails (the check then simply won't
    match a known Z.ai host, which is the correct outcome for a bad URL).
    """
    return (urlparse(url).hostname or url).lower()


#: Hard socket + read timeout (seconds) for the HTTP probe. Offline / slow
#: upstream must fail FAST and degrade to a WARN, not hang doctor. Splitting
#: connect vs read is not exposed by urllib's single ``timeout`` (it is both),
#: so one ceiling bounds the whole request.
_HTTP_TIMEOUT = 5.0

#: HTTP status codes that mean "the key was rejected" — a bad credential is a
#: broken link, so the probe FAILs on these.
_AUTH_REJECT_CODES = {401, 403}

#: HTTP status codes that mean "the endpoint is up but degraded right now"
#: (rate-limited, upstream error). NOT a credential problem, NOT healthy →
#: WARN, never PASS (so doctor never claims a degraded endpoint is working)
#: and never FAIL (the key may be fine; retry later).
_DEGRADED_CODES = {429, 500, 502, 503, 504}

#: The auth-enforcing probe path appended to the configured base URL. The
#: Claude-compatible Z.ai gateway only authenticates on the messages endpoint
#: — a bare GET of the base URL returns 200 even with no/invalid key (verified
#: live), so it cannot detect a bad credential. ``/v1/messages`` returns 401
#: for a bad key and 2xx for a valid one (verified live, model glm-4.5-flash),
#: so it is the real auth gate. The payload is a minimal 1-token request to
#: keep the billable cost negligible.
_PROBE_PATH = "/v1/messages"
_PROBE_PAYLOAD = '{"model":"glm-4.5-flash","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}'


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect-rejecting handler: a 3xx must NOT be followed.

    ``urllib.request.build_opener`` ALWAYS installs the default
    :class:`HTTPRedirectHandler` even when other handlers are passed, so simply
    omitting it does not disable redirect following (verified live). Subclassing
    it and returning ``None`` from :meth:`redirect_request` makes the opener
    surface a 3xx as the response status instead of following it — so a
    redirect cannot carry the credential-bearing headers across origins or
    downgrade HTTPS → HTTP. Installed via ``build_opener(_NoRedirectHandler)``;
    the subclass OVERRIDES the default because build_opener keeps at most one
    handler per type.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None

#: Known Z.ai region hosts, DERIVED from the canonical region→endpoint map in
#: :mod:`zai_python_helper.regions` (single source of truth — the planner and
#: the CLI read the same map; doctor must not hand-list hosts that can drift).
#: ``ANTHROPIC_BASE_URL`` pointing at one of these is a definitive PASS for the
#: postcondition — the Claude Code client is sending traffic to Z.ai.
_ZAI_HOSTS: frozenset[str] = frozenset(
    _host_of(url) for url in regions.ZAI_ANTHROPIC_BASE_URL_BY_REGION.values()
)

#: Hosts that mean the client is still pointed at the REAL Anthropic endpoint
#: (not Z.ai) — the postcondition FAILs on these. This is the "didn't switch to
#: Z.ai" failure the issue acceptance criteria name. There is no canonical
#: region map for these (regions.py only owns the Z.ai side), so they are
#: listed here. ``localhost`` and other unrecognized hosts are NOT here: a
#: custom/staging Z.ai deployment, or a test httpserver, is a WARN
#: ("unrecognized — verify it is Z.ai"), not a FAIL, so the HTTP probe still
#: runs and the doctor stays useful.
_ANTHROPIC_HOSTS: frozenset[str] = frozenset({"api.anthropic.com", "api.anthropic.cn"})

#: Default environment variable consulted for the API key when the settings
#: env block does not carry ``ANTHROPIC_AUTH_TOKEN``.
_KEY_ENV_VAR = "ZAI_API_KEY"


# --------------------------------------------------------------------------- #
# ANSI color helpers — manual, no Rich (parity with sibling doctor).
# --------------------------------------------------------------------------- #

#: ANSI escape sequences for the three verdict colors. Plain ASCII markers
#: (no escape) when color is disabled so logs/captured output stay readable.
_ANSI_COLOR = {"pass": "\033[32m", "warn": "\033[33m", "fail": "\033[31m"}
_ANSI_RESET = "\033[0m"

#: The verdict glyphs. Unicode (✓/!/✗) per the issue spec; the ANSI-wrapping
#: in :func:`_marker` is what toggles color, not a second glyph set.
_MARKERS = {"pass": "[✓]", "warn": "[!]", "fail": "[✗]"}


def _marker(verdict: str, *, color: bool) -> str:
    """Return the rendered marker for ``verdict`` (``pass``/``warn``/``fail``).

    When ``color`` is True the glyph is wrapped in the verdict's ANSI color;
    otherwise it is plain. The glyphs are Unicode but ASCII-safe-adjacent so
    captured/piped output stays readable.
    """
    glyph = _MARKERS[verdict]
    if not color:
        return glyph
    return f"{_ANSI_COLOR[verdict]}{glyph}{_ANSI_RESET}"


def render_check(result: CheckResult, *, color: bool | None = None) -> str:
    """Render a :class:`CheckResult` as one or two lines.

    Line 1: ``<marker> <name>: <detail>``.
    Line 2 (only when ``verdict != "pass"``): an indented ``    Hint: <hint>``.

    Args:
        result: The :class:`CheckResult` to render.
        color: ``True`` forces colored markers; ``False`` forces plain; ``None``
            (default) auto-detects from :func:`sys.stdout.isatty`.
    """
    use_color = color if color is not None else sys.stdout.isatty()
    marker = _marker(result.verdict, color=use_color)
    line = f"{marker} {result.name}: {result.detail}"
    if result.verdict != "pass" and result.hint:
        line += f"\n    Hint: {result.hint}"
    return line


# --------------------------------------------------------------------------- #
# CheckResult + the HTTP probe result type.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    """The outcome of a single doctor check.

    A pure value object: each check in the chain produces one. ``verdict`` is
    one of ``"pass"`` / ``"warn"`` / ``"fail"``:

    - ``pass`` — the link is healthy (marker ``[✓]``).
    - ``warn`` — non-fatal but worth surfacing (marker ``[!]``). E.g. offline,
      no key configured, a shell export that may override settings.json. A WARN
      alone does NOT fail doctor.
    - ``fail`` — the link is broken (marker ``[✗]``). Any FAIL → doctor exit 1.

    Fields:
        name: human-readable check name.
        verdict: ``"pass"`` / ``"warn"`` / ``"fail"``.
        detail: the observed state, one short phrase.
        hint: actionable ``Hint:`` text. Empty for a ``pass``.
    """

    name: str
    verdict: Literal["pass", "warn", "fail"]
    detail: str
    hint: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single HTTP probe — the ``http_get`` seam return type.

    Carries the status code when the server responded, or an ``error`` tag
    when the request never completed (offline / timeout / DNS). Doctor maps
    these to a :class:`CheckResult` verdict — the seam itself stays neutral.

    Attributes:
        status: the HTTP status code, or ``None`` if the request errored.
        error: a short human-readable error tag (e.g. ``"offline"``), or
            ``None`` when a response was received.
    """

    status: int | None
    error: str | None


#: The injectable HTTP seam. Production wires :func:`urllib_post`; tests wire a
#: fake that points at a pytest-httpserver instance (real socket, no network).
#: The callable POSTs ``body`` to ``url`` with ``headers`` and returns a
#: :class:`ProbeResult`. It MUST NOT raise — doctor reports, never raises.
HttpProbe = Callable[[str, dict[str, str], str], ProbeResult]


def urllib_post(url: str, headers: dict[str, str], body: str) -> ProbeResult:
    """Default ``http_get`` seam: a stdlib ``urllib`` POST with a hard timeout.

    Used in production. Returns a :class:`ProbeResult` — never raises.

    Security (credential-egress) posture, distinct from the old GET probe:

    - **HTTPS only.** A plain-``http`` probe URL is refused up front
      (``error="refused: non-https endpoint"``) — the key must never travel in
      the clear. The check maps this to a WARN (the user configured an
      insecure transport; not a broken link, but not probed).
    - **No redirect following.** A custom opener WITHOUT ``HTTPRedirectHandler``
      is used, so a 3xx cannot carry the credential-bearing headers across
      origins or downgrade HTTPS → HTTP. A 3xx is surfaced as the redirect
      status itself (mapped to WARN below), not silently followed.

    A ``URLError`` whose reason is a socket error (connection refused / DNS /
    unreachable) is reported as ``"offline"`` (WARN); a non-auth HTTP error
    (``HTTPError``) carries its status; everything else is reported as
    ``"error"``. ``status`` and ``error`` are kept mutually exclusive so the
    caller can branch on ``error is not None`` first.
    """
    if not url.lower().startswith("https://"):
        return ProbeResult(status=None, error="refused: non-https endpoint")
    try:
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        # _NoRedirectHandler overrides the default HTTPRedirectHandler that
        # build_opener would otherwise install: a 3xx must NOT be followed, so
        # the credential-bearing headers cannot be carried across origins or
        # downgraded HTTPS → HTTP.
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
            return ProbeResult(status=resp.status, error=None)
    except urllib.error.HTTPError as e:
        # A non-2xx HTTP response: the server was reached and answered. Doctor
        # decides the verdict from the status (only 401/403 fail; 429/5xx warn).
        return ProbeResult(status=e.code, error=None)
    except (TimeoutError, ConnectionError) as e:
        return ProbeResult(status=None, error=f"offline: {e}")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        # socket.gaierror (DNS) and ConnectionRefusedError nest under URLError
        # — offline either way; the host is not reachable right now.
        return ProbeResult(status=None, error=f"offline: {reason}")
    except Exception as e:  # noqa: BLE001 — doctor reports, never raises.
        return ProbeResult(status=None, error=f"error: {e}")


# --------------------------------------------------------------------------- #
# Config readers (READ-ONLY, via Paths + plain json — no backend dependency).
# --------------------------------------------------------------------------- #


def _read_settings_env(paths: Paths) -> dict[str, str] | None:
    """READ-ONLY: resolve the EFFECTIVE env block from all Claude settings.

    Uses the precedence resolver (issue #23) to merge env blocks across
    managed > local > project > user scopes. Returns the effective env dict
    if any settings file exists and has an env block; returns None otherwise.

    The two "absent" cases are deliberately conflated: doctor's first check
    reports "no env block" either way, and downstream checks key off None.

    Any read/parse error is folded to None — doctor treats an unreadable
    settings.json the same as a missing one at this layer (the check result
    will surface the reason). Uses JsonBackend through the resolver (S2 layer),
    per the security fix requirement.

    READ-ONLY: performs zero writes; only reads through the resolver.
    """
    try:
        # Extract home from claude_settings (~/home/.claude/settings.json -> ~/home)
        home = paths.claude_settings.parent.parent.parent
        return resolve_effective_env(
            user_settings_path=paths.claude_settings,
            project_settings_path=paths.project_claude_settings,
            local_settings_path=paths.local_claude_settings,
            cwd=paths.cwd,
            home=home,
        )
    except Exception:
        # Fold any read/parse error to None — doctor reports, never raises.
        return None


# --------------------------------------------------------------------------- #
# The checks — each returns a CheckResult, never raises.
# --------------------------------------------------------------------------- #


def _check_settings_env(paths: Paths) -> tuple[CheckResult, dict[str, str] | None]:
    """settings.json carries an ``env`` block (the postcondition source).

    Returns the check result AND the parsed env block (for downstream checks)
    so the file is read exactly once. A missing/unreadable file or a missing
    env block is a FAIL — without settings there is no configured chain.
    """
    name = "settings.json env block"
    if not paths.claude_settings.exists():
        result = CheckResult(
            name=name,
            verdict="fail",
            detail=f"not found at {paths.claude_settings}",
            hint="run `zai-python-helper use zai` to configure Claude Code",
        )
        return result, None
    env = _read_settings_env(paths)
    if env is None:
        result = CheckResult(
            name=name,
            verdict="fail",
            detail="missing or unreadable env block",
            hint="settings.json has no `env` mapping or failed to parse",
        )
        return result, None
    if "ANTHROPIC_BASE_URL" not in env:
        result = CheckResult(
            name=name,
            verdict="fail",
            detail="env block has no ANTHROPIC_BASE_URL",
            hint="run `zai-python-helper use zai` to set the Z.ai endpoint",
        )
        return result, env
    return (
        CheckResult(name=name, verdict="pass", detail="present", hint=""),
        env,
    )


def _check_zai_endpoint(
    env: dict[str, str] | None, extra_hosts: frozenset[str] | None = None
) -> CheckResult:
    """ANTHROPIC_BASE_URL points at a Z.ai endpoint (the postcondition).

    This is the postcondition check named in the issue. Three outcomes:

    - a known Z.ai host (``api.z.ai`` / ``api.zai.cn``) → PASS;
    - the real Anthropic host (``api.anthropic.com`` / ``.cn``) → FAIL — the
      client never switched to Z.ai (the acceptance criterion: "catches a
      wrong endpoint");
    - any other host (custom/staging deployment, a typo, a test httpserver) →
      WARN. doctor cannot prove it is Z.ai, so the credentialed probe is NOT
      run against it (see :func:`_check_http_probe`'s PASS-only gate). The
      caller may pass ``extra_hosts`` to treat additional user-confirmed Z.ai
      origins (e.g. a staging deployment) as PASS — production does not, so an
      unrecognized host never silently receives the key.

    Args:
        env: the parsed settings.json env block (``None`` if unreadable).
        extra_hosts: optional extra hosts the caller vouches are Z.ai (PASS).
            Used by tests to point the probe at a local httpserver; production
            passes ``None`` so only the canonical region hosts PASS.
    """
    name = "Z.ai endpoint"
    if env is None:
        return CheckResult(name=name, verdict="fail", detail="no base URL", hint="")
    base_url = env.get("ANTHROPIC_BASE_URL", "")
    host = _host_of(base_url)
    trusted = _ZAI_HOSTS | (extra_hosts or frozenset())
    if host in trusted:
        return CheckResult(name=name, verdict="pass", detail=f"{base_url}", hint="")
    if host in _ANTHROPIC_HOSTS:
        return CheckResult(
            name=name,
            verdict="fail",
            detail=f"pointed at Anthropic ({host}), not Z.ai",
            hint="run `zai-python-helper use zai` to switch to the Z.ai endpoint",
        )
    return CheckResult(
        name=name,
        verdict="warn",
        detail=f"unrecognized host ({host}) — verify it is Z.ai",
        hint="a non-standard endpoint; confirm it is your Z.ai deployment",
    )


def _check_key_present(
    env: dict[str, str] | None, environ: Mapping[str, str]
) -> tuple[CheckResult, str | None]:
    """An API key is available for the probe (WARN, not FAIL, if absent).

    Returns the check result AND the resolved key (for the HTTP probe). A
    missing key is a WARN: the chain may be correctly configured for an
    interactive-keyed session, and we simply skip the network probe rather
    than failing.
    """
    name = "API key present"
    # Resolve the probe key exactly as Claude Code authenticates:
    # ANTHROPIC_AUTH_TOKEN in the settings.json env block first, then
    # ZAI_API_KEY in the environment. None if neither is set (WARN above).
    token = env.get("ANTHROPIC_AUTH_TOKEN") if env else None
    key = token or environ.get(_KEY_ENV_VAR)
    if key:
        return (
            CheckResult(name=name, verdict="pass", detail="present", hint=""),
            key,
        )
    return (
        CheckResult(
            name=name,
            verdict="warn",
            detail="no ANTHROPIC_AUTH_TOKEN / ZAI_API_KEY",
            hint="set ZAI_API_KEY (or ANTHROPIC_AUTH_TOKEN) to probe the endpoint",
        ),
        None,
    )


def _check_http_probe(
    base_url: str | None,
    key: str | None,
    endpoint_verdict: str,
    http_get: HttpProbe,
) -> CheckResult:
    """HTTP probe: POST the auth-enforcing endpoint with the resolved key.

    Credential-egress gate: the API key is sent ONLY when ``endpoint_verdict``
    is ``"pass"`` (a canonical Z.ai origin). A ``"fail"`` (provably wrong target
    — e.g. still pointed at the real Anthropic, or no base URL) AND a ``"warn"``
    (unrecognized host — e.g. a typo'd or attacker-controlled domain) both SKIP
    the probe. This is the root fix for credential disclosure: doctor never
    attaches stored credentials to a URL it has not verified as Z.ai.

    Status mapping:

    - ``401``/``403`` → FAIL (bad key) — the one definitive credential failure.
    - ``2xx`` → PASS — the key was accepted (the gateway processed the request).
    - ``429``/``5xx`` → WARN (degraded) — up but overloaded; NOT PASS (doctor
      never claims a degraded endpoint is working) and NOT FAIL (the key may be
      fine; retry later).
    - any other 4xx → WARN (unverified) — the gateway responded in a way doctor
      can't interpret as success or auth failure.
    - offline / timeout / non-https → WARN (graceful — offline is never a FAIL).

    No key, no base URL, or a non-PASS endpoint → WARN (skipped), not a request.
    """
    name = "HTTP probe"
    # Credential-egress gate: the API key is sent ONLY to an endpoint the
    # postcondition PASSed (a canonical Z.ai origin). A FAIL (provably wrong
    # target — e.g. still pointed at Anthropic) OR a WARN (unrecognized host —
    # e.g. a typo'd attacker domain) both SKIP the probe: doctor must never
    # attach stored credentials to a URL it has not verified as Z.ai. This is
    # the root fix for the credential-disclosure finding — gating on PASS
    # alone, not "not FAIL", closes the unrecognized-host exfil path.
    if endpoint_verdict != "pass":
        return CheckResult(
            name=name,
            verdict="warn",
            detail="skipped (endpoint not verified as Z.ai — key not sent)",
            hint="fix the Z.ai endpoint check first; doctor only probes a verified Z.ai origin",
        )
    if not base_url or not key:
        return CheckResult(
            name=name,
            verdict="warn",
            detail="skipped (no endpoint or key)",
            hint="resolve the upstream checks first",
        )
    # The probe URL is the auth-enforcing messages endpoint, NOT the base URL:
    # a bare GET of the base returns 200 even for a bad key (verified live), so
    # only /v1/messages discriminates valid vs invalid credentials.
    probe_url = base_url.rstrip("/") + _PROBE_PATH
    headers = {
        "x-api-key": key,
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    try:
        probe = http_get(probe_url, headers, _PROBE_PAYLOAD)
    except Exception as e:  # noqa: BLE001 — doctor reports, never raises.
        return CheckResult(
            name=name,
            verdict="warn",
            detail=f"probe error: {e}",
            hint="the HTTP seam raised; check the endpoint and retry",
        )
    if probe.error is not None:
        return CheckResult(
            name=name,
            verdict="warn",
            detail=probe.error,
            hint="endpoint unreachable, timed out, or non-https (offline is not a failure)",
        )
    status = probe.status
    if status in _AUTH_REJECT_CODES:
        return CheckResult(
            name=name,
            verdict="fail",
            detail=f"{status} (key rejected)",
            hint="the API key is invalid/expired; set a valid ZAI_API_KEY",
        )
    if status in _DEGRADED_CODES:
        return CheckResult(
            name=name,
            verdict="warn",
            detail=f"{status} (endpoint degraded)",
            hint="the endpoint is rate-limited or erroring; the key may be fine — retry later",
        )
    if status is not None and 200 <= status < 300:
        # status is non-None here: the only path that sets status=None also
        # sets probe.error, returned on above. urllib_post keeps the two
        # mutually exclusive. The explicit ``is not None`` is for the type
        # checker, which can't follow the mutual-exclusion invariant.
        return CheckResult(name=name, verdict="pass", detail=f"{status} OK", hint="")
    return CheckResult(
        name=name,
        verdict="warn",
        detail=f"{status} (unverified)",
        hint="unexpected status; doctor could not confirm the key works",
    )


def _check_shell_override(paths: Paths) -> CheckResult | None:
    """WARN if ``.zshrc`` exports ``ANTHROPIC_BASE_URL`` (ADR-003 override risk).

    Per ADR-003, a shell ``export ANTHROPIC_*`` can override settings.json and
    silently win. doctor surfaces that as a WARN so the user can decide. The
    check is skipped (``None``) when ``.zshrc`` is absent — nothing to report.

    READ-ONLY: a plain text grep, never edits the file.
    """
    if not paths.zshrc.exists():
        return None
    try:
        text = paths.zshrc.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        # A real export, not a comment and not inside our managed block marker
        # (our block intentionally does NOT export ANTHROPIC_* — ADR-003).
        if stripped.startswith("export ANTHROPIC_"):
            return CheckResult(
                name="shell env override",
                verdict="warn",
                detail=f"{stripped.split('=', 1)[0]} in ~/.zshrc",
                hint="shell env overrides settings.json; remove the export or it wins",
            )
    return None


def _check_mcp_installed(paths: Paths) -> CheckResult:
    """Report installed Z.ai preset MCPs across tools (S7, READ-ONLY).

    Reads each cross-tool MCP config (Claude Code, OpenCode, Crush, Factory
    Droid) through :mod:`zai_python_helper.mcp` and reports how many of the four
    preset MCPs are installed. ALL verdicts are ``pass`` — installing a preset
    MCP is an explicit opt-in, so an UN-installed preset is NOT a broken link
    and must NEVER fail doctor. The detail names the installed presets and the
    tools that carry them; the hint points at the opt-in install command.

    No network, no subprocess: this is a pure config read. Returns ``None``
    when no tool config exists at all (nothing to report — the check is
    informational and there is no chain to verify).
    """
    from zai_python_helper.mcp import (
        PRESET_MCP_SERVICES,
        Tool,
        is_installed,
        read_config,
        tool_config_path,
    )

    # Derive home from claude_settings: it is ``<home>/.claude/settings.json``,
    # so two parents up is home. (``parent`` = ``.claude``, ``parent.parent`` =
    # home.) This is the resolved home the cross-tool MCP configs live under.
    home = paths.claude_settings.parent.parent
    preset_ids = [p["id"] for p in PRESET_MCP_SERVICES]
    installed: list[str] = []
    for tool in Tool:
        try:
            doc = read_config(tool_config_path(tool, home))
        except Exception:
            # Unreadable tool config: doctor reports, never raises. The check
            # is informational, so skip this tool rather than FAIL.
            continue
        if doc is None:
            continue
        for mcp_id in preset_ids:
            if is_installed(doc, tool, mcp_id):
                # Tag the installed id with the tool it was found in, so the
                # detail line is actionable when several tools are configured.
                label = f"{mcp_id}@{tool.value}"
                if label not in installed:
                    installed.append(label)
    name = "preset MCP servers"
    if not installed:
        return CheckResult(
            name=name,
            verdict="pass",
            detail="none installed (opt-in)",
            hint="install with `zai-python-helper mcp install <id> --tool <tool>`",
        )
    return CheckResult(
        name=name,
        verdict="pass",
        detail=f"{len(installed)} installed ({', '.join(installed)})",
        hint="",
    )


def _check_opencode_duplicate_providers(paths: Paths) -> CheckResult | None:
    """Report an OpenCode duplicate-provider state, when one is present.

    This is deliberately an informational, read-only check.  A duplicate
    whose one entry is provably ours can be repaired by ``use zai``; an
    unattributable (including retired-journal) duplicate requires a hand
    edit, because ``use default`` must not guess which credential to remove.
    Any read or parse failure skips the check so ``doctor`` never turns an
    unreadable optional OpenCode config into an unrelated failure.
    """
    try:
        from zai_python_helper.backends import JsonBackend
        from zai_python_helper.core.planner import opencode
        from zai_python_helper.ownership import OwnershipJournal

        doc = JsonBackend.read(paths.opencode)
        if not opencode.has_duplicate_regional_providers(doc):
            return None
        journal = OwnershipJournal(paths.ownership_json).read()
        owned = opencode.owned_regional_provider_name(doc, journal)
    except Exception:
        return None

    if owned is not None:
        return CheckResult(
            name="OpenCode regional providers",
            verdict="warn",
            detail=f"duplicate state; helper owns {owned}",
            hint="run `zai-python-helper use zai` to self-heal the duplicate",
        )
    return CheckResult(
        name="OpenCode regional providers",
        verdict="fail",
        detail="duplicate state; neither entry is attributable to the helper",
        hint=(
            "edit opencode.json by hand and delete the unwanted regional "
            "provider entry; `use default` will not clear it"
        ),
    )


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def run_doctor(
    paths: Paths,
    *,
    http_get: HttpProbe = urllib_post,
    environ: Mapping[str, str] | None = None,
    color: bool | None = None,
    extra_zai_hosts: frozenset[str] | None = None,
) -> int:
    """Run the doctor diagnostic pipeline and print verdicts.

    Walks the Claude Code ⇄ Z.ai chain link-by-link, prints a verdict per
    check, and returns ``0`` unless at least one check FAILED (``[✗]``).
    WARNs (``[!]``) alone → exit ``0``.

    ALL applicable checks run (no short-circuit on the first FAIL): a later
    check may still produce useful info, and the earliest failure's hint is
    usually the root cause that explains later ones.

    Args:
        paths: the injected :class:`Paths` bundle. Tests inject
            ``Paths.from_home(tmp_path)``; the CLI handler injects
            ``Paths.default()``.
        http_get: the injectable HTTP seam (production: :func:`urllib_post`,
            a stdlib POST to the auth-enforcing ``/v1/messages`` endpoint with
            redirects disabled and HTTPS enforced; tests: a fake pointing at
            pytest-httpserver over a real socket).
        environ: the environment to consult for ``ZAI_API_KEY``. Defaults to
            ``os.environ`` (production); tests inject a controlled dict.
        color: force colored (``True``) / plain (``False``) markers; ``None``
            auto-detects from :func:`sys.stdout.isatty`.
        extra_zai_hosts: optional extra hosts the caller vouches are Z.ai
            origins, treated as a PASS for the postcondition (and so eligible
            for the credentialed probe). Production passes ``None`` — only the
            canonical region hosts are trusted, so an unrecognized host never
            silently receives the API key. Tests pass the httpserver host.

    Returns:
        ``0`` if no check has verdict ``"fail"``; ``1`` otherwise. WARNs do
        NOT fail doctor.

    READ-ONLY: this function performs NO writes and mutates no config file.
    """
    env = environ if environ is not None else os.environ
    results: list[CheckResult] = []

    def _emit(result: CheckResult) -> CheckResult:
        results.append(result)
        print(render_check(result, color=color))
        return result

    # 1. settings.json env block (the postcondition source). Read once, feed
    # the parsed env to the downstream checks.
    settings_result, settings_env = _check_settings_env(paths)
    _emit(settings_result)

    # 2. Z.ai endpoint postcondition. Capture its verdict: the HTTP probe is
    # GATED on it (a non-PASS endpoint never receives the credentialed probe).
    endpoint_result = _check_zai_endpoint(settings_env, extra_zai_hosts)
    _emit(endpoint_result)

    # 3. API key present (also resolves the key for the probe).
    key_result, key = _check_key_present(settings_env, env)
    _emit(key_result)

    # 4. HTTP probe against the auth-enforcing endpoint. The base URL may be
    # absent if step 1 failed, and the probe is SKIPPED if step 2 FAILED —
    # either way it WARNs "skipped" rather than sending the key.
    base_url = settings_env.get("ANTHROPIC_BASE_URL") if settings_env else None
    _emit(_check_http_probe(base_url, key, endpoint_result.verdict, http_get))

    # 5. shell env override risk (ADR-003) — WARN, skipped when no .zshrc.
    shell = _check_shell_override(paths)
    if shell is not None:
        _emit(shell)

    # 6. preset MCP servers (S7) — READ-ONLY config read across tools. Always a
    # PASS (installing a preset is an explicit opt-in, never a broken link), so
    # it contributes nothing to the exit code; it surfaces what IS installed.
    _emit(_check_mcp_installed(paths))

    # 7. OpenCode duplicate regional providers — optional, READ-ONLY, and only
    # emitted when the problematic state is actually present.
    duplicate = _check_opencode_duplicate_providers(paths)
    if duplicate is not None:
        _emit(duplicate)

    return 1 if any(r.verdict == "fail" for r in results) else 0
