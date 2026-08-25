"""Phase-1 parity smoke: ``-v`` / ``--version`` FORMAT matches upstream.

Both tools must print a bare semver (no program-name prefix, no extra info) on
stdout, exit 0, with empty stderr. The version NUMBER differs (we are not the
upstream package), so the test normalizes the number away and asserts the
remaining FORMAT is identical.

This is a Phase-1 parity surface (see this package's docstring + issue #17).

Run strategy (auto-selected):
  - If a usable ``docker`` is present AND the parity image is available (or can
    be built on the fly), run BOTH tools inside the image. This is the most
    faithful: it exercises the real installed ``coding-helper`` binary.
  - Otherwise, fall back to subprocess: the upstream tool via ``npx`` (if node
    is installed) and ours via ``python -m``. This keeps the test meaningful in
    CI runners that have no Docker daemon.

If neither docker nor npx can reach the upstream tool, the upstream-asserting
tests skip (with a clear reason) rather than fail — the FORMAT helper below is
still unit-tested directly.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "parity" / "Dockerfile"
IMAGE_TAG = "zai-parity:smoke"
UPSTREAM_PACKAGE = "@z_ai/coding-helper@0.0.7"
_CONTAINER_LABEL = f"ao.session={os.environ.get('AO_SESSION_ID', 'parity-tests')}"

# A semver token (optionally with a leading 'v' and a pre-release/build suffix),
# anchored to word boundaries so it matches a standalone number AND a number
# embedded in ``prog 1.2.3``. Matches BOTH the upstream (``0.0.7``) and our
# (``0.1.0``) outputs, and is tolerant of future bumps (``1.2.3``, ``0.2.0-rc1``).
# The number is what we normalize AWAY; everything around it is the FORMAT
# under test. The bare-semver check below is the SAME pattern anchored to a
# whole line, so the two can never drift.
_SEMVER = r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?"
_SEMVER_RE = re.compile(rf"\b{_SEMVER}\b")
_BARE_SEMVER_RE = re.compile(rf"^{_SEMVER}$")


def normalize_version_format(output: str) -> str:
    """Return ``output`` with every semver token replaced by ``<version>``.

    The FORMAT under test is everything EXCEPT the version number(s). The
    upstream prints ``0.0.7\\n``; we print ``0.1.0\\n``. After normalization
    both become ``<version>\\n`` — identical format, different (normalized-away)
    numbers. A program-prefixed output (``prog 1.2.3``) becomes ``prog
    <version>`` so the prefix survives and correctly FAILS a bare-semver
    parity comparison.
    """
    return _SEMVER_RE.sub("<version>", output)


# --------------------------------------------------------------------------- #
# Unit-test the FORMAT helper directly (runs everywhere, no docker/npx needed).
# --------------------------------------------------------------------------- #
def test_normalize_bare_semver():
    assert normalize_version_format("0.0.7\n") == "<version>\n"
    assert normalize_version_format("0.1.0\n") == "<version>\n"
    assert normalize_version_format("1.2.3\n") == "<version>\n"
    assert normalize_version_format("v0.0.7\n") == "<version>\n"


def test_normalize_exposes_format_drift():
    """A program-prefixed output must NOT normalize to bare ``<version>``.

    If a tool ever printed ``chelper 0.0.7`` (program name + number), the
    normalized form keeps the prefix so the parity comparison fails — which is
    the whole point of a FORMAT check.
    """
    assert normalize_version_format("chelper 0.0.7\n") == "chelper <version>\n"


# --------------------------------------------------------------------------- #
# Upstream reachability: pick the available runner, or skip honestly.
# --------------------------------------------------------------------------- #
def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        # `docker info` exits non-zero when there is no daemon / permission.
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


def _npx_available() -> bool:
    return shutil.which("npx") is not None and shutil.which("node") is not None


def _ensure_image() -> None:
    """Build the parity image if it is missing (CI parity job calls this first)."""
    if _image_present():
        return
    subprocess.run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE_TAG, "."],
        cwd=str(REPO_ROOT),
        check=True,
    )


@pytest.fixture(scope="module")
def upstream_version_outputs():
    """Capture the upstream tool's ``-v`` / ``--version`` stdout, or skip.

    Returns a dict ``{"-v": str, "--version": str}`` of raw stdout (incl. the
    trailing newline). Picks Docker if usable, else npx; skips if neither can
    reach the upstream tool.
    """
    if _docker_available():
        _ensure_image()
        out: dict[str, str] = {}
        for flag in ("-v", "--version"):
            res = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--label",
                    _CONTAINER_LABEL,
                    IMAGE_TAG,
                    "coding-helper",
                    flag,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert res.stderr == "", (
                f"`coding-helper {flag}` wrote to stderr: {res.stderr!r}"
            )
            out[flag] = res.stdout
        return out

    if _npx_available():
        # Suppress npx/npm installer chatter while preserving the child tool's
        # stderr, which is itself a Phase-1 assertion below.
        env = dict(
            os.environ,
            NPX_INSTALL_FORCE="1",
            NPM_CONFIG_LOGLEVEL="silent",
        )
        out: dict[str, str] = {}
        for flag in ("-v", "--version"):
            res = subprocess.run(
                ["npx", "--yes", UPSTREAM_PACKAGE, flag],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if res.returncode != 0:
                pytest.skip(
                    f"npx could not reach upstream coding-helper ({flag}): "
                    f"exit {res.returncode}, stderr: {res.stderr[:200]}"
                )
            assert res.stderr == "", (
                f"`npx {UPSTREAM_PACKAGE} {flag}` wrote to stderr: {res.stderr!r}"
            )
            out[flag] = res.stdout
        return out

    pytest.skip("no docker daemon and no npx available; cannot reach upstream tool")


def _run_ours(flag: str) -> subprocess.CompletedProcess:
    """Invoke ``python -m zai_python_helper <flag>`` once and return the result."""
    return subprocess.run(
        [sys.executable, "-m", "zai_python_helper", flag],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def ours_version_outputs():
    """Our tool's ``-v`` / ``--version`` stdout, captured ONCE per module.

    Mirror of ``upstream_version_outputs``: caches both flags so the parametrized
    assertions below don't each spawn a fresh interpreter. Asserts the
    Phase-1 invariants (exit 0, empty stderr) here, so every consumer can treat
    the cached value as clean stdout.
    """
    out: dict[str, str] = {}
    for flag in ("-v", "--version"):
        res = _run_ours(flag)
        assert res.returncode == 0, f"`zai-python-helper {flag}` failed: {res.stderr}"
        assert res.stderr == "", f"`zai-python-helper {flag}` wrote to stderr: {res.stderr!r}"
        out[flag] = res.stdout
    return out


# --------------------------------------------------------------------------- #
# Phase-1 parity assertions.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_format_matches_upstream(flag, upstream_version_outputs, ours_version_outputs):
    """The FORMAT of our version output must match the upstream tool's.

    Phase-1 parity (issue #17): bare semver on stdout, exit 0, empty stderr —
    no program-name prefix, no extra info. The number is normalized away.
    """
    upstream_fmt = normalize_version_format(upstream_version_outputs[flag])
    ours_fmt = normalize_version_format(ours_version_outputs[flag])
    assert ours_fmt == upstream_fmt, (
        f"version FORMAT drift for `{flag}`: "
        f"upstream={upstream_fmt!r}, ours={ours_fmt!r}"
    )


@pytest.mark.smoke
@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_ours_version_is_bare_semver(flag, ours_version_outputs):
    """Our version output must itself be a bare semver (independent of upstream).

    Guards the Phase-1 format directly, so the test still asserts something
    even when the upstream runner is skipped: a regression that added a prefix
    (e.g. ``zai-python-helper 0.1.0``) is caught here too.
    """
    ours = ours_version_outputs[flag].rstrip("\n")
    assert _BARE_SEMVER_RE.match(ours), f"`{flag}` output is not a bare semver: {ours!r}"
