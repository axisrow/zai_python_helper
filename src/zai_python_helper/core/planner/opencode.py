"""OpenCode planning functions (PURE, per ADR-001).

These functions transform a *parsed* ``~/.config/opencode/opencode.json``
document into a :class:`PatchPlan`. They never open a file, never read the
environment, and never prompt. The CLI layer parses the file (or supplies an
empty seed) and passes the parsed structure in; the IO layer
(:mod:`zai_python_helper.backends`) turns the delta into an atomic write.

OpenCode config shape (per issue #7 / epic #1 spec B)::

    {
      "$schema": "...",            # preserved (foreign)
      "provider": {
        "zai-coding-plan": {       # global provider name (china: zhipuai-coding-plan)
          "options": {"apiKey": "<key>"}
        },
        "<other-provider>": {...}  # foreign providers preserved
      },
      "model": "zai/glm-4.6",      # top-level; references the coding-plan provider
      "small_model": "zai/glm-4.5-air"
    }

Contract (ADR-005):

- ``plan_zai`` produces ONE delta — ``opencode.json`` (deep-merge: add the
  coding-plan provider with its apiKey; set ``model``/``small_model`` to the
  coding-plan models; remove any PRIOR coding-plan provider so a global↔china
  switch does not leave a stale one; preserve ``$schema`` and all foreign
  providers/keys).
- ``plan_default`` produces the inverse — remove BOTH coding-plan provider
  names; clear ``model``/``small_model`` IF they referenced a coding-plan
  provider (leave them untouched if the user pointed them elsewhere).
- Both are IDEMPOTENT: a second call on the post-state of the first yields a
  NOOP delta.

The provider NAME is region-dependent (``zai-coding-plan`` global /
``zhipuai-coding-plan`` china), but the closed set of BOTH names is fixed
below so ``plan_default`` removes either regardless of the current region —
the two are exact inverses.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.regions import Region

# ---------------------------------------------------------------------------
# Provider naming + models (closed, explicit)
# ---------------------------------------------------------------------------
#
# The two coding-plan provider names OpenCode uses, one per region. ``plan_zai``
# adds the name for the CURRENT region; ``plan_default`` removes BOTH so a
# cross-region revert is clean (a global activation then a china revert — or
# vice versa — leaves no stale provider behind).
PROVIDER_NAME_BY_REGION: dict[Region, str] = {
    Region.GLOBAL: "zai-coding-plan",
    Region.CHINA: "zhipuai-coding-plan",
}
ALL_PROVIDER_NAMES: tuple[str, ...] = tuple(PROVIDER_NAME_BY_REGION.values())

# A provider key is "ours" iff it EXACTLY equals one of the two regional names
# above. Exact-match (not a ``coding-plan`` substring) is deliberate: a substring
# would also catch a foreign provider whose name merely contains the marker
# (e.g. ``my-coding-plan-proxy``) and destroy it on activation / reversion,
# violating ADR-004 (do not clobber external state).

# Model strings written to top-level ``model`` / ``small_model``. OpenCode
# model IDs are ``<provider_id>/<model_id>`` and MUST reference the configured
# coding-plan provider (the same name we install under ``provider.<name>``) —
# otherwise OpenCode cannot resolve the model to our provider and the
# activation is inert (or, worse, routes to a different provider). So the
# prefix is region-dependent: ``zai-coding-plan`` global /
# ``zhipuai-coding-plan`` china. The model IDs follow the spec: glm-4.6 for the
# main model, glm-4.5-air for the small model.
MODEL_ID_MAIN = "glm-4.6"
MODEL_ID_SMALL = "glm-4.5-air"


def provider_name_for_region(region: Region) -> str:
    """Return the coding-plan provider name for ``region`` (pure lookup)."""
    try:
        return PROVIDER_NAME_BY_REGION[region]
    except KeyError as e:  # pragma: no cover - enum-closed, unreachable
        raise ValueError(f"Unknown region: {region!r}") from e


def model_main_for_region(region: Region) -> str:
    """The top-level ``model`` string for ``region`` — ``<provider>/glm-4.6``."""
    return f"{provider_name_for_region(region)}/{MODEL_ID_MAIN}"


def model_small_for_region(region: Region) -> str:
    """The top-level ``small_model`` string for ``region`` — ``<provider>/glm-4.5-air``."""
    return f"{provider_name_for_region(region)}/{MODEL_ID_SMALL}"


def _is_our_provider(name: str) -> bool:
    """True iff ``name`` is one of the two managed regional provider names.

    Exact-match against :data:`ALL_PROVIDER_NAMES` — a substring match
    (``coding-plan``) would also catch a foreign provider whose name merely
    contains the marker (e.g. ``my-coding-plan-proxy``) and destroy it on
    activation / reversion, violating ADR-004 (do not clobber external state).
    """
    return name in ALL_PROVIDER_NAMES


def _references_our_provider(value: Any) -> bool:
    """True iff a model string references one of our managed providers.

    OpenCode model strings are ``<provider>/<model>``; the provider prefix is
    ours iff it exactly equals one of the two regional names.
    """
    if not isinstance(value, str) or "/" not in value:
        return False
    return value.split("/", 1)[0] in ALL_PROVIDER_NAMES


def has_duplicate_regional_providers(doc: dict[str, Any] | None) -> bool:
    """True iff BOTH regional provider names are present in ``doc`` (PURE).

    This is the *duplicate-state* seed of issue #50: a doc carrying both
    ``zai-coding-plan`` (global) AND ``zhipuai-coding-plan`` (china) at once.
    Such a doc is reachable only via a manual config edit or migration from
    the old broken version — the normal cross-region switch never creates it
    (``_plan_zai_doc`` removes any prior regional provider before adding the
    current one, leaving at most one).

    It is genuinely ambiguous here WHICH entry the user means to keep, and a
    region switch would silently clobber one entry's distinct
    credentials/options (Bug 4 edge) because the ownership journal keys the
    apiKey under a single fixed logical name (``provider.apiKey``) and so
    cannot tell the two regional names apart through a revert. Rather than
    guess (and lose data), :func:`plan_zai` refuses the activation — the
    non-destructive, fail-closed choice (ADR-004: do not clobber state we
    cannot safely switch). The guard is symmetric: it fires regardless of
    content equivalence or insertion order, because two coexisting managed
    names is itself the condition we cannot round-trip.
    """
    if not doc:
        return False
    providers = doc.get("provider") or {}
    return all(name in providers for name in ALL_PROVIDER_NAMES)


# ---------------------------------------------------------------------------
# Document transforms
# ---------------------------------------------------------------------------


def _plan_zai_doc(
    doc: dict[str, Any] | None,
    *,
    region: Region,
    auth_token: str,
) -> dict[str, Any]:
    """Return the desired ``opencode.json`` document after ``use zai``.

    Deep-merge: add the region's coding-plan provider with its apiKey; set
    ``model`` / ``small_model`` to the coding-plan models; remove any PRIOR
    coding-plan provider (so a global↔china switch leaves no stale entry);
    preserve ``$schema`` and every foreign provider / top-level key. Does NOT
    mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}

    # Providers: drop EVERY prior coding-plan provider first, then DEEP-MERGE
    # the current region's apiKey into one migration-source entry — preserving
    # any foreign keys the user set on it (e.g. timeout, concurrency, nested
    # models). Foreign providers round-trip untouched. Replacing the whole
    # entry (the old code) discarded user configuration irreversibly.
    name = provider_name_for_region(region)
    providers = dict(new_doc.get("provider") or {})
    # Seed the merge from the first prior coding-plan entry so a same-region
    # re-activation (or a global→china switch keeping user keys) preserves
    # them. Foreign providers are never touched. Remove EVERY regional entry
    # — at most one exists in normal flow, but a prior cross-region switch can
    # leave both; popping only the first (the old ``break``) left a stale
    # helper credential behind, surviving ``use default``.
    entry: dict[str, Any] = {}
    for prior in [n for n in list(providers) if _is_our_provider(n)]:
        if not entry:
            entry = dict(providers.pop(prior) or {})
        else:
            providers.pop(prior, None)
    options = dict(entry.get("options") or {})
    options["apiKey"] = auth_token
    entry["options"] = options
    providers[name] = entry
    new_doc["provider"] = providers

    # Top-level model strings — always set to the coding-plan models on
    # activation. The prefix is the region's provider name so OpenCode resolves
    # the model against OUR provider (postcondition requires this).
    new_doc["model"] = model_main_for_region(region)
    new_doc["small_model"] = model_small_for_region(region)
    return new_doc


