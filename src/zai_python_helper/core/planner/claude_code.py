"""
Claude Code planning functions (PURE, per ADR-001).

These functions transform *parsed* config documents into a :class:`PatchPlan`
of file deltas. They never open a file, never read the environment, and never
prompt. The CLI layer parses the files (or supplies empty seeds) and passes
the parsed structures in; the IO layer (:mod:`zai_python_helper.backends`)
turns each delta into an atomic write.

Contract for each plan (ADR-005):

- ``plan_zai`` produces up to three deltas — ``settings.json`` (deep-merge
  of ``env``), ``.claude.json`` (``hasCompletedOnboarding``), ``.zshrc``
  (owned marker-fenced block).
- ``plan_default`` produces the inverse — drop the managed ``env`` keys from
  ``settings.json``, leave ``.claude.json`` untouched, remove the owned
  block from ``.zshrc``.
- Both are IDEMPOTENT: a second call on the post-state of the first yields a
  plan of all-NOOP deltas.

The set of environment variables we manage is fixed and named explicitly
below (not driven by a free-form dict) so ``plan_default`` is the exact
inverse of ``plan_zai`` — the two never drift.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.domain import ProviderSpec
from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.core.planner.models import plan_model_config
from zai_python_helper.regions import ZAI_ANTHROPIC_BASE_URL_BY_REGION, Region
from zai_python_helper.shell_block import (
    install_owned_block,
    owns_owned_block,
    remove_owned_block,
)

# ---------------------------------------------------------------------------
# Managed keys
# ---------------------------------------------------------------------------
#
# The exact, closed set of ``settings.json`` → ``env`` keys this tool owns.
# ``plan_zai`` sets the MANAGED_ZAI_KEYS (auth/url/timeout) plus whatever the
# model mode contributes, and REMOVES ``ANTHROPIC_API_KEY``. ``plan_default``
# removes every managed key (ZAI + model-mode) so the two are exact inverses.

# Always managed by ``use zai`` regardless of model mode.
MANAGED_ZAI_KEYS: tuple[str, ...] = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)

# Removed on ``use zai`` (Z.ai authenticates via AUTH_TOKEN, not API_KEY).
# ``plan_default`` does NOT re-add it — we never owned the user's prior key
# value, so we leave ``env`` free of it (consistent with "exact inverse of
# our activation": activation removed it, deactivation keeps it removed).
REMOVED_ON_ZAI_KEYS: tuple[str, ...] = ("ANTHROPIC_API_KEY",)


def base_url_for_region(region: Region) -> str:
    """Return the Z.ai Anthropic-compatible base URL for ``region``.

    Pure lookup into the static region→URL map. Raises if the region is
    somehow unknown (defensive; the enum makes this unreachable today).
    """
    try:
        return ZAI_ANTHROPIC_BASE_URL_BY_REGION[region]
    except KeyError as e:  # pragma: no cover - enum-closed, unreachable
        raise ValueError(f"Unknown region: {region!r}") from e


def _managed_model_keys(provider_spec: ProviderSpec) -> tuple[str, ...]:
    """The env keys the given model mode contributes (beyond MANAGED_ZAI_KEYS).

    Computed by asking ``plan_model_config`` for the mode's env and removing
    the always-managed keys. This keeps the managed set in sync with whatever
    ``models.py`` decides — there is no second hand-maintained list that
    could drift.
    """
    mode_env = plan_model_config(provider_spec)
    always = set(MANAGED_ZAI_KEYS)
    return tuple(sorted(k for k in mode_env if k not in always))


# ---------------------------------------------------------------------------
# settings.json transform
# ---------------------------------------------------------------------------


def _plan_settings_doc(
    settings_doc: dict[str, Any] | None,
    *,
    region: Region,
    provider_spec: ProviderSpec,
    auth_token: str,
) -> dict[str, Any]:
    """Return the desired ``settings.json`` document after ``use zai``.

    Deep-merges the Z.ai ``env`` over the existing ``env`` (foreign keys
    survive), and removes ``ANTHROPIC_API_KEY`` from ``env`` (Z.ai
    authenticates via AUTH_TOKEN). Does NOT mutate the input.
    """
    doc: dict[str, Any] = dict(settings_doc) if settings_doc else {}
    env: dict[str, Any] = dict(doc.get("env") or {})

    # Build the Z.ai env contribution: auth token, base URL (region), plus
    # whatever the selected model mode contributes. plan_model_config already
    # includes API_TIMEOUT_MS + CLAUDE_CODE_DISABLE_* + (its own copy of)
    # ANTHROPIC_BASE_URL; we overwrite the base URL with the region URL last
    # so the canonical source is base_url_for_region, not models.py.
    mode_env = plan_model_config(provider_spec)
    zai_env: dict[str, str] = {
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "ANTHROPIC_BASE_URL": base_url_for_region(region),
    }
    zai_env.update(mode_env)
    zai_env["ANTHROPIC_BASE_URL"] = base_url_for_region(region)

    env.update(zai_env)
    for removed in REMOVED_ON_ZAI_KEYS:
        env.pop(removed, None)

    new_doc = dict(doc)
    new_doc["env"] = env
    return new_doc


def _plan_default_settings_doc(
    settings_doc: dict[str, Any] | None,
    *,
    provider_spec: ProviderSpec,
) -> dict[str, Any]:
    """Return the desired ``settings.json`` document after ``use default``.

    Removes every managed key (always-managed ZAI keys + model-mode keys)
    from ``env``, preserving foreign keys. Drops ``env`` entirely if it
    becomes empty. ``.claude.json`` is intentionally NOT touched.
    """
    doc: dict[str, Any] = dict(settings_doc) if settings_doc else {}
    env: dict[str, Any] = dict(doc.get("env") or {})

    managed = set(MANAGED_ZAI_KEYS) | set(_managed_model_keys(provider_spec))
    for key in managed:
        env.pop(key, None)

    new_doc = dict(doc)
    if env:
        new_doc["env"] = env
    else:
        # env is now empty — drop the key rather than leaving ``"env": {}``.
        new_doc.pop("env", None)
    return new_doc


# ---------------------------------------------------------------------------
# .claude.json transform
# ---------------------------------------------------------------------------


def _plan_claude_json_doc(
    claude_json_doc: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the desired ``.claude.json`` after ``use zai``.

    Sets ``hasCompletedOnboarding`` to ``True`` ONLY if it is absent or falsy;
    if the user already completed onboarding this is a no-op. Never touches
    any other key (foreign keys round-trip untouched).
    """
    doc: dict[str, Any] = dict(claude_json_doc) if claude_json_doc else {}
    if doc.get("hasCompletedOnboarding") is True:
        return doc  # already set — no-op
    new_doc = dict(doc)
    new_doc["hasCompletedOnboarding"] = True
    return new_doc


