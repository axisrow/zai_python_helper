"""Argparse parser builder + handlers for the ``zai-python-helper`` CLI.

The dispatch contract: each subcommand registers a handler via
``set_defaults(func=...)``, and :func:`zai_python_helper.__main__.main` calls
``args.func(args)``. Handlers are THIN SHELLS — resolve ``Paths.default()``,
delegate to the planner + IO backends, return an int. They do NOT
catch/print/exit: a :class:`ZaiPythonHelperError` propagates to :func:`main`,
which formats it as one-line stderr + exit 1 (full traceback under ``--debug``).

Root flags (``--debug`` / ``--dry-run``) attach via a single shared parent
parser so they parse BOTH before and after the subcommand (dual-parser
pattern).
"""

import argparse
import difflib
import re
from pathlib import Path

from zai_python_helper.core.planner import DeltaKind, FileTag, PatchPlan
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region

# Tag → Paths attribute. Single place that maps a semantic file tag to its
# resolved path. Keeps the planner path-free and the CLI's apply loop generic.
_TAG_TO_PATH = {
    FileTag.SETTINGS: "claude_settings",
    FileTag.CLAUDE_JSON: "claude_json",
    FileTag.ZSHRC: "zshrc",
}

# Keys whose values are secrets and must be redacted in any diff/echo output
# (ADR: secrets never logged). Used by both --dry-run and the post-run echo.
# This is a conservative allowlist-by-name: a key is secret if it is one of
# the explicit names below OR matches a credential-ish suffix/pattern. We
# redact defensively so a foreign key we don't know about (OPENAI_API_KEY,
# cloud tokens, etc.) never leaks through a dry-run diff or echo.
_SECRET_ENV_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
_SECRET_NAME_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD")
_SECRET_NAME_SUBSTRINGS = ("SECRET", "PASSWORD", "CREDENTIAL", "TOKEN", "API_KEY")

_RESTART_NOTICE = "restart recommended for deterministic switching"


def _is_secret_key(key: str) -> bool:
    """Heuristic: is ``key`` likely a credential that must be redacted?

    Conservative — errs on the side of redacting. Matches the explicit
    managed-secret names plus credential-ish suffixes/substrings so foreign
    secrets (OPENAI_API_KEY, cloud tokens) are caught even though we don't
    enumerate them.
    """
    upper = key.upper()
    if key in _SECRET_ENV_KEYS:
        return True
    if any(upper.endswith(suf) for suf in _SECRET_NAME_SUFFIXES):
        return True
    return any(sub in upper for sub in _SECRET_NAME_SUBSTRINGS)


def _resolve_path(paths: Paths, tag: FileTag) -> Path:
    """Map a semantic :class:`FileTag` to its resolved :class:`Path`."""
    return getattr(paths, _TAG_TO_PATH[tag])


def _redact_text(text: str) -> str:
    """Redact secret values in rendered text for safe diffing/echo.

    Replaces the value of any credential-looking assignment with
    ``<redacted>``. Covers BOTH syntaxes a managed file may use so a secret
    never leaks through a ``--dry-run`` diff's context lines:

    - JSON (``settings.json`` / ``.claude.json``): ``"KEY": "value"``
    - shell (``.zshrc``): ``export KEY="value"``, ``export KEY=value``,
      and bare ``KEY=value`` assignments.

    A key is "secret" by :func:`_is_secret_key` (name heuristic), so foreign
    credentials we don't manage (OPENAI_API_KEY, cloud tokens) are caught in
    either file type. A secret value never reaches stdout/stderr.
    """
    # JSON: ``"KEY": "value"`` → ``"KEY": "<redacted>"``.
    def _replace_json(match: re.Match[str]) -> str:
        key = match.group(1)
        if _is_secret_key(key):
            return f'"{key}": "<redacted>"'
        return match.group(0)

    text = re.sub(r'"([A-Za-z0-9_]+)"\s*:\s*"([^"]*)"', _replace_json, text)

    # Shell: ``[export ]KEY="value"`` (quoted) or ``[export ]KEY=value``
    # (unquoted, value = run of non-whitespace). One alternation handles both
    # so a line is matched at most once. Keep ``export`` + key + quotes,
    # redact only the value for secret keys.
    _shell_pat = re.compile(
        r'(?m)^(\s*export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"\n]*)"|([^\s"\'#]+))'
    )

    def _replace_shell(match: re.Match[str]) -> str:
        prefix = match.group(1) or ""
        key = match.group(2)
        if not _is_secret_key(key):
            return match.group(0)
        # group(3) = quoted value, group(4) = unquoted value.
        if match.group(3) is not None:
            return f'{prefix}{key}="<redacted>"'
        return f"{prefix}{key}=<redacted>"

    return _shell_pat.sub(_replace_shell, text)