def _plan_default_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Return the desired ``opencode.json`` document after ``use default``.

    Remove BOTH coding-plan providers; clear ``model`` / ``small_model`` ONLY
    if they referenced a coding-plan provider (leave them if the user pointed
    them at a foreign provider). Drop ``provider`` if it becomes empty.
    Preserve ``$schema`` and all other keys. Does NOT mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}

    providers = dict(new_doc.get("provider") or {})
    for name in [n for n in providers if _is_our_provider(n)]:
        providers.pop(name, None)
    if providers:
        new_doc["provider"] = providers
    else:
        new_doc.pop("provider", None)

    # Clear model strings only if they referenced a coding-plan provider.
    for field in ("model", "small_model"):
        if _references_our_provider(new_doc.get(field)):
            new_doc.pop(field, None)
    return new_doc


# ---------------------------------------------------------------------------
# Public planning API
# ---------------------------------------------------------------------------


def plan_zai(
    region: Region,
    *,
    opencode_doc: dict[str, Any] | None = None,
    auth_token: str,
) -> PatchPlan:
    """Plan the ``use zai`` activation for OpenCode (PURE).

    Args:
        region: Selects the coding-plan provider name (global vs china).
        opencode_doc: Parsed ``opencode.json`` (or ``None`` if absent).
        auth_token: The Z.ai auth token for the provider ``options.apiKey``.
            Resolved by the caller — never read from env here.

    Returns:
        A :class:`PatchPlan` with one delta for the OpenCode config file.
        Idempotent: a second ``use zai`` on the post-state is a NOOP.

    Raises:
        ConfigurationError: If ``opencode_doc`` is a duplicate-state seed —
            i.e. BOTH regional provider names are present at once (issue #50,
            Bug 4 edge). Such a doc is ambiguous (which entry does the user
            mean to keep?) and a region switch would silently clobber one
            entry's distinct credentials because the journal's single
            ``provider.apiKey`` key cannot round-trip two regional names. We
            refuse the activation rather than guess — the user resolves the
            duplicate by hand. ``plan_default`` is unaffected: a blind
            remove-both is non-destructive there.
    """
    if has_duplicate_regional_providers(opencode_doc):
        from zai_python_helper.errors import ConfigurationError

        raise ConfigurationError(
            "opencode.json carries BOTH regional providers "
            f"({ALL_PROVIDER_NAMES[0]} and {ALL_PROVIDER_NAMES[1]}) at once. "
            "This duplicate state is ambiguous and a region switch would "
            "silently destroy one entry's credentials. Remove the entry you "
            "no longer want, then run `use zai` again."
        )
    desired = _plan_zai_doc(opencode_doc, region=region, auth_token=auth_token)
    kind = DeltaKind.NOOP if opencode_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.OPENCODE, kind, desired),))


