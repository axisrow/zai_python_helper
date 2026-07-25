"""Phase-1 parity: our ``use zai`` config output must match the upstream tool.

Issue #17. This is the FULL behavior-parity test for the Claude Code config path,
built on the headless equivalent of the upstream's interactive ``enter
claude-code`` flow pinned by research issue #9:

    upstream:  chelper auth glm_coding_plan_global <key>   # validates + saves
               chelper auth reload claude                   # patches Claude Code
    ours:      zai-python-helper use zai --mode original --api-key <key>

Both run INSIDE the parity image (``zai-parity:smoke``) in a fresh isolated HOME
per tool; we then diff the resulting ``~/.claude/settings.json`` env block and
``~/.claude.json``. Phase-1 is strict byte-for-byte after token redaction.

Why the upstream run needs two fixtures baked into the image (see
``docker/parity/Dockerfile``):

- ``claude-shim`` on PATH: ``chelper auth reload claude`` probes for a ``claude``
  binary; a no-op shim satisfies the probe without shipping the real package.
- ``fetch-mock.cjs`` via ``NODE_OPTIONS=--require``: the upstream VALIDATES the
  key over the network (``GET api.z.ai/api/coding/paas/v4/models``) and refuses
  to save on non-200. The shim returns ``200 []`` for that one call so a FAKE
  key validates fully offline — no real Z.ai endpoint is ever hit, no real key
  is ever used.

Out of parity scope (Phase-2 / our own features — asserted elsewhere, not here):
``.zshrc`` (the upstream's headless path does not write it), MCP install,
``use default``/restore (the upstream has no headless restore), our model modes,
the ownership journal, and the headless CLI surface.

If no Docker daemon is available, the parity assertions SKIP (not fail) — the
``normalize_text`` contract is still unit-tested directly so the module always
asserts something.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "parity" / "Dockerfile"
IMAGE_TAG = "zai-parity:smoke"

# Absolute path of the fetch-mock shim INSIDE the image (set by the Dockerfile).
FETCH_MOCK_IN_IMAGE = "/opt/parity/fetch-mock.cjs"

# A clearly-fake, hardcoded token. Never a real key, never read from the
# environment. The fetch-mock guarantees it is never sent to a real endpoint.
FAKE_TOKEN = "sk-fake-zai-parity-test-key-do-not-use"

# Container PATH: npm bin (chelper/coding-helper/node), our claude-shim, and
# python — all live under /usr/local/bin in the runtime stage.
_CONTAINER_PATH = "/usr/local/bin:/usr/bin:/bin"

# Per ``docker run`` timeout (seconds). The upstream validates+reloads; ours is a
# single in-process-equivalent command. 120s is generous headroom over cold image
# start + npm boot.
_RUN_TIMEOUT = 120

# Run the container as the host user so files written into the mounted HOME are
# readable by the (non-root) pytest process on Linux CI. See _docker_run.
_HOST_UID = str(os.getuid())
_HOST_GID = str(os.getgid())


# --------------------------------------------------------------------------- #
# Docker runner selection — mirrors tests/parity/test_version_format.py so the
# two parity modules behave identically when docker is absent.
# --------------------------------------------------------------------------- #
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
    """Build the parity image if it is missing (CI smoke job builds it first)."""
    if _image_present():
        return
    subprocess.run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE_TAG, str(REPO_ROOT)],
        check=True,
    )


# --------------------------------------------------------------------------- #
# normalize(): redact the token, then canonically re-serialize.
#
# Comparing the RE-SERIALIZED text (not just parsed dicts) is deliberate: it is
# type-robust. ``json.loads`` round-trips JSON ``1`` as Python ``int 1`` and
# ``"1"`` as ``str "1"``; ``json.dumps`` then renders them differently. So an
# int-vs-string drift (the exact regression that once existed on
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC) survives normalize and fails the
# equality — which is the whole point. Whitespace/indent differences also
# collapse, so only semantic value/key/order drift can fail the test.
# --------------------------------------------------------------------------- #
def _redact(doc: dict) -> dict:
    """Non-mutating copy with ``env.ANTHROPIC_AUTH_TOKEN`` -> ``<REDACTED>``."""
    out = dict(doc)
    env = out.get("env")
    if isinstance(env, dict) and "ANTHROPIC_AUTH_TOKEN" in env:
        env = dict(env)
        env["ANTHROPIC_AUTH_TOKEN"] = "<REDACTED>"
        out["env"] = env
    return out


def normalize_text(raw: str | None) -> str:
    """Redact the token and canonically re-serialize a JSON config document.

    ``sort_keys=False`` preserves insertion order, matching our backend
    (``json.dumps(indent=2)``) and the upstream's key order. Returns ``""`` for a
    missing file so an absent-vs-present mismatch surfaces as a clear diff.
    """
    if not raw:
        return ""
    doc = json.loads(raw)
    redacted = _redact(doc)
    return json.dumps(redacted, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Tool runners: each drives `docker run` with a fresh mounted HOME.
# --------------------------------------------------------------------------- #
def _read_files(home: Path) -> dict[str, str | None]:
    """Read the two parity-relevant files back from the mounted host HOME.

    The ``-v <home>:<home>`` mount makes container writes visible on the host at
    the same absolute path, so we read them here directly. Missing files -> None.
    """
    settings = home / ".claude" / "settings.json"
    claude_json = home / ".claude.json"
    return {
        "settings.json": settings.read_text() if settings.exists() else None,
        ".claude.json": claude_json.read_text() if claude_json.exists() else None,
    }


def _docker_run(home: Path, extra_env: dict[str, str], command: list[str]) -> None:
    """Run ``command`` inside the parity image with an isolated mounted HOME.

    Asserts exit 0; the last 500 chars of stderr are attached to the failure so a
    misconfigured fixture is debuggable. The token is fake + the fetch-mock
    guarantees no real key ever leaves the container, so stderr is safe to show.

    ``--user $(id -u):$(id -g)`` makes the container run as the HOST user so the
    files it writes into the mounted HOME are owned by the host pytest process.
    Without it, on Linux CI the container (running as root) creates root-owned
    files that the non-root pytest runner then cannot read (PermissionError). On
    macOS Docker Desktop the UID is mapped either way, so this is a no-op there.
    """
    home_abs = str(home.resolve())
    env_args: list[str] = []
    for key, value in {"HOME": home_abs, "PATH": _CONTAINER_PATH, **extra_env}.items():
        env_args += ["-e", f"{key}={value}"]

    res = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{_HOST_UID}:{_HOST_GID}",
            *env_args,
            "-v",
            f"{home_abs}:{home_abs}",
            IMAGE_TAG,
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT,
    )
    assert res.returncode == 0, (
        f"`{' '.join(command)}` exited {res.returncode} in the parity image.\n"
        f"stderr (tail):\n{res.stderr[-500:]}"
    )


def run_original(home: Path) -> dict[str, str | None]:
    """Drive the upstream's headless path: auth set, then reload claude.

    Two ``docker run`` invocations share the mounted HOME so the saved plan/key
    state from step 1 persists into step 2. ``NODE_OPTIONS=--require`` loads the
    fetch-mock so the validation call succeeds offline; the claude-shim on PATH
    satisfies the reload's tool-presence probe.
    """
    node_env = {"NODE_OPTIONS": f"--require {FETCH_MOCK_IN_IMAGE}"}
    _docker_run(
        home,
        node_env,
        ["chelper", "auth", "glm_coding_plan_global", FAKE_TOKEN],
    )
    _docker_run(home, node_env, ["chelper", "auth", "reload", "claude"])
    return _read_files(home)


def run_ours(home: Path) -> dict[str, str | None]:
    """Drive OUR tool's ``use zai --mode original`` in the parity image."""
    _docker_run(
        home,
        {},
        [
            "python",
            "-m",
            "zai_python_helper",
            "use",
            "zai",
            "--mode",
            "original",
            "--api-key",
            FAKE_TOKEN,
        ],
    )
    return _read_files(home)


