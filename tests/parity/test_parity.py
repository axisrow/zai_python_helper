"""Full-HOME Phase-1 parity for the upstream Claude Code configuration path.

The compared flows run in fresh, mounted ``HOME`` directories inside one Docker
image containing both tools:

    upstream: chelper auth glm_coding_plan_global <key>
              chelper auth reload claude
    ours:     zai-python-helper use zai --mode original --api-key <key>

The assertion snapshots every regular file created below ``HOME``.  The file
set is deliberately closed: an unlisted extra or missing artifact fails.  Four
intentional, directional exceptions are documented in the constants below:

* upstream-only ``.chelper/config.yaml`` persists the upstream auth plan/key;
* ours-only ``.zshrc`` is the headless-first shell warning block (Phase 2);
* ours-only ownership journal and lock implement ADR-004/ADR-005 (Phase 2).

The two common files use a documented normalized-JSON contract: parse UTF-8,
replace the hard-coded fake token, then serialize with sorted object keys.
Values, JSON types, array order, and key presence are parity-significant;
object-key order, whitespace, and final newlines are not.  No real token is
read or sent: the image's fetch mock validates the fake token offline.

``default``, ``select``, and ``custom`` are intentionally not driven here:
upstream 0.0.7 has no equivalent model-selection surface.  They are Phase-2
extensions covered by this project's model-mode tests, not invented upstream
comparisons.
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
_CONTAINER_LABEL = f"ao.session={os.environ.get('AO_SESSION_ID', 'parity-tests')}"


# Full expected artifact sets. These are intentionally exact path lists, not
# globs: any newly-created file on either side must make the parity test fail.
COMMON_HOME_FILES = frozenset(
    {
        ".claude/settings.json",
        ".claude.json",
    }
)
UPSTREAM_ONLY_HOME_FILES = {
    ".chelper/config.yaml": "upstream persists its saved auth plan and token",
}
OURS_ONLY_HOME_FILES = {
    ".zshrc": "Phase-2 headless-first shell warning block",
    ".zai-python-helper/ownership.json": "Phase-2 ADR-004 ownership journal",
    ".zai-python-helper/lock": "Phase-2 ADR-005 PatchPlan process lock",
}
UPSTREAM_EXPECTED_HOME_FILES = COMMON_HOME_FILES | frozenset(UPSTREAM_ONLY_HOME_FILES)
OURS_EXPECTED_HOME_FILES = COMMON_HOME_FILES | frozenset(OURS_ONLY_HOME_FILES)


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
    """Build the parity image if it is missing (CI parity job builds it first)."""
    if _image_present():
        return
    subprocess.run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE_TAG, str(REPO_ROOT)],
        check=True,
    )


# --------------------------------------------------------------------------- #
# Normalized-JSON comparison and full-HOME snapshots.
# --------------------------------------------------------------------------- #
def _redact_fake_token(value):
    """Return a JSON-compatible value with the fixed fake token redacted."""
    if isinstance(value, dict):
        return {key: _redact_fake_token(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_fake_token(item) for item in value]
    if isinstance(value, str):
        return value.replace(FAKE_TOKEN, "<REDACTED>")
    return value


def normalize_json(raw: bytes | None) -> str:
    """Canonicalize JSON after token redaction, ignoring only formatting/order."""
    if raw is None:
        return ""
    value = json.loads(raw.decode("utf-8"))
    return json.dumps(
        _redact_fake_token(value),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"


def snapshot_home(home: Path) -> dict[str, bytes]:
    """Return every regular file below ``home`` keyed by its POSIX-relative path."""
    return {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def _file_set_drift(actual: set[str], expected: frozenset[str]) -> str | None:
    """Describe missing/unexpected paths, or return ``None`` for an exact set."""
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if not missing and not unexpected:
        return None
    parts: list[str] = []
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    if unexpected:
        parts.append(f"unexpected: {', '.join(unexpected)}")
    return "; ".join(parts)


def _assert_expected_file_set(
    tool: str, snapshot: dict[str, bytes], expected: frozenset[str]
) -> None:
    drift = _file_set_drift(set(snapshot), expected)
    if drift:
        pytest.fail(f"Phase-1 parity artifact-set drift for {tool}: {drift}")


def _fail_with_diff(label: str, original: bytes | None, ours: bytes | None) -> None:
    """Print a token-redacted normalized-JSON unified diff and fail."""
    original_text = normalize_json(original)
    ours_text = normalize_json(ours)
    diff = "\n".join(
        difflib.unified_diff(
            original_text.splitlines(),
            ours_text.splitlines(),
            fromfile=f"original/{label}",
            tofile=f"ours/{label}",
            lineterm="",
        )
    )
    pytest.fail(f"Phase-1 normalized-JSON parity drift in {label}:\n{diff}")


# --------------------------------------------------------------------------- #
# Tool runners: each drives `docker run` with a fresh mounted HOME.
# --------------------------------------------------------------------------- #
def _docker_run(home: Path, extra_env: dict[str, str], command: list[str]) -> None:
    """Run ``command`` inside the parity image with an isolated mounted HOME."""
    home_abs = str(home.resolve())
    env_args: list[str] = []
    for key, value in {"HOME": home_abs, "PATH": _CONTAINER_PATH, **extra_env}.items():
        env_args += ["-e", f"{key}={value}"]

    res = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--label",
            _CONTAINER_LABEL,
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


def run_original(home: Path) -> dict[str, bytes]:
    """Drive upstream's headless auth+reload path and snapshot every HOME file."""
    node_env = {"NODE_OPTIONS": f"--require {FETCH_MOCK_IN_IMAGE}"}
    _docker_run(
        home,
        node_env,
        ["chelper", "auth", "glm_coding_plan_global", FAKE_TOKEN],
    )
    _docker_run(home, node_env, ["chelper", "auth", "reload", "claude"])
    return snapshot_home(home)


