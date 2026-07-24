"""Main entry point for the ``zai-python-helper`` CLI.

This module provides the :func:`main` function that is invoked by the
``zai-python-helper`` console script and by ``python -m zai_python_helper``.
It enforces the error contract: :class:`ZaiPythonHelperError` is caught once
and formatted as ``error: <message>`` on stderr with exit 1, unless ``--debug``
is passed (full traceback).
"""

import argparse
import sys

from zai_python_helper.cli import build_parser
from zai_python_helper.errors import ZaiPythonHelperError


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI.

    Parses arguments, dispatches to the appropriate handler, and enforces the
    error contract. Returns an exit code (0 for success, non-zero for error).

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code (0 on success, 1 on :class:`ZaiPythonHelperError`,
        2 on argparse error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except ZaiPythonHelperError as e:
        # Expected error: one-line message + exit 1, no traceback.
        # Under --debug, re-raise so Python emits the full traceback.
        if getattr(args, "debug", False):
            raise
        print(f"error: {e}", file=sys.stderr)
        return 1
    except argparse.ArgumentError as e:
        # Argparse errors are already formatted by argparse; just exit.
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