# ---------------------------------------------------------------------------
# .zshrc transform (owned marker-fenced block, ADR-003)
# ---------------------------------------------------------------------------


def _plan_zshrc_install(zshrc_text: str) -> tuple[str, DeltaKind]:
    """Desired ``.zshrc`` text + delta kind for ``use zai``.

    Installs the owned block if absent (NOOP otherwise). Foreign lines are
    never touched — :func:`install_owned_block` appends only.
    """
    if owns_owned_block(zshrc_text):
        return zshrc_text, DeltaKind.NOOP
    return install_owned_block(zshrc_text), DeltaKind.WRITE_TEXT


def _plan_zshrc_remove(zshrc_text: str) -> tuple[str, DeltaKind]:
    """Desired ``.zshrc`` text + delta kind for ``use default``.

    Removes ONLY the owned block (NOOP if absent). Foreign lines are never
    touched.
    """
    if not owns_owned_block(zshrc_text):
        return zshrc_text, DeltaKind.NOOP
    return remove_owned_block(zshrc_text), DeltaKind.WRITE_TEXT


# ---------------------------------------------------------------------------
# Public planning API
# ---------------------------------------------------------------------------


def plan_zai(
    provider_spec: ProviderSpec,
    region: Region,
    *,
    settings_doc: dict[str, Any] | None = None,
    claude_json_doc: dict[str, Any] | None = None,
    zshrc_text: str = "",
    auth_token: str,
) -> PatchPlan:
    """Plan the ``use zai`` activation for Claude Code.

    PURE: takes parsed documents + the resolved auth token (resolved by the
    caller in the IO layer, never here) and returns a :class:`PatchPlan` of
    up to three deltas. Each delta is a NOOP when the file already matches
    the desired state, so a second ``use zai`` on the post-state yields an
    all-NOOP plan (idempotent).

    Args:
        provider_spec: Domain spec carrying the model mode (and select/custom
            details). Drives which model-mode env keys are managed.
        region: Selects the Z.ai base URL (global vs china).
        settings_doc: Parsed ``settings.json`` (or ``None`` if absent).
        claude_json_doc: Parsed ``.claude.json`` (or ``None`` if absent).
        zshrc_text: Raw ``.zshrc`` text (or ``""`` if absent).
        auth_token: The Z.ai auth token for ``ANTHROPIC_AUTH_TOKEN``.
            Resolved by the caller — never read from env here.

    Returns:
        A :class:`PatchPlan` with ordered deltas for the three managed files.
    """
    desired_settings = _plan_settings_doc(
        settings_doc,
        region=region,
        provider_spec=provider_spec,
        auth_token=auth_token,
    )
    settings_kind = (
        DeltaKind.NOOP if settings_doc == desired_settings else DeltaKind.WRITE_JSON
    )

    desired_claude_json = _plan_claude_json_doc(claude_json_doc)
    claude_json_kind = (
        DeltaKind.NOOP
        if claude_json_doc == desired_claude_json
        else DeltaKind.WRITE_JSON
    )

    desired_zshrc, zshrc_kind = _plan_zshrc_install(zshrc_text)

    return PatchPlan(
        deltas=(
            FileDelta(FileTag.SETTINGS, settings_kind, desired_settings),
            FileDelta(FileTag.CLAUDE_JSON, claude_json_kind, desired_claude_json),
            FileDelta(FileTag.ZSHRC, zshrc_kind, desired_zshrc),
        )
    )


