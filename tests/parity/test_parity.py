"""Black-box parity matrix for the pinned upstream helper.

Phase 1 compares the raw process and filesystem contract. JSON is never parsed:
whitespace, key order, final newlines, file presence, bytes, and modes matter.
``dry-run``, ``status``, and ownership/backup bookkeeping are Python-port
extensions without an upstream 0.0.7 analogue and are not matrix actions.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "parity" / "Dockerfile"
IMAGE_TAG = "zai-parity:smoke"
FAKE_TOKEN = "sk-fake-zai-parity-test-key-do-not-use"
_CONTAINER_PATH = "/usr/local/bin:/usr/bin:/bin"
_RUN_TIMEOUT = 120
_HOST_UID = str(os.getuid())
_HOST_GID = str(os.getgid())
_CONTAINER_LABEL = f"ao.session={os.environ.get('AO_SESSION_ID', 'parity-tests')}"
TOOLS = ("claude-code", "opencode", "crush", "factory-droid")
REGIONS = ("global", "china")
ACTIONS = ("activate", "revert", "mcp-install", "mcp-uninstall")
MCP_ID = "web-search-prime"
# CI records the raw drift while follow-up parity issues repair it. Set this
# locally (or in the parity workflow) to make drift a hard failure.
STRICT_PARITY = os.environ.get("ZAI_PARITY_STRICT") == "1"


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    files: dict[str, tuple[bytes, int]]


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        subprocess.run(
            [docker, "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def _image_present() -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", IMAGE_TAG],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _ensure_image() -> None:
    if not _image_present():
        subprocess.run(
            ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE_TAG, str(REPO_ROOT)],
            check=True,
        )


def snapshot_home(home: Path) -> dict[str, tuple[bytes, int]]:
    """Snapshot every regular HOME file, including its exact permission mode."""
    return {
        path.relative_to(home).as_posix(): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def _docker_run(home: Path, command: list[str]) -> subprocess.CompletedProcess[bytes]:
    home_abs = str(home.resolve())
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--label",
            _CONTAINER_LABEL,
            "--user",
            f"{_HOST_UID}:{_HOST_GID}",
            "-e",
            f"HOME={home_abs}",
            "-e",
            f"PATH={_CONTAINER_PATH}",
            "-v",
            f"{home_abs}:{home_abs}",
            IMAGE_TAG,
            *command,
        ],
        capture_output=True,
        text=False,
        timeout=_RUN_TIMEOUT,
    )


def _upstream(home: Path, tool: str, region: str, action: str) -> ProcessResult:
    result = _docker_run(
        home,
        [
            "node",
            "/opt/parity/upstream-runner.mjs",
            tool,
            region,
            action,
            MCP_ID,
            FAKE_TOKEN,
        ],
    )
    return ProcessResult(
        result.returncode, result.stdout, result.stderr, snapshot_home(home)
    )


def _ours(home: Path, tool: str, region: str, action: str) -> ProcessResult:
    our_tool = tool.replace("-", "_")
    if action == "activate":
        command = [
            "use",
            "zai",
            "--tool",
            our_tool,
            "--region",
            region,
            "--api-key",
            FAKE_TOKEN,
        ]
    elif action == "revert":
        command = ["use", "default", "--tool", our_tool, "--region", region]
    elif action == "mcp-install":
        command = [
            "mcp",
            "install",
            MCP_ID,
            "--tool",
            tool,
            "--region",
            region,
            "--api-key",
            FAKE_TOKEN,
        ]
    else:
        command = ["mcp", "uninstall", MCP_ID, "--tool", tool]
    result = _docker_run(home, ["python", "-m", "zai_python_helper", *command])
    return ProcessResult(
        result.returncode, result.stdout, result.stderr, snapshot_home(home)
    )


def _prepare(home: Path, tool: str, region: str, action: str, runner) -> None:
    if action == "revert":
        runner(home, tool, region, "activate")
    elif action == "mcp-uninstall":
        runner(home, tool, region, "mcp-install")


def _format_drift(path: str, upstream: ProcessResult, ours: ProcessResult) -> str:
    return f"{path}: upstream={upstream!r} != ours={ours!r}"


@pytest.mark.smoke
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("action", ACTIONS)
def test_pinned_upstream_raw_parity(
    tmp_path_factory, tool: str, region: str, action: str
) -> None:
    """Every tool × region × action cell has one raw, byte-for-byte verdict."""
    if not _docker_available():
        pytest.skip("no docker daemon; full parity matrix requires the image")
    _ensure_image()
    upstream_home = tmp_path_factory.mktemp(f"upstream-{tool}-{region}-{action}")
    ours_home = tmp_path_factory.mktemp(f"ours-{tool}-{region}-{action}")
    # Each side must prepare its own HOME with its own implementation.  Mixing
    # the setup commands would make inverse actions operate on foreign state
    # and would also leak the Python ownership journal into the upstream case.
    _prepare(upstream_home, tool, region, action, _upstream)
    _prepare(ours_home, tool, region, action, _ours)
    upstream = _upstream(upstream_home, tool, region, action)
    ours = _ours(ours_home, tool, region, action)
    # A broken adapter/image is an infrastructure failure, never an expected
    # parity drift. Raw mismatches are temporarily reported as xfail until the
    # dedicated follow-up issues (#79–#85) close the existing contract gaps.
    assert upstream.exit_code == 0, upstream.stderr.decode(errors="replace")
    assert ours.exit_code == 0, ours.stderr.decode(errors="replace")
    if upstream != ours and not STRICT_PARITY:
        pytest.xfail(_format_drift(f"{tool}/{region}/{action}", upstream, ours))
    assert upstream == ours, _format_drift(f"{tool}/{region}/{action}", upstream, ours)
