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


def _classify_region(base_url: str) -> Region | None:
    """Map a base URL to a :class:`Region`, or ``None`` if not a Z.ai host.

    Normalizes scheme/path/port away — only the host matters. A URL pointing
    at neither ``z.ai`` nor ``z.cn`` (e.g. the real ``api.anthropic.com``)
    is "not Z.ai" → ``None``, reported as inactive.
    """
    # Strip scheme.
    cleaned = base_url
    for scheme in ("https://", "http://"):
        if cleaned.startswith(scheme):
            cleaned = cleaned[len(scheme):]
    # Strip path / port / query.
    host = cleaned.split("/", 1)[0].split(":", 1)[0].lower()
    for region, hosts in _ZAI_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return region
    return None


def mask_key(value: str, visible_suffix: int = 4) -> str:
    """Mask a secret for status display: ``<prefix>••••<suffix>``.

    Mirrors the task's ``zai-••••3f2a`` shape — a fixed 4-bullet core with
    a leading prefix and a trailing visible suffix, so two different keys
    are still distinguishable at a glance while the secret body is hidden.

    The visible prefix and suffix together must always be SHORTER than the
    value: if they covered the whole value the bullets would hide nothing
    and the two ends would reconstruct the secret. For values too short to
    leave a non-empty hidden core, the entire value is shown as bullets.
    """
    if not value:
        return ""

    # Choose the largest suffix (<= visible_suffix) that still leaves a
    # non-empty hidden core with a <=4-char prefix.
    suffix = min(visible_suffix, max(0, len(value) - 5))
    prefix_len = min(4, len(value) - suffix)
    if prefix_len <= 0 or suffix <= 0 or prefix_len + suffix >= len(value):
        # Too short to expose anything safely — hide the whole value.
        return "•" * 4
    return f"{value[:prefix_len]}{'•' * 4}{value[-suffix:]}"


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
            base_url = env.get("ANTHROPIC_BASE_URL")
            for var in _API_KEY_VARS:
                val = env.get(var)
                if val:
                    key_var = var
                    key_masked = mask_key(str(val))
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
