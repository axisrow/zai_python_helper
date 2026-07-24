"""Read-only status report: detect current tool configuration + render.

This module answers "what is my Claude Code ⇄ Z.ai integration state right
now?" without writing anything and without touching the network. It is the
S4 observability surface called by the ``status`` subcommand.

**Layering note (ADR-001).** ``status`` is *read-only IO* — it opens config
files. S2 (``io/backends``) is not landed yet, so for now this module reads
files directly through :class:`~zai_python_helper.paths.Paths` and
``pathlib``. The read surface is intentionally narrow (a few ``read_text`` /
``json.load`` calls) so it can later delegate to a read-only backend method
without changing this module's public contract. What it must NEVER do:
write a file, mutate state, or open a socket. Enforced by review.

Public surface:
- :func:`detect_status` — pure read, returns a :class:`StatusReport`.
- :func:`render_status` — renders a :class:`StatusReport` to text (ANSI only
  when the stream is a tty).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region

# ADR-003: the owned marker-fenced block we install in ~/.zshrc. We detect
# its presence to report "managed block installed" — we do NOT parse or
# trust its contents.
ZSHRC_BLOCK_BEGIN = "# >>> zai-python-helper managed >>>"
ZSHRC_BLOCK_END = "# <<< zai-python-helper managed <<<"

# ``export ANTHROPIC_FOO=...`` outside our managed block can override
# settings.json. We warn when we see one (ADR-003). The capture group
# yields the variable NAME only (e.g. ``ANTHROPIC_API_KEY``) — the value
# is deliberately never captured, so a secret in the assignment can never
# reach the report (the "no secrets in output" invariant).
_ANTHROPIC_EXPORT_RE = re.compile(
    r"""^\s*export\s+(ANTHROPIC_+\w*)\s*=""", re.MULTILINE
)

# Where Claude Code stores the API key. Z.ai keys live in AUTH_TOKEN
# (format "<id>.<secret>"); the older *_API_KEY form is checked too.
_API_KEY_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")

# Z.ai endpoint hosts by region. Kept here (not in regions.py) because
# regions.py owns the *canonical* endpoints, while status needs to match
# arbitrary user-entered URLs (with path/port/protocol variants) to a
# region — a matching concern, distinct from endpoint lookup.
_ZAI_HOSTS = {
    Region.GLOBAL: ("z.ai",),
    Region.CHINA: ("z.cn",),
}

# ANSI codes used by the renderer. Applied ONLY on a tty.
_ANSI = {
    "dim": "\033[2m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "reset": "\033[0m",
}


@dataclass
class ZshrcState:
    """State of ``~/.zshrc`` relative to the integration.

    ``foreign_exports`` holds the **variable names** of foreign
    ``export ANTHROPIC_*`` assignments (e.g. ``["ANTHROPIC_API_KEY"]``),
    never their values — a value could be a live secret, and the report
    must never carry one (the "no secrets in output" invariant).
    """

    exists: bool
    managed_block_present: bool
    foreign_exports: list[str] = field(default_factory=list)


@dataclass
class ClaudeCodeStatus:
    """Detected state of the Claude Code integration."""

    settings_present: bool
    zai_active: bool
    region: Region | None
    base_url: str | None
    key_var: str | None
    key_masked: str | None
    zshrc: ZshrcState


@dataclass
class StatusReport:
    """The full read-only status of all detected tools."""

    claude_code: ClaudeCodeStatus | None = None


def _host_of(url: str) -> str:
    """Return the lowercase host of a URL, robust to userinfo/port.

    Uses :func:`urllib.parse.urlsplit` (pure parsing, no network) so a
    ``user:pass@host`` userinfo or a ``:port`` suffix does not corrupt the
    host extraction the way a naive ``split(':')`` would.
    """
    return urlsplit(url).hostname or ""


def _classify_region(base_url: str) -> Region | None:
    """Map a base URL to a :class:`Region`, or ``None`` if not a Z.ai host.

    Only the host matters. A URL pointing at neither ``z.ai`` nor ``z.cn``
    (e.g. the real ``api.anthropic.com``) is "not Z.ai" → ``None``,
    reported as inactive.
    """
    host = _host_of(base_url)
    for region, hosts in _ZAI_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return region
    return None


def _safe_endpoint(url: str) -> str:
    """Strip secret-bearing components from a URL for status display.

    Removes userinfo (``user:pass@`` — a common place to embed a token),
    the query string, and the fragment before rendering. The scheme, host,
    port, and path are kept, which is enough to recognize the endpoint
    without ever echoing an embedded credential. Pure parsing — no network.

    **Fail-closed:** if the URL does not parse to a real authority/hostname
    (malformed input — missing ``//`` before the authority, leading
    whitespace/control chars, etc.), ``urlsplit`` would leave the raw string
    in ``path`` and the credential could still be echoed. In that case we
    return a placeholder rather than risk partial disclosure, because
    ``status`` explicitly must tolerate malformed config without leaking.
    """
    parts = urlsplit(url.strip())
    # Require a parseable hostname; otherwise do not echo any of the raw URL.
    if not parts.hostname:
        return "(malformed endpoint)"
    # Drop userinfo, query, fragment; keep scheme/host/port/path.
    return urlunsplit((parts.scheme, parts.netloc.split("@")[-1], parts.path, "", ""))


def mask_key(value: str, visible_suffix: int = 4) -> str:
    """Mask a secret for status display: ``<prefix>••••<suffix>``.

    Mirrors the task's ``zai-••••3f2a`` shape — a fixed 4-bullet core with
    a leading prefix and a trailing visible suffix, so two different keys
    are still distinguishable at a glance while the secret body is hidden.

    **Fail-closed for short values.** The hidden core (characters covered
    by the bullets) must be at least as large as the visible suffix, so a
    short key cannot be trivially reconstructed by enumerating the few
    hidden characters. Values too short to leave such a core are shown as
    bullets only — we never disclose the majority of a secret's characters.
    """
    if not value:
        return ""

    # Visible chars = a <=4-char prefix + a suffix. Require the hidden core
    # to be at least ``visible_suffix`` chars (4 by default), so a key short
    # enough to enumerate can't have most of its body disclosed: a 6–8 char
    # key is fully hidden, while a long production key keeps a recognizable
    # prefix/suffix (e.g. ``zai-••••3f2a``). Try the largest suffix first;
    # shrink until the invariant holds; if it can't, hide the whole value.
    for suffix in range(min(visible_suffix, len(value)), 0, -1):
        prefix_len = min(4, len(value) - suffix)
        hidden = len(value) - prefix_len - suffix
        if prefix_len > 0 and hidden >= visible_suffix:
            return f"{value[:prefix_len]}{'•' * 4}{value[-suffix:]}"
    # Too short to expose anything safely — hide the whole value.
    return "•" * 4


def _read_zshrc(zshrc: Path) -> ZshrcState:
    """Detect the managed block and any foreign ``ANTHROPIC_*`` exports.

    A foreign export is one that lives OUTSIDE our managed marker-fenced
    block (ADR-003): exports inside the block are ours by definition and
    we never write any, so any ``export ANTHROPIC_*`` we see outside it is
    the user's (or another tool's) and may override ``settings.json``.

    Only the variable **names** are collected (never the assigned values),
    so a secret stored in such an export can never reach the report.
    """
    if not zshrc.exists():
        return ZshrcState(exists=False, managed_block_present=False)

    text = zshrc.read_text(encoding="utf-8", errors="replace")

    # A well-formed block has BEGIN before END. Guard the ordering: if the
    # markers are inverted or duplicated, the slice would cut the wrong
    # span and mis-flag exports, so treat a malformed pair as "no block"
    # (then every export is counted as foreign — the safe direction).
    begin = text.find(ZSHRC_BLOCK_BEGIN)
    end = text.find(ZSHRC_BLOCK_END)
    in_block = begin != -1 and end != -1 and begin < end

    # Slice out our managed block so exports inside it are not flagged.
    if in_block:
        outside = text[:begin] + text[end + len(ZSHRC_BLOCK_END):]
    else:
        outside = text

    foreign_names = _ANTHROPIC_EXPORT_RE.findall(outside)

    return ZshrcState(
        exists=True,
        managed_block_present=in_block,
        foreign_exports=foreign_names,
    )


def _detect_claude_code(paths: Paths) -> ClaudeCodeStatus:
    """Read Claude Code config files and classify the integration state.

    Read-only: opens ``settings.json`` and ``.zshrc``; opens no sockets,
    writes nothing.
    """
    settings_path = paths.claude_settings
    settings_present = settings_path.exists()

    base_url: str | None = None
    key_var: str | None = None
    key_masked: str | None = None

    if settings_present:
        try:
            data = json.loads(
                settings_path.read_text(encoding="utf-8", errors="replace")
            )
        except (json.JSONDecodeError, OSError):
            data = {}
        env = data.get("env") if isinstance(data, dict) else None
        if isinstance(env, dict):
            # ``ANTHROPIC_BASE_URL`` must be a string; a malformed settings
            # file (schema drift / manual corruption) may hold an int/list/
            # dict/bool. Degrade to "no endpoint" rather than crashing the
            # diagnostic command (the read-only invariant must hold under
            # corrupt input).
            raw_url = env.get("ANTHROPIC_BASE_URL")
            if isinstance(raw_url, str):
                base_url = _safe_endpoint(raw_url)
            for var in _API_KEY_VARS:
                val = env.get(var)
                if isinstance(val, str) and val:
                    key_var = var
                    key_masked = mask_key(val)
                    break

    region = _classify_region(base_url) if base_url else None
    zai_active = region is not None

    return ClaudeCodeStatus(
        settings_present=settings_present,
        zai_active=zai_active,
        region=region,
        base_url=base_url,
        key_var=key_var,
        key_masked=key_masked,
        zshrc=_read_zshrc(paths.zshrc),
    )


def detect_status(paths: Paths) -> StatusReport:
    """Read all tool configs and return a :class:`StatusReport`.

    Read-only and side-effect free. Today only Claude Code is detected;
    OpenCode/Crush/Factory Droid join after S6.
    """
    return StatusReport(claude_code=_detect_claude_code(paths))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _ansi(name: str, use_color: bool) -> str:
    return _ANSI[name] if use_color else ""


def _render_claude_code(
    cc: ClaudeCodeStatus, *, use_color: bool
) -> list[str]:
    """Render one Claude Code block as lines of text."""
    green = _ansi("green", use_color)
    yellow = _ansi("yellow", use_color)
    dim = _ansi("dim", use_color)
    reset = _ansi("reset", use_color)

    lines: list[str] = ["Claude Code", "──────────"]

    if not cc.settings_present:
        lines.append(f"{dim}(no settings.json found){reset}")
        return lines

    # Active / region line.
    if cc.zai_active:
        assert cc.region is not None  # narrowed by zai_active
        lines.append(
            f"  Z.ai: {green}active{reset} "
            f"(region: {cc.region.value})"
        )
        if cc.base_url:
            lines.append(f"  endpoint: {cc.base_url}")
    else:
        lines.append(f"  Z.ai: {dim}inactive{reset}")
        if cc.base_url:
            lines.append(f"  endpoint: {cc.base_url} (not a Z.ai host)")

    # Key line.
    if cc.key_masked:
        assert cc.key_var is not None
        lines.append(f"  key ({cc.key_var}): {cc.key_masked}")
    else:
        lines.append(f"  key: {dim}(not set){reset}")

    # Managed .zshrc block.
    zsh = cc.zshrc
    if not zsh.exists:
        lines.append(f"  managed block: {dim}(no .zshrc){reset}")
    elif zsh.managed_block_present:
        lines.append(f"  managed block: {green}installed{reset}")
    else:
        lines.append(f"  managed block: {dim}absent{reset}")

    # WARNING: foreign export may override settings.json (ADR-003).
    # Render only the variable NAME with a literal ``<redacted>`` placeholder
    # — the assigned value is a possible secret and must never be echoed.
    if zsh.foreign_exports:
        lines.append(
            f"  {yellow}⚠ shell env may override settings.json:{reset}"
        )
        for name in zsh.foreign_exports:
            lines.append(f"    {dim}export {name}=<redacted>{reset}")

    return lines


def render_status(
    report: StatusReport, *, stream=None, use_color: bool | None = None
) -> str:
    """Render a :class:`StatusReport` to a single string.

    Args:
        report: The detected status.
        stream: A text stream (defaults to :data:`sys.stdout`). Used only to
            decide ANSI coloring when ``use_color`` is ``None``.
        use_color: Force ANSI on/off. When ``None`` (default), color is
            enabled iff ``stream.isatty()`` — so tests capturing stdout get
            plain text and the terminal gets color.
    """
    if use_color is None:
        stream = stream if stream is not None else sys.stdout
        use_color = bool(getattr(stream, "isatty", lambda: False)())

    blocks: list[str] = []
    if report.claude_code is not None:
        blocks.append(
            "\n".join(_render_claude_code(report.claude_code, use_color=use_color))
        )

    if not blocks:
        return "(no supported tools detected)"
    return "\n\n".join(blocks)
