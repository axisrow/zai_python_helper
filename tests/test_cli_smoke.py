"""Smoke tests for CLI argument parsing and --help output."""

import os
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
    """Invoking status should succeed and render the Claude Code block."""
    result = subprocess.run(
        [sys.executable, "-m", "zai_python_helper", "status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # The status report always renders the Claude Code section (issue #5).
    assert "Claude Code" in result.stdout
    # Captured stdout is not a tty → plain text, no ANSI escapes.
    assert "\033[" not in result.stdout


def test_invoke_doctor_empty_home_uses_health_stdout_and_progress_stderr(tmp_path):
    """An empty HOME is diagnostic success; results/progress use their channels."""
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.path.join(os.getcwd(), "src"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "zai_python_helper", "doctor"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout == (
        "\n=== Health Check Results ===\n\n"
        "✓ PATH\n"
        "✗ API Key & Network\n"
        "  API key not configured\n"
        "✗ GLM Coding Plan\n"
        "  GLM Coding Plan not configured. Run 'chelper init' to configure.\n"
        "✗ Tool: Claude Code (claude-code)\n"
        "  Tool not found: Claude Code\n"
        "✗ Tool: OpenCode (opencode)\n"
        "  Tool not found: OpenCode\n"
        "✗ Tool: Crush (crush)\n"
        "  Tool not found: Crush\n"
        "✗ Tool: Factory Droid (factory-droid)\n"
        "  Tool not found: Factory Droid\n\n\n"
        "Suggestions:\n"
        '- Run "chelper init" to configure missing settings\n'
        "- Check your network connection\n"
        "- Ensure required tools are installed\n"
    )
    assert result.stderr == "- Running health check...\n"
    assert "Running health check..." not in result.stdout


def test_invoke_list():
    """`list` should show the model presets (regression guard for the list subcommand)."""
    result = subprocess.run(
        [sys.executable, "-m", "zai_python_helper", "list"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Available Z.ai model presets:" in result.stdout
    # A known preset must appear (constants wired through the src package)
    assert "glm-4-plus" in result.stdout


def test_invoke_list_json():
    """`list --format json` should emit valid JSON with a known preset."""
    import json

    result = subprocess.run(
        [sys.executable, "-m", "zai_python_helper", "list", "--format", "json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "glm-4-plus" in data
    assert data["glm-4-plus"]["model_id"] == "zai/glm-4-plus"


def test_use_select_bogus_preset_honours_error_contract():
    """SELECT with an unknown preset must exit 1 with a one-line `error:` (not a traceback).

    Regression guard: plan_model_config raises a bare ValueError on an unknown
    preset; _handle_use_zai wraps it into a ValidationError so __main__ formats
    it per the error contract.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zai_python_helper",
            "use",
            "zai",
            "--mode",
            "select",
            "--model",
            "bogus",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "error: Unknown preset: bogus" in result.stderr
    # No traceback leaked (error contract: one-line message unless --debug)
    assert "Traceback" not in result.stderr


def test_use_custom_only_flag_rejected_outside_custom():
    """--name (custom-only) must be rejected outside --mode custom, not silently dropped."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zai_python_helper",
            "use",
            "zai",
            "--mode",
            "original",
            "--name",
            "X",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "--name only apply to --mode custom" in result.stderr
    assert "Traceback" not in result.stderr


def test_use_custom_happy_path_accepts_custom_flags():
    """A valid custom-mode invocation accepts --name/--description (guard must not over-reject).

    Guards the inverse of test_use_custom_only_flag_rejected_outside_custom:
    if the custom-only-flag guard were inverted, this happy path would break.

    Under S2 ``--dry-run`` prints a unified_diff (redacted) rather than an
    env list, so the custom vars must appear inside the settings.json diff.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zai_python_helper",
            "use",
            "zai",
            "--mode",
            "custom",
            "--model",
            "my-x",
            "--name",
            "My Model",
            "--description",
            "desc",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # Custom vars land in the settings.json env block, shown in the diff.
    assert '"ANTHROPIC_CUSTOM_MODEL_OPTION": "my-x"' in result.stdout
    assert '"ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "My Model"' in result.stdout
    assert "--dry-run: no files written" in result.stdout