def plan_default(
    provider_spec: ProviderSpec,
    *,
    settings_doc: dict[str, Any] | None = None,
    zshrc_text: str = "",
) -> PatchPlan:
    """Plan the ``use default`` reversion for Claude Code.

    PURE inverse of :func:`plan_zai` for the env/zshrc concerns:

    - ``settings.json``: remove every managed key (ZAI + model-mode), keep
      foreign keys, drop ``env`` if empty.
    - ``.claude.json``: NOT touched (we never "un-complete" onboarding).
    - ``.zshrc``: remove ONLY the owned block.

    Idempotent: a second ``use default`` on the post-state is all-NOOP.

    Args:
        provider_spec: Carries the model mode so the same model-mode keys that
            ``plan_zai`` would set are the ones removed here. The two stay
            exact inverses regardless of mode.
        settings_doc: Parsed ``settings.json`` (or ``None`` if absent).
        zshrc_text: Raw ``.zshrc`` text (or ``""`` if absent).

    Returns:
        A :class:`PatchPlan` with deltas for ``settings`` and ``zshrc``
        (``.claude.json`` is intentionally absent from the plan).
    """
    desired_settings = _plan_default_settings_doc(
        settings_doc, provider_spec=provider_spec
    )
    settings_kind = (
        DeltaKind.NOOP if settings_doc == desired_settings else DeltaKind.WRITE_JSON
    )

    desired_zshrc, zshrc_kind = _plan_zshrc_remove(zshrc_text)

    return PatchPlan(
        deltas=(
            FileDelta(FileTag.SETTINGS, settings_kind, desired_settings),
            FileDelta(FileTag.ZSHRC, zshrc_kind, desired_zshrc),
        )
    )


def postconditions(
    region: Region,
    *,
    settings_doc: dict[str, Any] | None = None,
    zshrc_text: str = "",
) -> bool:
    """True iff ``settings.json`` + ``.zshrc`` reflect an active ``use zai``.

    PURE predicate used by ``status``/``doctor`` to confirm the activation
    took effect. Checks:

    - ``settings.json`` → ``env`` has ``ANTHROPIC_AUTH_TOKEN`` and
      ``ANTHROPIC_BASE_URL`` equal to the region's Z.ai URL, and does NOT
      have ``ANTHROPIC_API_KEY``.
    - ``.zshrc`` carries our owned block (presence marker).

    Note: this validates state, NOT secrets — it checks key presence and the
    base URL, never the token value (which may be redacted upstream).
    """
    doc: dict[str, Any] = settings_doc or {}
    env: dict[str, Any] = dict(doc.get("env") or {})

    if "ANTHROPIC_AUTH_TOKEN" not in env:
        return False
    if env.get("ANTHROPIC_BASE_URL") != base_url_for_region(region):
        return False
    if "ANTHROPIC_API_KEY" in env:
        return False
    if not owns_owned_block(zshrc_text):
        return False
    return True
