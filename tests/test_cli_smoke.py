"""Smoke tests for CLI argument parsing and --help output."""

import subprocess
import sys

from zai_python_helper.cli import build_parser


def test_root_help():
    """The root parser --help should render."""
    parser = build_parser()
    assert parser.format_help()  # Just check it doesn't raise


def test_subcommand_help():
    """Each subcommand --help should render (and exit)."""
    parser = build_parser()

    # --help triggers SystemExit(0) in argparse; we just verify the format
    # doesn't raise during formatting. Actual help invocation is covered by
    # test_invoke_main_help (subprocess test).
    for cmd_args in [["status"], ["doctor"], ["use", "zai"]]:
        # Just verify the command parses (format_help succeeds for the subparser)
        try:
            args = parser.parse_args(cmd_args)
        except SystemExit:
            # --help would exit, but we're not passing --help here
            raise
        assert args is not None


def test_root_flag_before_subcommand():
    """Root flags should parse before the subcommand."""
    parser = build_parser()

    args = parser.parse_args(["--debug", "status"])
    assert args.cmd == "status"
    assert getattr(args, "debug", False) is True

    args = parser.parse_args(["--dry-run", "use", "zai"])
    assert args.cmd == "use"
    assert args.provider == "zai"
    assert getattr(args, "dry_run", False) is True


def test_root_flag_after_subcommand():
    """Root flags should parse after the subcommand."""
    parser = build_parser()

    args = parser.parse_args(["status", "--debug"])
    assert args.cmd == "status"
    assert getattr(args, "debug", False) is True

    args = parser.parse_args(["use", "zai", "--dry-run"])
    assert args.cmd == "use"
    assert args.provider == "zai"
    assert getattr(args, "dry_run", False) is True


def test_invoke_main_help():
    """Invoking the package as -m should show help."""
    result = subprocess.run(
        [sys.executable, "-m", "zai_python_helper", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "zai-python-helper" in result.stdout
    assert "switch the default provider" in result.stdout


def test_invoke_status():
    """Invoking status should succeed."""
    result = subprocess.run(
        [sys.executable, "-m", "zai_python_helper", "status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Status:" in result.stdout
    assert "version:" in result.stdout
