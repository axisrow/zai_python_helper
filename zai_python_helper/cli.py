"""
CLI entry point for zai_python_helper.

This module provides the command-line interface using argparse.
Per architecture, cli.py is thin — just arg parsing and output delegation.
"""

import argparse
import sys

from zai_python_helper import __version__
from zai_python_helper.constants import (
    ZAI_ANTHROPIC_BASE_URL,
    list_available_presets,
)
from zai_python_helper.core.domain import ModelMode


def create_parser() -> argparse.ArgumentParser:
    """
    Create the main argument parser for zai-python-helper.

    Returns:
        Configured ArgumentParser instance
    """
    parser = argparse.ArgumentParser(
        prog="zai-python-helper",
        description="Connect Claude Code to Z.ai GLM Coding Plan",
        epilog="MIT licensed. See https://github.com/axisrow/zai_python_helper",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 'use' command — switch configuration
    use_parser = subparsers.add_parser(
        "use",
        help="Switch Claude Code to use a provider",
    )
    use_parser.add_argument(
        "provider",
        choices=["zai", "default"],
        help="Provider to use ('zai' for Z.ai, 'default' for Anthropic)",
    )
    use_parser.add_argument(
        "--mode",
        choices=[m.value for m in ModelMode],
        default=ModelMode.ORIGINAL.value,
        help=(
            "Model selection mode (default: original). "
            "Original: only ANTHROPIC_BASE_URL. "
            "Default: use preset models. "
            "Select: choose from list. "
            "Custom: provide custom model ID."
        ),
    )
    use_parser.add_argument(
        "--model",
        help=(
            "Model ID (for 'select' or 'custom' mode). "
            "For 'select': use preset name (e.g., glm-4-plus). "
            "For 'custom': provide full model ID."
        ),
    )
    use_parser.add_argument(
        "--name",
        help="Display name for custom model (custom mode only)",
    )
    use_parser.add_argument(
        "--description",
        help="Description for custom model (custom mode only)",
    )
    use_parser.add_argument(
        "--capabilities",
        help="Supported capabilities for custom model (e.g., 'effort,thinking')",
    )
    use_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without making changes",
    )
    use_parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed debugging information",
    )

    # 'list' command — show available models
    list_parser = subparsers.add_parser(
        "list",
        help="List available Z.ai model presets",
    )
    list_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )

    # 'status' command — show current configuration (S4, future)
    status_parser = subparsers.add_parser(
        "status",
        help="Show current Claude Code configuration (future)",
    )

    # 'doctor' command — verify configuration (S6, future)
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Verify that the configuration works (future)",
    )

    return parser


def cmd_list_models(args: argparse.Namespace) -> int:
    """
    Handle the 'list' command — show available model presets.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success)
    """
    presets = list_available_presets()

    if args.format == "json":
        import json

        from zai_python_helper.constants import ZAI_MODEL_PRESETS

        print(json.dumps(ZAI_MODEL_PRESETS, indent=2))
    else:
        print("Available Z.ai model presets:")
        print()
        for preset in presets:
            from zai_python_helper.constants import get_preset_model

            config = get_preset_model(preset)
            print(f"  {preset}:")
            print(f"    ID: {config['model_id']}")
            print(f"    Name: {config['name']}")
            print(f"    Description: {config['description']}")
            print(f"    Maps to: {config['anthropic_alias']}")
            print()

    return 0


def cmd_use(args: argparse.Namespace) -> int:
    """
    Handle the 'use' command — switch configuration.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    if args.dry_run:
        print("[DRY RUN] Would make the following changes:")
        # TODO: Show diff of planned changes
        return 0

    if args.provider == "default":
        print("Switching to default (Anthropic) provider...")
        # TODO: Implement restore from ownership journal
        print("Not yet implemented — see issue #4 (ownership journal)")
        return 1

    if args.provider == "zai":
        mode = ModelMode(args.mode)

        if args.dry_run:
            print(f"Would configure Z.ai with mode: {mode.value}")
        else:
            print(f"Configuring Z.ai with mode: {mode.value}")

        # TODO: Implement actual configuration
        # 1. Resolve API key (from env or prompt)
        # 2. Build ProviderSpec
        # 3. Generate PatchPlan
        # 4. Apply changes with ownership journal

        print("Not yet fully implemented — see epic #1")
        return 1

    return 0


def main() -> int:
    """
    Main entry point for the CLI.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "list":
            return cmd_list_models(args)
        elif args.command == "use":
            return cmd_use(args)
        elif args.command == "status":
            print("Status command not yet implemented — see issue #5")
            return 1
        elif args.command == "doctor":
            print("Doctor command not yet implemented — see issue #6")
            return 1
        else:
            print(f"Unknown command: {args.command}")
            return 1
    except Exception as e:
        if args.debug:
            import traceback

            traceback.print_exc()
        else:
            print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
