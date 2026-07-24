"""Argparse parser builder for the ``zai-python-helper`` CLI.

The dispatch contract: each subcommand registers a handler via
``set_defaults(func=...)``, and :func:`zai_python_helper.__main__.main` calls
``args.func(args)``. Handlers are THIN SHELLS — resolve ``Paths.default()``,
delegate to a service, return its int. They do NOT catch/print/exit:
a :class:`ZaiPythonHelperError` propagates to :func:`main`, which formats it
as one-line stderr + exit 1 (full traceback under ``--debug``).

Root flags (``--debug`` / ``--dry-run``) attach via a single shared parent
parser so they parse BOTH before and after the subcommand (dual-parser pattern).
"""

import argparse


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


def _handle_use_zai(args: argparse.Namespace) -> int:
    """Make Z.ai the default provider.

    The ``--mode`` flag selects one of four model-selection strategies
    (original / default / select / custom); the remaining flags carry the
    model details that ``select``/``custom`` modes need. The actual config
    patching is applied in a later phase — for now this handler plans the
    env and reports what it would do (full effect under S2+).
    """
    from zai_python_helper.constants import get_preset_model
    from zai_python_helper.core.domain import ModelMode, ProviderSpec
    from zai_python_helper.core.planner.models import plan_model_config
    from zai_python_helper.errors import ValidationError
    from zai_python_helper.paths import Paths

    mode = ModelMode(getattr(args, "mode", ModelMode.ORIGINAL.value))
    paths = Paths.default()

    # Build the domain spec and validate it against the mode before planning.
    spec = ProviderSpec(
        base_url="https://api.z.ai/api/anthropic",
        model_mode=mode,
        selected_model=getattr(args, "model", None) if mode == ModelMode.SELECT else None,
        custom_model_id=getattr(args, "model", None) if mode == ModelMode.CUSTOM else None,
        custom_model_name=getattr(args, "name", None),
        custom_model_description=getattr(args, "description", None),
        custom_capabilities=getattr(args, "capabilities", None),
    )
    if not spec.validate():
        if mode == ModelMode.SELECT:
            raise ValidationError("--model is required for --mode select")
        if mode == ModelMode.CUSTOM:
            raise ValidationError("--model is required for --mode custom")

    # Validate the preset before planning: plan_model_config raises a bare
    # ValueError on an unknown preset, which would bypass the error contract.
    # Wrap it here so __main__ can format it as "error: ..." with exit 1.
    if mode == ModelMode.SELECT:
        selected = spec.selected_model
        if selected is None or get_preset_model(selected) is None:
            raise ValidationError(f"Unknown preset: {selected}")

    env = plan_model_config(spec)

    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print(f"dry-run: would configure Z.ai provider (mode: {mode.value})")
        print(f"  state_dir: {paths.state_dir}")
        for key in sorted(env):
            print(f"  {key}={env[key]}")
        return 0

    print(f"Configuring Z.ai provider (mode: {mode.value})")
    print(f"  state_dir: {paths.state_dir}")
    for key in sorted(env):
        print(f"  {key}={env[key]}")
    print("Not yet written to settings — see epic #1 (planner/IO phases)")
    return 0


def _handle_use_default(args: argparse.Namespace) -> int:
    """Revert to default provider (stub for S2)."""
    from zai_python_helper.paths import Paths

    paths = Paths.default()
    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("dry-run: would revert to default provider")
        return 0
    print(f"Reverted to default provider (paths: {paths.state_dir})")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Print current status (stub for S2)."""
    from zai_python_helper import __version__
    from zai_python_helper.paths import Paths

    paths = Paths.default()
    lines = [
        "Status:",
        f"  version: {__version__}",
        f"  state_dir: {paths.state_dir}",
        "",
        "Config paths:",
        f"  claude_settings: {paths.claude_settings}",
        f"  claude_json: {paths.claude_json}",
        f"  ownership_json: {paths.ownership_json}",
    ]
    print("\n".join(lines))
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    """Run diagnostics (stub for S2)."""
    print("Doctor: all checks passed (stub)")
    return 0


def _add_use_zai_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the model-selection flags to the ``use zai`` subparser."""
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
    # Global flags: work BOTH before AND after the subcommand. ONE shared
    # parent parser (SUPPRESS defaults) is attached to the root parser AND
    # every subparser via parents=[sub_flags], so --debug/--dry-run parse in
    # either position. SUPPRESS means a subparser copy does not override a
    # value the root already parsed.
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
    # No subcommand → show help (the bare invocation default). Every
    # subcommand overrides this via its own set_defaults.
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

    use_sub.add_parser(
        "default",
        help="revert to default provider",
        parents=[sub_flags],
    ).set_defaults(func=_handle_use_default)

    # `status` — read-only observability
    p_status = subparsers.add_parser(
        "status",
        help="show current status and paths",
        parents=[sub_flags],
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
