"""
CLI entry point for zai_python_helper.

This module provides the command-line interface using argparse.
Per architecture, cli.py is thin — just arg parsing and output delegation.
"""

import argparse
import sys

from zai_python_helper import __version__
from zai_python_helper.constants import (
    list_available_presets,
)
from zai_python_helper.core.domain import ModelMode
from zai_python_helper.errors import ZaiPythonHelperError


def create_parser() -> argparse.ArgumentParser:
    """
    Create the main argument parser for zai-python-helper.

    Returns:
        Configured ArgumentParser instance
    """
    parser = argparse.ArgumentParser(
        prog="zai-python-helper",
        description="Connect Claude Code to Z.ai GLM Coding Plan and switch the default provider",
        epilog="MIT licensed. See https://github.com/axisrow/zai_python_helper",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed debugging information",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without making changes",
    )

    # Common flags shared by the root parser and all subcommands (so that
    # --debug works both before and after the subcommand).
    # Common flags shared by the root parser and all subcommands (so that
    # --debug works both before and after the subcommand). SUPPRESS keeps the
    # parent from overwriting the root parser's value when the flag is absent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show detailed debugging information",
    )

    subparsers = parser.add_subparsers(dest="cmd", help="Available commands")

    # 'use' command — switch configuration
    use_parser = subparsers.add_parser(
        "use",
        parents=[common],
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
        default=argparse.SUPPRESS,
        help="Show what would change without making changes",
    )

    # 'list' command — show available models
    list_parser = subparsers.add_parser(
        "list",
        parents=[common],
        help="List available Z.ai model presets",
    )
    list_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )

    # 'status' command — show current configuration
    subparsers.add_parser(
        "status",
        parents=[common],
        help="Show current Claude Code configuration",
    )

    # 'doctor' command — verify configuration (S6, future)
    subparsers.add_parser(
        "doctor",
        parents=[common],
        help="Verify that the configuration works (future)",
    )

    return parser


# Alias for compatibility with existing tests
build_parser = create_parser


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


def cmd_use(args: argparse.Namespace) -> int:
    """
    Handle the 'use' command — switch configuration.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    if args.provider == "default":
        print("Switching to default (Anthropic) provider...")
        # TODO: Implement restore from ownership journal
        print("Not yet implemented — see issue #4 (ownership journal)")
        return 1

    if args.provider == "zai":
        mode = ModelMode(args.mode)

        if args.dry_run:
            print(f"[DRY RUN] Would configure Z.ai with mode: {mode.value}")
            # TODO: Show diff of planned changes
            return 0

        print(f"Configuring Z.ai with mode: {mode.value}")

        # TODO: Implement actual configuration
        # 1. Resolve API key (from env or prompt)
        # 2. Build ProviderSpec
        # 3. Generate PatchPlan
        # 4. Apply changes with ownership journal

        print("Not yet fully implemented — see epic #1")
        return 1

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """
    Handle the 'status' command — show current configuration.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    print(f"Status: zai-python-helper v{__version__}")
    print(f"version: {__version__}")
    print()
    print("Configuration file patching is not yet implemented (see epic #1).")
    print("Model selection modes are available via 'use zai --mode ...'")
    return 0


def main() -> int:
    """
    Main entry point for the CLI.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    parser = create_parser()
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return 0

    try:
        if args.cmd == "list":
            return cmd_list_models(args)
        elif args.cmd == "use":
            return cmd_use(args)
        elif args.cmd == "status":
            return cmd_status(args)
        elif args.cmd == "doctor":
            print("Doctor command not yet implemented — see issue #6")
            return 1
        else:
            print(f"Unknown command: {args.cmd}")
            return 1
    except ZaiPythonHelperError as e:
        # Per error contract: one-line error message + exit 1
        # Full traceback only with --debug
        if args.debug:
            import traceback

            traceback.print_exc()
        else:
            print(f"error: {e}")
        return 1
    except Exception as e:
        # Unexpected exception
        if args.debug:
            import traceback

            traceback.print_exc()
        else:
            print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