def _apply_plan(paths: Paths, plan: PatchPlan, *, dry_run: bool) -> list[FileTag]:
    """Apply (or preview) a plan's deltas. Returns the list of written tags.

    For each non-NOOP delta, in dry-run mode it computes a ``unified_diff``
    between current and desired content (secrets redacted) and prints it; in
    real mode it writes the file via the matching backend. NOOP deltas are
    skipped silently in both modes.

    Returns the tags that were actually written (empty under dry-run, since
    dry-run writes nothing — callers use the return only in real mode).
    """
    from zai_python_helper.backends import JsonBackend, ShellBackend

    written: list[FileTag] = []
    for delta in plan.deltas:
        if delta.kind == DeltaKind.NOOP:
            continue
        path = _resolve_path(paths, delta.tag)

        if delta.kind == DeltaKind.WRITE_JSON:
            current_doc = JsonBackend.read(path)
            current_text = (
                JsonBackend.render(current_doc) if current_doc is not None else ""
            )
            desired_text = JsonBackend.render(delta.content)
        else:  # WRITE_TEXT
            current_text = ShellBackend.read(path)
            desired_text = delta.content

        if dry_run:
            _print_diff(path, current_text, desired_text, delta.tag)
            continue

        if delta.kind == DeltaKind.WRITE_JSON:
            JsonBackend.write(path, delta.content)
        else:
            from zai_python_helper.backends import atomic_write_bytes

            atomic_write_bytes(path, desired_text.encode("utf-8"))
        written.append(delta.tag)
    return written


def _print_diff(path: Path, current: str, desired: str, tag: FileTag) -> None:
    """Print a redacted unified_diff for one file under ``--dry-run``."""
    label = f"{path} ({tag.value})"
    cur_lines = current.splitlines(keepends=True)
    des_lines = desired.splitlines(keepends=True)
    diff = difflib.unified_diff(
        cur_lines,
        des_lines,
        fromfile=f"{label} (current)",
        tofile=f"{label} (desired)",
    )
    diff_text = "".join(diff)
    if not diff_text:
        # No textual diff — nothing would change for this file.
        return
    print(_redact_text(diff_text), end="")


def _handle_list(args: argparse.Namespace) -> int:
    """List available Z.ai model presets."""
    from zai_python_helper.constants import (
        ZAI_MODEL_PRESETS,
        get_preset_model,
        list_available_presets,
    )

    fmt = getattr(args, "format", "table")
    if fmt == "json":
        import json

        print(json.dumps(ZAI_MODEL_PRESETS, indent=2))
        return 0

    print("Available Z.ai model presets:")
    print()
    for preset in list_available_presets():
        config = get_preset_model(preset)
        if config is None:
            print(f"  {preset}: (error: preset not found)")
            continue
        print(f"  {preset}:")
        print(f"    ID: {config['model_id']}")
        print(f"    Name: {config['name']}")
        print(f"    Description: {config['description']}")
        print(f"    Maps to: {config['anthropic_alias']}")
        print()
    return 0