def plan_default(
    *,
    opencode_doc: dict[str, Any] | None = None,
) -> PatchPlan:
    """Plan the ``use default`` reversion for OpenCode (PURE blind inverse).

    Removes both coding-plan providers and clears model strings that
    referenced them. ``.claude.json``-style ownership-aware revert is handled
    by :func:`plan_revert` (the journal-aware path the CLI uses); this
    function is retained for callers that want the pure inverse.

    Idempotent: a second ``use default`` on the post-state is a NOOP.
    """
    desired = _plan_default_doc(opencode_doc)
    kind = DeltaKind.NOOP if opencode_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.OPENCODE, kind, desired),))


# ---------------------------------------------------------------------------
# Ownership journal integration (ADR-004)
# ---------------------------------------------------------------------------
#
# OpenCode owns three logical fields: the provider apiKey and the two
# top-level model strings. The provider NAME is region-dependent, so the
# journal keys are FIXED logical names (not the region-specific provider
# name) — this keeps a global→china switch stable in the journal. The
# ManagedField descriptors (in tools/opencode.py) resolve the region-specific
# provider name at get/set time.

#: The closed set of journal keys ``use default`` considers for OpenCode.
JOURNAL_KEY_APIKEY = "provider.apiKey"
JOURNAL_KEY_MODEL = "model"
JOURNAL_KEY_SMALL_MODEL = "small_model"