# --------------------------------------------------------------------------- #
# Module fixture: run BOTH tools once, share the results. Skips (not fails) when
# docker is unavailable so a no-daemon CI matrix still passes cleanly.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def parity_pair(tmp_path_factory):
    """Run both tools in fresh isolated HOMEs; return (original, ours) file maps."""
    if not _docker_available():
        pytest.skip("no docker daemon; full parity test requires the image")
    _ensure_image()
    home_original = tmp_path_factory.mktemp("home_original")
    home_ours = tmp_path_factory.mktemp("home_ours")
    return run_original(home_original), run_ours(home_ours)


def _fail_with_diff(label: str, original: str | None, ours: str | None) -> None:
    """Print a redacted unified diff and fail. Token is already redacted by normalize."""
    o = normalize_text(original).splitlines()
    n = normalize_text(ours).splitlines()
    diff = "\n".join(
        difflib.unified_diff(
            o,
            n,
            fromfile=f"original/{label}",
            tofile=f"ours/{label}",
            lineterm="",
        )
    )
    pytest.fail(f"Phase-1 parity drift in {label}:\n{diff}")


# --------------------------------------------------------------------------- #
# Phase-1 parity assertions.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_use_zai_settings_matches_original(parity_pair):
    """Our ``settings.json`` env block must match the upstream's byte-for-byte.

    After token redaction + canonical re-serialization, the two documents must be
    identical: same keys (ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, API_TIMEOUT_MS,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC), same VALUES, same VALUE TYPES
    (int vs string is caught), same order. Any drift fails with a unified diff.
    """
    original, ours = parity_pair
    if normalize_text(original["settings.json"]) != normalize_text(
        ours["settings.json"]
    ):
        _fail_with_diff("settings.json", original["settings.json"], ours["settings.json"])