def _build_provider_spec(args: argparse.Namespace, mode, region: Region):
    """Build a ProviderSpec from CLI args. Carries the model mode + selection.

    The ``base_url`` stored on the spec is the region's Z.ai URL; the planner
    also reads the canonical URL from ``base_url_for_region(region)`` so this
    field is informational here (kept consistent for ``status``/echo).
    """
    from zai_python_helper.core.domain import ProviderSpec
    from zai_python_helper.core.planner.claude_code import base_url_for_region

    return ProviderSpec(
        base_url=base_url_for_region(region),
        model_mode=mode,
        selected_model=getattr(args, "model", None) if mode.value == "select" else None,
        custom_model_id=getattr(args, "model", None) if mode.value == "custom" else None,
        custom_model_name=getattr(args, "name", None),
        custom_model_description=getattr(args, "description", None),
        custom_capabilities=getattr(args, "capabilities", None),
    )


def _handle_use_zai(args: argparse.Namespace) -> int:
    """Make Z.ai the default provider — patch settings.json/.claude.json/.zshrc.

    Plans the activation via the PURE planner, then either previews it
    (``--dry-run`` prints redacted unified_diffs, writes nothing) or applies
    it via the IO backends. Prints ``restart recommended`` whenever it changes
    files (ADR-005). Idempotent: a second run with no drift is a no-op.
    """
    from zai_python_helper.constants import get_preset_model
    from zai_python_helper.core.domain import ModelMode
    from zai_python_helper.core.planner import plan_zai
    from zai_python_helper.core.planner.claude_code import base_url_for_region
    from zai_python_helper.errors import ValidationError
    from zai_python_helper.io.secrets import resolve_key

    mode = ModelMode(getattr(args, "mode", ModelMode.ORIGINAL.value))
    region = Region(getattr(args, "region", Region.GLOBAL.value))
    paths = Paths.default()
    dry_run = getattr(args, "dry_run", False)

    # Custom-only flags must be rejected outside custom mode (a typo'd --mode
    # must not silently drop the user's --name etc.).
    custom_only = {
        "--name": getattr(args, "name", None),
        "--description": getattr(args, "description", None),
        "--capabilities": getattr(args, "capabilities", None),
    }
    if mode != ModelMode.CUSTOM:
        used = [flag for flag, value in custom_only.items() if value]
        if used:
            raise ValidationError(f"{', '.join(used)} only apply to --mode custom")

    spec = _build_provider_spec(args, mode, region)
    if not spec.validate():
        if mode == ModelMode.SELECT:
            raise ValidationError("--model is required for --mode select")
        if mode == ModelMode.CUSTOM:
            raise ValidationError("--model is required for --mode custom")

    # Validate the preset before planning (plan_model_config raises a bare
    # ValueError on an unknown preset; wrap it for the error contract).
    if mode == ModelMode.SELECT:
        selected = spec.selected_model
        if selected is None or get_preset_model(selected) is None:
            raise ValidationError(f"Unknown preset: {selected}")

    # Resolve the auth token in the IO layer (env/flag/prompt) — never in core.
    # In dry-run we still need a token to plan (the planner writes it into the
    # delta); a placeholder keeps the preview meaningful without prompting.
    if dry_run:
        auth_token = getattr(args, "api_key", None) or "<redacted>"
    else:
        auth_token = resolve_key(getattr(args, "api_key", None))

    # Read current parsed state for the planner (pure transform).
    from zai_python_helper.backends import JsonBackend, ShellBackend

    settings_doc = JsonBackend.read(paths.claude_settings)
    claude_json_doc = JsonBackend.read(paths.claude_json)
    zshrc_text = ShellBackend.read(paths.zshrc)

    plan = plan_zai(
        spec,
        region,
        settings_doc=settings_doc,
        claude_json_doc=claude_json_doc,
        zshrc_text=zshrc_text,
        auth_token=auth_token,
    )

    print(f"Configuring Z.ai provider (mode: {mode.value}, region: {region.value})")

    if dry_run:
        print("--dry-run: no files written")
        if plan.is_empty:
            print("(no changes — already in desired state)")
        _apply_plan(paths, plan, dry_run=True)
        return 0

    written = _apply_plan(paths, plan, dry_run=False)
    if not written:
        print("(no changes — already in desired state)")
    else:
        for tag in written:
            print(f"  updated: {_resolve_path(paths, tag)}")
        print(f"  {_RESTART_NOTICE}")

    # Echo ONLY the tool-owned managed keys (never foreign env values, which
    # may carry unrelated secrets like OPENAI_API_KEY). The managed set is
    # derived from the planner so it stays in sync; secrets among them are
    # redacted by name.
    from zai_python_helper.core.planner.claude_code import (
        MANAGED_ZAI_KEYS,
        _all_managed_model_keys,
    )

    settings_delta = plan.delta_for(FileTag.SETTINGS)
    desired_env = settings_delta.content.get("env", {}) if settings_delta else {}
    managed_keys = set(MANAGED_ZAI_KEYS) | set(_all_managed_model_keys())
    owned = {k: desired_env[k] for k in desired_env if k in managed_keys}
    if owned:
        print(f"  base_url: {base_url_for_region(region)}")
        print("  env (managed):")
        for key in sorted(owned):
            val = "<redacted>" if _is_secret_key(key) else owned[key]
            print(f"    {key}={val}")
    return 0