def revert_key_set() -> tuple[str, ...]:
    """The closed set of journal keys ``use default`` must consider (S3)."""
    return (JOURNAL_KEY_APIKEY, JOURNAL_KEY_MODEL, JOURNAL_KEY_SMALL_MODEL)


def apply_revert_decisions(
    doc: dict[str, Any] | None,
    *,
    decisions,
    region: Region,
) -> dict[str, Any]:
    """Apply per-field :class:`RevertDecision` to an OpenCode doc (PURE).

    The decisions are keyed by :func:`revert_key_set`. Because the provider
    NAME is region-dependent and the journal keys are logical, this helper
    needs ``region`` to map the apiKey decision onto the right provider entry.

    - RESTORE: put back the prior value (apiKey → the region's provider
      ``options.apiKey``; model strings → top-level keys). A prior that was
      absent re-removes the field.
    - CLEAR: drop the field (apiKey → remove the region's coding-plan provider
      if it has no other keys; model → remove the top-level key).
    - REFUSE: leave the current value (copy through from ``doc``).

    Foreign providers/keys always round-trip untouched.
    """
    from zai_python_helper.ownership import RevertAction

    new_doc: dict[str, Any] = dict(doc) if doc else {}
    providers = dict(new_doc.get("provider") or {})
    provider_name = provider_name_for_region(region)

    for key, decision in decisions.items():
        if key == JOURNAL_KEY_APIKEY:
            entry = dict(providers.get(provider_name) or {})
            options = dict(entry.get("options") or {})
            if decision.action == RevertAction.RESTORE:
                if decision.prior_present:
                    options["apiKey"] = decision.prior_value
                    entry["options"] = options
                    providers[provider_name] = entry
                else:
                    # Prior was absent — ensure no apiKey we set remains.
                    options.pop("apiKey", None)
                    if options:
                        entry["options"] = options
                        providers[provider_name] = entry
                    else:
                        entry.pop("options", None)
                        if entry:
                            providers[provider_name] = entry
                        else:
                            providers.pop(provider_name, None)
            elif decision.action == RevertAction.CLEAR:
                options.pop("apiKey", None)
                if options:
                    entry["options"] = options
                    providers[provider_name] = entry
                else:
                    entry.pop("options", None)
                    if entry:
                        providers[provider_name] = entry
                    else:
                        providers.pop(provider_name, None)
            # REFUSE: leave current value (already in `providers`).
        elif key in (JOURNAL_KEY_MODEL, JOURNAL_KEY_SMALL_MODEL):
            if decision.action == RevertAction.RESTORE:
                if decision.prior_present:
                    new_doc[key] = decision.prior_value
                else:
                    new_doc.pop(key, None)
            elif decision.action == RevertAction.CLEAR:
                new_doc.pop(key, None)
            # REFUSE: leave current value (already in new_doc).

    if providers:
        new_doc["provider"] = providers
    else:
        new_doc.pop("provider", None)
    return new_doc


def plan_revert(
    decisions,
    *,
    opencode_doc: dict[str, Any] | None = None,
    region: Region,
) -> PatchPlan:
    """Plan the journal-aware ``use default`` reversion for OpenCode (S3).

    PURE. Honors the ownership journal's per-key :class:`RevertDecision` so the
    reversion is non-destructive (RESTORE / CLEAR / REFUSE per ADR-004). See
    :func:`apply_revert_decisions` for the field-by-field semantics.
    """
    desired = apply_revert_decisions(
        opencode_doc, decisions=decisions, region=region
    )
    kind = DeltaKind.NOOP if opencode_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.OPENCODE, kind, desired),))


def postconditions(region: Region, *, opencode_doc: dict[str, Any] | None) -> bool:
    """True iff ``opencode.json`` reflects an active ``use zai`` for ``region``.

    PURE predicate used by ``status`` / ``doctor``. Checks that the region's
    coding-plan provider exists with an ``options.apiKey`` and that ``model``
    references it. Never inspects the token VALUE (may be redacted upstream).
    """
    doc: dict[str, Any] = opencode_doc or {}
    providers = doc.get("provider") or {}
    name = provider_name_for_region(region)
    entry = providers.get(name) or {}
    options = entry.get("options") or {}
    if "apiKey" not in options:
        return False
    if not _references_our_provider(doc.get("model")):
        return False
    return True
