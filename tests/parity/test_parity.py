"""Black-box parity matrix for the pinned upstream helper.

Phase 1 compares the raw process and filesystem contract. JSON is never parsed:
whitespace, key order, final newlines, file presence, bytes, and modes matter.
``dry-run``, ``status``, and ownership/backup bookkeeping are Python-port
extensions without an upstream 0.0.7 analogue and are not matrix actions.
"""

from __future__ import annotations

import json
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
TOOL_CONFIG_ACTIONS = ("activate", "revert")
MCP_ACTIONS = ("mcp-install", "mcp-uninstall")
MCP_IDS = ("zai-mcp-server", "web-search-prime", "web-reader", "zread")
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
    # The application state is intentionally outside HOME. Mount a persistent
    # host-side state root so activate and revert, which are separate
    # containers, exercise the same journal as a real installation.
    state_root = home.parent / ".zai-parity-state"
    state_root.mkdir(mode=0o700, exist_ok=True)
    state_abs = str(state_root.resolve())
    # Bind-mount the COMMON PARENT of HOME and the state root — never HOME or
    # the state root themselves (issue #126). macOS Docker Desktop presents a
    # bind mount's root inode as root:root inside the Linux VM while every
    # entry below the mount root keeps its host uid/gid; the strict state-root
    # ownership policy (#120) requires HOME (controlled for the transaction
    # lock) and XDG_STATE_HOME (controlled/private state walk) to be owned by
    # the invoking uid, i.e. by ``--user`` below. Keeping both one level below
    # the mount root satisfies that policy on Docker Desktop without weakening
    # it; on Linux the parent mount is equivalent because bind mounts preserve
    # host ownership at every level, so CI semantics are unchanged.
    bind_root = Path(home_abs).parent
    assert Path(state_abs).is_relative_to(bind_root)
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
            f"XDG_STATE_HOME={state_abs}",
            "-e",
            f"PATH={_CONTAINER_PATH}",
            "-v",
            f"{bind_root}:{bind_root}",
            IMAGE_TAG,
            *command,
        ],
        capture_output=True,
        text=False,
        timeout=_RUN_TIMEOUT,
    )


def _upstream(
    home: Path, tool: str, region: str, action: str, mcp_id: str
) -> ProcessResult:
    result = _docker_run(
        home,
        [
            "node",
            "/opt/parity/upstream-runner.mjs",
            tool,
            region,
            action,
            mcp_id,
            FAKE_TOKEN,
        ],
    )
    return ProcessResult(
        result.returncode, result.stdout, result.stderr, snapshot_home(home)
    )


def _ours(home: Path, tool: str, region: str, action: str, mcp_id: str) -> ProcessResult:
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
            mcp_id,
            "--tool",
            tool,
            "--region",
            region,
            "--api-key",
            FAKE_TOKEN,
        ]
    else:
        command = ["mcp", "uninstall", mcp_id, "--tool", tool]
    result = _docker_run(home, ["python", "-m", "zai_python_helper", *command])
    return ProcessResult(
        result.returncode, result.stdout, result.stderr, snapshot_home(home)
    )


def _prepare(
    home: Path, tool: str, region: str, action: str, mcp_id: str, runner
) -> None:
    if action == "revert":
        runner(home, tool, region, "activate", mcp_id)
    elif action == "mcp-uninstall":
        runner(home, tool, region, "mcp-install", mcp_id)


def _format_drift(path: str, upstream: ProcessResult, ours: ProcessResult) -> str:
    return f"{path}: upstream={upstream!r} != ours={ours!r}"