def _handle_use_default(args: argparse.Namespace) -> int:
    """Revert to the default provider — strip managed env keys, remove block.

    Inverse of ``use zai`` for ``settings.json`` and ``.zshrc``.
    ``.claude.json`` is intentionally NOT reverted. Idempotent.
    """
    from zai_python_helper.core.domain import ModelMode
    from zai_python_helper.core.planner import plan_default
    from zai_python_helper.errors import ValidationError

    # Model mode is irrelevant to removal (the managed set is derived from it),
    # but we accept --mode for symmetry and reject custom-only flags the same
    # way use zai does so the CLI surface is consistent.
    mode = ModelMode(getattr(args, "mode", ModelMode.ORIGINAL.value))
    region = Region(getattr(args, "region", Region.GLOBAL.value))
    paths = Paths.default()
    dry_run = getattr(args, "dry_run", False)

    custom_only = {
        "--name": getattr(args, "name", None),
        "--description": getattr(args, "description", None),
        "--capabilities": getattr(args, "capabilities", None),
    }
    if mode != ModelMode.CUSTOM:
        used = [flag for flag, value in custom_only.items() if value]
        if used:
            raise ValidationError(f"{', '.join(used)} only apply to --mode custom")

    spec = _build_provider_spec(args, mode, region)

    from zai_python_helper.backends import JsonBackend, ShellBackend

    settings_doc = JsonBackend.read(paths.claude_settings)
    zshrc_text = ShellBackend.read(paths.zshrc)

    plan = plan_default(spec, settings_doc=settings_doc, zshrc_text=zshrc_text)

    print(f"Reverting to default provider (region: {region.value})")

    if dry_run:
        print("--dry-run: no files written")
        if plan.is_empty:
            print("(no changes — already at default)")
        _apply_plan(paths, plan, dry_run=True)
        return 0

    written = _apply_plan(paths, plan, dry_run=False)
    if not written:
        print("(no changes — already at default)")
    else:
        for tag in written:
            print(f"  updated: {_resolve_path(paths, tag)}")
        print(f"  {_RESTART_NOTICE}")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Print the current Claude Code ⇄ Z.ai integration state.

    Delegates to :mod:`zai_python_helper.status` (read-only detect + render).
    Detection is read-only: no writes, no network. The ``--region`` flag
    (added by S2 for the postconditions-based stub) is accepted for CLI
    compatibility but not used — ``status`` auto-detects the region from
    the configured endpoint.
    """
    from zai_python_helper.paths import Paths
    from zai_python_helper.status import detect_status, render_status

    paths = Paths.default()
    report = detect_status(paths)
    print(render_status(report))
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    """Run diagnostics (stub for later phases)."""
    print("Doctor: all checks passed (stub)")
    return 0


def _add_use_zai_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the model-selection + region flags to a ``use`` subparser."""
    from zai_python_helper.core.domain import ModelMode

    parser.add_argument(
        "--mode",
        choices=[m.value for m in ModelMode],
        default=ModelMode.ORIGINAL.value,
        help=(
            "model selection mode (default: original). "
            "original: only ANTHROPIC_BASE_URL; "
            "default: preset ANTHROPIC_DEFAULT_*_MODEL vars; "
            "select: choose a preset; "
            "custom: provide a custom model ID"
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "model ID (for --mode select or custom). "
            "select: preset name (e.g. glm-4-plus); "
            "custom: full model ID"
        ),
    )
    parser.add_argument(
        "--region",
        choices=[r.value for r in Region],
        default=Region.GLOBAL.value,
        help="Z.ai region (default: global)",
    )
    parser.add_argument(
        "--api-key",
        help="Z.ai auth token (else resolved from ZAI_API_KEY env / prompt)",
    )
    parser.add_argument(
        "--name",
        help="display name for the custom model (custom mode only)",
    )
    parser.add_argument(
        "--description",
        help="description for the custom model (custom mode only)",
    )
    parser.add_argument(
        "--capabilities",
        help="supported capabilities for the custom model (e.g. 'effort,thinking')",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the root ``zai-python-helper`` argparse parser.

    The root parser owns the global flags (``--debug`` / ``--dry-run``) and
    the subcommand dispatch table. Root flags work BOTH before AND after the
    subcommand via a single shared parent parser attached to the root parser
    AND every subparser via ``parents=[sub_flags]``.
    """
    sub_flags = argparse.ArgumentParser(add_help=False)
    sub_flags.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show full traceback on error",
    )
    sub_flags.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="preview without writing",
    )

    parser = argparse.ArgumentParser(
        prog="zai-python-helper",
        description="Manage Claude Code ⇄ Z.ai integration without hand-editing config.",
        parents=[sub_flags],
    )
    # ``-v`` / ``--version`` prints the bare version string, mirroring the
    # upstream ``@z_ai/coding-helper`` (Commander ``.version(version)``), which
    # prints just the package version with no program-name prefix. This is a
    # Phase-1 parity surface (see issue #17): the FORMAT must match the
    # original even though the version NUMBER differs.
    from zai_python_helper import __version__

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    parser.set_defaults(func=lambda args: (parser.print_help(), 0)[1])

    subparsers = parser.add_subparsers(
        dest="cmd",
        required=False,
        metavar="<command>",
    )

    # `list` — show available Z.ai model presets
    p_list = subparsers.add_parser(
        "list",
        help="list available Z.ai model presets",
        parents=[sub_flags],
    )
    p_list.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="output format (default: table)",
    )
    p_list.set_defaults(func=_handle_list)

    # `use` — nested provider sub-subs
    p_use = subparsers.add_parser(
        "use",
        help="switch the default provider",
        parents=[sub_flags],
    )
    use_sub = p_use.add_subparsers(
        dest="provider",
        required=True,
        metavar="<provider>",
    )

    p_use_zai = use_sub.add_parser(
        "zai",
        help="make Z.ai the default",
        parents=[sub_flags],
    )
    _add_use_zai_flags(p_use_zai)
    p_use_zai.set_defaults(func=_handle_use_zai)

    p_use_default = use_sub.add_parser(
        "default",
        help="revert to default provider",
        parents=[sub_flags],
    )
    _add_use_zai_flags(p_use_default)
    p_use_default.set_defaults(func=_handle_use_default)

    # `status` — read-only observability
    p_status = subparsers.add_parser(
        "status",
        help="show current status and paths",
        parents=[sub_flags],
    )
    p_status.add_argument(
        "--region",
        choices=[r.value for r in Region],
        default=Region.GLOBAL.value,
        help="Z.ai region to check against (default: global)",
    )
    p_status.set_defaults(func=_handle_status)

    # `doctor` — diagnostic
    p_doctor = subparsers.add_parser(
        "doctor",
        help="diagnose the integration",
        parents=[sub_flags],
    )
    p_doctor.set_defaults(func=_handle_doctor)

    return parser
