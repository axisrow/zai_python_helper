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


def _handle_use_zai(args: argparse.Namespace) -> int:
    """Make Z.ai the default provider (stub for S2)."""
    from zai_python_helper.paths import Paths

    paths = Paths.default()
    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("dry-run: would configure Z.ai provider")
        return 0
    print(f"Configured Z.ai provider (paths: {paths.state_dir})")
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
    use_sub.add_parser(
        "zai",
        help="make Z.ai the default",
        parents=[sub_flags],
    ).set_defaults(func=_handle_use_zai)
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