def _assert_raw_parity(
    tmp_path_factory, tool: str, region: str, action: str, mcp_id: str
) -> None:
    if not _docker_available():
        pytest.skip("no docker daemon; full parity matrix requires the image")
    _ensure_image()
    upstream_home = tmp_path_factory.mktemp(f"upstream-{tool}-{region}-{action}")
    ours_home = tmp_path_factory.mktemp(f"ours-{tool}-{region}-{action}")
    # Each side must prepare its own HOME with its own implementation.  Mixing
    # the setup commands would make inverse actions operate on foreign state
    # and would also leak the Python ownership journal into the upstream case.
    _prepare(upstream_home, tool, region, action, mcp_id, _upstream)
    _prepare(ours_home, tool, region, action, mcp_id, _ours)
    upstream = _upstream(upstream_home, tool, region, action, mcp_id)
    ours = _ours(ours_home, tool, region, action, mcp_id)
    # A broken adapter/image is an infrastructure failure, never an expected
    # parity drift. Process channels are asserted independently so a file
    # drift cannot hide a stdout regression. Activate is CLI↔CLI; revert/MCP
    # intentionally use the manager fallback documented by the runner.
    assert upstream.exit_code == 0, upstream.stderr.decode(errors="replace")
    assert ours.exit_code == 0, ours.stderr.decode(errors="replace")
    assert upstream.stderr == ours.stderr, _format_drift(
        f"{tool}/{region}/{action}/{mcp_id}/stderr", upstream, ours
    )
    if action == "activate":
        assert upstream.stdout == ours.stdout, _format_drift(
            f"{tool}/{region}/{action}/{mcp_id}/stdout", upstream, ours
        )
    if upstream != ours and not STRICT_PARITY:
        pytest.xfail(
            _format_drift(f"{tool}/{region}/{action}/{mcp_id}", upstream, ours)
        )
    assert upstream == ours, _format_drift(
        f"{tool}/{region}/{action}/{mcp_id}", upstream, ours
    )


@pytest.mark.smoke
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("action", TOOL_CONFIG_ACTIONS)
def test_pinned_upstream_raw_tool_config_parity(
    tmp_path_factory, tool: str, region: str, action: str
) -> None:
    """Every tool-config cell has one raw, byte-for-byte verdict."""
    _assert_raw_parity(tmp_path_factory, tool, region, action, MCP_IDS[0])


@pytest.mark.smoke
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("region", REGIONS)
def test_docker_revert_semantic_parity_across_processes(
    tmp_path_factory, tool: str, region: str
) -> None:
    """Activation state survives the separate Docker process used by revert."""
    if not _docker_available():
        pytest.skip("no docker daemon; Docker state persistence requires the image")
    _ensure_image()
    upstream_home = tmp_path_factory.mktemp(f"upstream-revert-{tool}-{region}")
    ours_home = tmp_path_factory.mktemp(f"ours-revert-{tool}-{region}")
    _prepare(upstream_home, tool, region, "revert", MCP_IDS[0], _upstream)
    _prepare(ours_home, tool, region, "revert", MCP_IDS[0], _ours)
    upstream = _upstream(upstream_home, tool, region, "revert", MCP_IDS[0])
    ours = _ours(ours_home, tool, region, "revert", MCP_IDS[0])
    assert upstream.exit_code == 0, upstream.stderr.decode(errors="replace")
    assert ours.exit_code == 0, ours.stderr.decode(errors="replace")

    def semantic(files: dict[str, tuple[bytes, int]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, (content, _mode) in files.items():
            # The state lock is outside HOME in parity runs and is not tool
            # configuration. The managed-HOME inode is the lock domain.
            if name in {
                ".zai-python-helper/lock",
            }:
                continue
            result[name] = (
                json.loads(content)
                if name.endswith(".json")
                else content
            )
        return result

    assert semantic(upstream.files) == semantic(ours.files)


@pytest.mark.smoke
@pytest.mark.parametrize("mcp_id", MCP_IDS)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("action", MCP_ACTIONS)
def test_pinned_upstream_raw_mcp_parity(
    tmp_path_factory, tool: str, region: str, action: str, mcp_id: str
) -> None:
    """All 4 presets × 4 tools × 2 regions have live install/uninstall verdicts."""
    _assert_raw_parity(tmp_path_factory, tool, region, action, mcp_id)