@pytest.mark.smoke
def test_use_zai_claude_json_matches_original(parity_pair):
    """Our ``.claude.json`` must equal the upstream's ``{"hasCompletedOnboarding": true}``."""
    original, ours = parity_pair
    if normalize_text(original[".claude.json"]) != normalize_text(ours[".claude.json"]):
        _fail_with_diff(".claude.json", original[".claude.json"], ours[".claude.json"])


# --------------------------------------------------------------------------- #
# normalize() contract — unit-tested directly so the module asserts something
# even when docker is absent, and to HONESTLY prove the parity comparison catches
# drift (the "remove API_TIMEOUT_MS -> RED" acceptance criterion).
# --------------------------------------------------------------------------- #
def test_normalize_catches_value_type_drift():
    """An int-vs-string value drift must survive normalize and fail equality.

    This is the exact regression that once existed on
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC (upstream int ``1`` vs our string
    ``"1"``). Proving drift survives ``normalize_text`` proves the main parity
    test fails on it — by direct analogy, removing a key or changing any value
    also fails.
    """
    golden = '{"env": {"ANTHROPIC_AUTH_TOKEN": "tok", "X": 1}}'
    drifted = '{"env": {"ANTHROPIC_AUTH_TOKEN": "tok", "X": "1"}}'
    assert normalize_text(golden) != normalize_text(drifted)


def test_normalize_catches_missing_key_drift():
    """A missing key (e.g. API_TIMEOUT_MS removed) must survive normalize."""
    full = '{"env": {"A": "1", "B": "2"}}'
    missing = '{"env": {"A": "1"}}'
    assert normalize_text(full) != normalize_text(missing)


def test_normalize_redacts_token():
    """The fake token must NEVER appear in normalized output (diffs/logs)."""
    raw = json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN, "X": 1}})
    out = normalize_text(raw)
    assert FAKE_TOKEN not in out
    assert "<REDACTED>" in out


def test_normalize_empty_is_empty():
    """A missing file normalizes to '' so absence surfaces as a diff, not a crash."""
    assert normalize_text(None) == ""
    assert normalize_text("") == ""