def run_ours(home: Path) -> dict[str, bytes]:
    """Drive our original-mode path and snapshot every HOME file."""
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
    return snapshot_home(home)


# --------------------------------------------------------------------------- #
# Module fixture: run BOTH tools once, share the snapshots. Skips (not fails)
# when Docker is unavailable so no-daemon runners report honestly.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def parity_pair(tmp_path_factory):
    if not _docker_available():
        pytest.skip("no docker daemon; full parity test requires the image")
    _ensure_image()
    home_original = tmp_path_factory.mktemp("home_original")
    home_ours = tmp_path_factory.mktemp("home_ours")
    return run_original(home_original), run_ours(home_ours)


# --------------------------------------------------------------------------- #
# Phase-1 parity assertions.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_use_zai_artifact_sets_match_the_closed_contract(parity_pair):
    """Both full-HOME snapshots contain only their explicitly allowed files."""
    original, ours = parity_pair
    _assert_expected_file_set("upstream", original, UPSTREAM_EXPECTED_HOME_FILES)
    _assert_expected_file_set("ours", ours, OURS_EXPECTED_HOME_FILES)


@pytest.mark.smoke
@pytest.mark.parametrize("path", sorted(COMMON_HOME_FILES))
def test_use_zai_common_files_match_upstream_normalized_json(parity_pair, path):
    """Common config files match after documented JSON normalization."""
    original, ours = parity_pair
    if normalize_json(original.get(path)) != normalize_json(ours.get(path)):
        _fail_with_diff(path, original.get(path), ours.get(path))


# --------------------------------------------------------------------------- #
# Pure contract tests: they run even without Docker and prove that normalization
# and the closed file-set guard reject the intended drift classes.
# --------------------------------------------------------------------------- #
def test_normalize_ignores_formatting_and_object_key_order():
    """Whitespace, trailing newlines, and object-key order are not significant."""
    assert normalize_json(b'{"env": {"A": 1, "B": 2}}') == normalize_json(
        b'{\n  "env": {"B": 2, "A": 1}\n}\n'
    )


def test_normalize_catches_value_type_drift():
    """An int-vs-string value drift survives JSON normalization."""
    assert normalize_json(b'{"env":{"X":1}}') != normalize_json(
        b'{"env":{"X":"1"}}'
    )


def test_normalize_catches_missing_key_drift():
    """A missing config key remains a normalized-JSON difference."""
    assert normalize_json(b'{"env":{"A":"1","B":"2"}}') != normalize_json(
        b'{"env":{"A":"1"}}'
    )


def test_normalize_preserves_array_order():
    """Array order remains significant under canonical object serialization."""
    assert normalize_json(b'{"items":[1,2]}') != normalize_json(
        b'{"items":[2,1]}'
    )


def test_normalize_redacts_fake_token_recursively():
    """The fake token cannot leak from nested JSON into diffs or logs."""
    raw = json.dumps({"items": [{"token": FAKE_TOKEN}], "other": "keep"}).encode()
    out = normalize_json(raw)
    assert FAKE_TOKEN not in out
    assert '"token": "<REDACTED>"' in out
    assert '"other": "keep"' in out


def test_normalize_empty_is_empty():
    assert normalize_json(None) == ""


def test_file_set_contract_rejects_unlisted_artifact():
    """A newly-created file cannot silently enter either side's allowlist."""
    actual = set(UPSTREAM_EXPECTED_HOME_FILES) | {".new-unlisted-artifact"}
    assert _file_set_drift(actual, UPSTREAM_EXPECTED_HOME_FILES) == (
        "unexpected: .new-unlisted-artifact"
    )


def test_file_set_contract_rejects_missing_artifact():
    """A required common or directional artifact cannot silently disappear."""
    actual = set(UPSTREAM_EXPECTED_HOME_FILES) - {".claude.json"}
    assert _file_set_drift(actual, UPSTREAM_EXPECTED_HOME_FILES) == (
        "missing: .claude.json"
    )
