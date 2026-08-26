"""Factory Droid planning functions (PURE, per ADR-001).

These functions transform a *parsed* ``~/.factory/settings.json`` document
into a :class:`PatchPlan`. They never open a file, never read the environment,
and never prompt. The CLI layer parses the file (or supplies an empty seed)
and passes the parsed structure in; the IO layer
(:mod:`zai_python_helper.backends`) turns the delta into an atomic write.

Factory Droid config shape (per issue #7 / epic #1 spec D)::

    {
      "customModels": [
        {
          "displayName": "GLM-4.7 [GLM Coding Plan Global] - Anthropic",  # contains marker
          "provider": "anthropic",                              # protocol
          "model": "glm-4.7",
          "maxOutputTokens": 131072,
          "baseUrl": "<anthropic-endpoint>",                    # region+proto
          "apiKey": "<key>"
        },
        { ...same with provider: "generic-chat-completion-api", baseUrl: <paas-endpoint>... }
      ],
      "<other-top-level-keys>": ...                           # preserved
    }

Contract (ADR-005):

- ``plan_zai`` produces ONE delta — ``settings.json`` (deep-merge: remove any
  prior ``GLM Coding Plan`` entries from ``customModels``, then append our two
  protocol entries with the region's endpoints and the auth token; preserve
  every foreign ``customModels`` entry and all other top-level keys).
- ``plan_default`` produces the inverse — filter out entries whose
  ``displayName`` contains the marker; drop ``customModels`` if it becomes
  empty.
- Both are IDEMPOTENT: a second call on the post-state of the first yields a
  NOOP delta.

Detection: an entry is OURS iff its ``displayName`` contains the substring
``GLM Coding Plan``. The two entries are addressed by a STABLE protocol
discriminator (``anthropic`` / ``openai``), never by array index — an index
shifts on filter and would break idempotent revert.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.errors import ValidationError
from zai_python_helper.regions import (
    ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2,
    ZAI_PAAS_BASE_URL_BY_REGION,
    Region,
)

# ---------------------------------------------------------------------------
# Detection + entry shape (closed, explicit)
# ---------------------------------------------------------------------------
#
# An entry is ours iff its displayName contains this substring. Conservative —
# matches both protocol entries and any future variant following the convention.
_MARKER = "GLM Coding Plan"

# The two protocols we install, one entry each. Stable discriminators used as
# ownership-journal sub-keys (never array index).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "generic-chat-completion-api"
# Older releases wrote ``openai``.  Continue recognizing it so activation can
# replace a stale entry rather than leaving a duplicate managed model behind.
_LEGACY_PROVIDER_OPENAI = "openai"
OUR_PROTOCOLS: tuple[str, ...] = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI)

MODEL_ID = "glm-4.7"
MAX_OUTPUT_TOKENS = 131072

# Every value the helper has EVER written for a managed field, current first.
# The drift guard relaxes only for values inside these sets: a canonical-name
# entry carrying one is a stale post-state we may upgrade in place, while any
# other value is user customization we must refuse (see
# :func:`_assert_no_managed_field_drift`). APPEND the outgoing value here
# whenever MODEL_ID / MAX_OUTPUT_TOKENS changes — dropping one turns routine
# activation into a refusal for users still on it.
_KNOWN_MODEL_IDS: frozenset[str] = frozenset({MODEL_ID, "glm-4.6"})
_KNOWN_MAX_OUTPUT_TOKENS: frozenset[int] = frozenset({MAX_OUTPUT_TOKENS})


def anthropic_base_url_for_region(region: Region) -> str:
    """Z.ai Anthropic-protocol endpoint for ``region`` (epic #1 V2 matrix)."""
    try:
        return ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2[region]
    except KeyError as e:  # pragma: no cover - enum-closed, unreachable
        raise ValueError(f"Unknown region: {region!r}") from e


def paas_base_url_for_region(region: Region) -> str:
    """Z.ai OpenAI/paas-protocol endpoint for ``region``."""
    try:
        return ZAI_PAAS_BASE_URL_BY_REGION[region]
    except KeyError as e:  # pragma: no cover - enum-closed, unreachable
        raise ValueError(f"Unknown region: {region!r}") from e


def _is_our_entry(entry: Any) -> bool:
    """True iff a customModels entry is ours (displayName carries the marker)."""
    if not isinstance(entry, dict):
        return False
    name = entry.get("displayName")
    return isinstance(name, str) and _MARKER in name


def _protocol_of(entry: Any) -> str | None:
    """The protocol discriminator of an entry, or None if not set/recognized."""
    if not isinstance(entry, dict):
        return None
    proto = entry.get("provider")
    if proto == PROVIDER_ANTHROPIC:
        return PROVIDER_ANTHROPIC
    if proto in (PROVIDER_OPENAI, _LEGACY_PROVIDER_OPENAI):
        return PROVIDER_OPENAI
    return None


def _canonical_display_name(provider: str, region: Region) -> str:
    """The EXACT ``displayName`` the helper writes for ``provider``.

    Used as the provenance discriminator by the drift guard: the helper always
    writes this exact string, so an entry carrying it is one we wrote, while an
    entry merely containing the ``_MARKER`` substring may be user-authored.
    """
    plan_name = "Global" if region is Region.GLOBAL else "China"
    protocol_name = "Anthropic" if provider == PROVIDER_ANTHROPIC else "Openai"
    return f"GLM-4.7 [GLM Coding Plan {plan_name}] - {protocol_name}"


def _is_canonical_display_name(value: Any, provider: str, region: Region) -> bool:
    """Whether ``value`` is current or legacy helper-owned naming."""
    if value in {
        _canonical_display_name(provider, Region.GLOBAL),
        _canonical_display_name(provider, Region.CHINA),
    }:
        return True
    # Releases before upstream parity used a region-independent name.
    return value in {
        "Z.ai GLM Coding Plan (Anthropic)"
        if provider == PROVIDER_ANTHROPIC
        else "Z.ai GLM Coding Plan (OpenAI)"
    }


def _entry(provider: str, region: Region, auth_token: str) -> dict[str, Any]:
    """One desired customModels entry for ``use zai`` (pure)."""
    if provider == PROVIDER_ANTHROPIC:
        base_url = anthropic_base_url_for_region(region)
    else:  # PROVIDER_OPENAI
        base_url = paas_base_url_for_region(region)
    display = _canonical_display_name(provider, region)
    return {
        "displayName": display,
        "provider": provider,
        "model": MODEL_ID,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "baseUrl": base_url,
        "apiKey": auth_token,
    }


def _our_entries(region: Region, auth_token: str) -> list[dict[str, Any]]:
    """The two desired customModels entries for ``use zai`` (pure)."""
    return [
        _entry(PROVIDER_ANTHROPIC, region, auth_token),
        _entry(PROVIDER_OPENAI, region, auth_token),
    ]


# ---------------------------------------------------------------------------
# Entry-identity guards (issue #53) — fail closed on ambiguous / lossy state
# ---------------------------------------------------------------------------
#
# Two edge-case data-loss paths share one root cause: the ownership model
# addresses customModels[] entries by marker+protocol (a synthetic key), not
# by an exact immutable identity, and journals ONLY the prior apiKey — never
# model / baseUrl / limits. Both guards REFUSE the operation (raise
# ValidationError → one-line ``error:`` + exit 1) rather than silently drop a
# foreign entry or irreversibly clobber user config.
#
# F2 (field drift): activation deep-merges our managed fields over a pre-
# existing GLM entry, but only the apiKey is journaled — so model/baseUrl/
# limits would be lost on revert. Refuse when a managed field carries a value
# we would overwrite and cannot restore. Provenance takes TWO checks (see
# _assert_no_managed_field_drift): the exact canonical displayName proves the
# ENTRY is ours (the marker is a substring a user's own entry can satisfy), and
# _is_helper_value proves the VALUE is ours (displayName is user-editable, so a
# user customizing a managed entry keeps the canonical name). "Looks like ours"
# is not "is ours" — at either level.
#
# F3 (duplicates): two GLM-marker entries for one protocol make "which is
# ours" indeterminate — the merge keeps the first (dropping the rest) and
# revert removes the first (possibly a foreign entry inserted before ours).
# Refuse when >1 exists per protocol.

# Managed fields Factory Droid writes into an entry. A pre-existing GLM entry
# carrying any of these at a value DIFFERENT from the helper's canonical value
# means activation would IRREVERSIBLY clobber user config (only the apiKey is
# journaled). ``displayName`` is EXCLUDED — detection is an intentional
# substring match, so renaming to our canonical displayName is desired
# identity behavior, not data loss. ``provider`` is the protocol discriminator
# (not a data field); ``apiKey`` is journaled and restorable (drift there is
# normal token rotation).
_MANAGED_FIELDS: tuple[str, ...] = ("model", "maxOutputTokens", "baseUrl")


def _canonical_entry(provider: str, region: Region) -> dict[str, Any]:
    """The helper's canonical managed-field values for ``provider`` at ``region``.

    A pre-existing GLM entry whose managed fields ALL match these is a true
    re-activation post-state (idempotent path); any field that DIFFERS is user
    config we would clobber irreversibly. Derived from the same constants
    :func:`_entry` writes, so it tracks them automatically.
    """
    base_url = (
        anthropic_base_url_for_region(region)
        if provider == PROVIDER_ANTHROPIC
        else paas_base_url_for_region(region)
    )
    return {
        "model": MODEL_ID,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "baseUrl": base_url,
    }


def _known_base_urls(provider: str) -> set[str]:
    """Every regional baseUrl the helper could have written for ``provider``.

    One of the three "values the helper could have written" sets consumed by
    :func:`_assert_no_managed_field_drift`. A canonical-name entry on one of
    these URLs is a cross-region post-state we may rewrite; any other endpoint
    (a private proxy, say) is user configuration and refuses.
    """
    if provider == PROVIDER_ANTHROPIC:
        return set(ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2.values())
    return set(ZAI_PAAS_BASE_URL_BY_REGION.values())


def _is_helper_value(field: str, value: Any, provider: str) -> bool:
    """True iff ``value`` is one the helper itself could have written.

    The second half of the provenance test. A canonical ``displayName`` proves
    the ENTRY is ours; it does NOT prove the current field VALUES are, because
    displayName is user-editable and a user customizing an existing helper
    entry naturally keeps the managed name. So each managed field is checked
    against the closed set of values this helper has ever written.
    """
    if field == "model":
        return value in _KNOWN_MODEL_IDS
    if field == "maxOutputTokens":
        return value in _KNOWN_MAX_OUTPUT_TOKENS
    return value in _known_base_urls(provider)  # baseUrl


def _assert_no_duplicates(models: list[Any], *, path: str) -> None:
    """Raise ``ValidationError`` if >1 our-entry exists per protocol (F3).

    Two GLM-marker entries for one protocol make "which is ours" indeterminate
    — ``existing_by_proto`` keeps only the first (silently dropping the rest)
    and ``apply_revert_decisions``'s ``next(...)`` removes whichever comes
    first (possibly a foreign GLM entry inserted before ours). Refusing is
    safe: the user inspects ``settings.json`` and removes the stray entry.
    ``path`` labels the call site (``use zai`` / ``use default``) in the
    message.
    """
    seen: dict[str, int] = {}
    for m in models:
        if not _is_our_entry(m):
            continue
        proto = _protocol_of(m)
        if proto is None:
            # Marker + unrecognized provider: not one of our two protocols, so
            # it cannot make a protocol ambiguous and is not counted here. It
            # is NOT left intact, though — the merge loop in ``_plan_zai_doc``
            # drops such an entry (it lands in neither ``kept`` nor
            # ``existing_by_proto``). That drop is pre-existing and out of
            # scope for these guards; see the PR notes.
            continue
        seen[proto] = seen.get(proto, 0) + 1
    dups = sorted(p for p, n in seen.items() if n > 1)
    if dups:
        raise ValidationError(
            f"{path}: found multiple 'GLM Coding Plan' entries for "
            f"provider(s) {dups} in ~/.factory/settings.json customModels — "
            "remove the duplicate(s) before retrying (the helper cannot tell "
            "them apart safely)."
        )


def _assert_no_managed_field_drift(models: list[Any], region: Region) -> None:
    """Raise ``ValidationError`` if a pre-existing our-entry's managed field
    carries a value activation would irreversibly overwrite (F2).

    Checks the FIRST our-entry per protocol (duplicates are refused first by
    :func:`_assert_no_duplicates`). A managed field (``model`` /
    ``maxOutputTokens`` / ``baseUrl``) present at a value DIFFERENT from the
    canonical helper value trips the guard, because the journal restores only
    the apiKey — that overwrite cannot round-trip.

    PROVENANCE is the discriminator, and it takes TWO independent checks —
    one for the entry, one for each value. Neither alone is sufficient:

    * **The entry** — ``_is_our_entry`` matches the ``_MARKER`` substring, which
      a USER-authored entry can also carry ("My GLM Coding Plan china"). The
      helper always writes the EXACT canonical ``displayName``, so only that
      exact string marks the entry as one we created.
    * **The value** — a canonical ``displayName`` does NOT prove the current
      field values are ours. displayName is user-editable, and a user
      customizing an existing helper entry naturally KEEPS the managed name
      (it is what marks the entry as managed). So each managed field is
      additionally checked against :func:`_is_helper_value` — the closed set of
      values this helper has ever written.

    A field is drift unless BOTH hold. That keeps the stale-constant upgrade
    (canonical name + a value we wrote under an earlier ``MODEL_ID``, or
    another region's URL after a global↔china switch) while refusing in-place
    user customization (canonical name + a private proxy URL, a fine-tune, a
    hand-set token limit) — which the journal cannot restore, since it records
    only the prior apiKey.

    A field that is ABSENT — or explicitly ``None``, which in JSON means unset
    — is not drift; activation just writes it.
    """
    want = {
        PROVIDER_ANTHROPIC: _canonical_entry(PROVIDER_ANTHROPIC, region),
        PROVIDER_OPENAI: _canonical_entry(PROVIDER_OPENAI, region),
    }
    first_by_proto: dict[str, dict[str, Any]] = {}
    for m in models:
        if not _is_our_entry(m):
            continue
        proto = _protocol_of(m)
        if proto is None or proto in first_by_proto:
            continue  # dups refused above; inspect only the first per protocol
        first_by_proto[proto] = m
        canonical = want[proto]
        # The entry is ours iff the displayName is EXACTLY what _entry writes.
        helper_written = _is_canonical_display_name(m.get("displayName"), proto, region)
        drifted: list[str] = []
        for fld in _MANAGED_FIELDS:
            value = m.get(fld)
            if value is None:
                continue  # absent or explicit null → unset; activation adds it
            if value == canonical[fld]:
                continue  # already what we would write
            # Non-canonical value: ours to upgrade only if BOTH the entry and
            # the value are the helper's. Either one alone means user config.
            if helper_written and _is_helper_value(fld, value, proto):
                continue  # stale helper state (old constant / other region)
            drifted.append(fld)
        if drifted:
            raise ValidationError(
                f"Factory Droid activation would overwrite user-configured "
                f"field(s) {drifted} on an existing '{proto}' 'GLM Coding "
                f"Plan' entry in ~/.factory/settings.json. The helper only "
                f"journals the prior apiKey, so this overwrite is "
                f"irreversible. Back up the entry, then either move your "
                f"customization to a separate entry the helper does not manage "
                f"(one whose displayName omits 'GLM Coding Plan') or align "
                f"these field(s) with the helper values ({canonical}) and "
                f"retry."
            )


# ---------------------------------------------------------------------------
# Document transforms
# ---------------------------------------------------------------------------


def _plan_zai_doc(
    doc: dict[str, Any] | None,
    *,
    region: Region,
    auth_token: str,
) -> dict[str, Any]:
    """Return the desired ``settings.json`` document after ``use zai``.

    For each of our two protocol entries: DEEP-MERGE our managed fields
    (displayName/provider/model/maxOutputTokens/baseUrl/apiKey) into any
    existing GLM Coding Plan entry of the SAME protocol, preserving foreign
    sibling keys the user set on it — so activation never clobbers user
    config. Any prior GLM entry of a DIFFERENT protocol is dropped (a
    global↔china switch keeps only the current protocols). Foreign entries and
    all other top-level keys round-trip untouched. Does NOT mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}
    models = list(new_doc.get("customModels") or [])
    # Entry-identity guards (issue #53) — refuse BEFORE the merge loop, which
    # would otherwise silently drop duplicate GLM entries (F3) and irreversibly
    # clobber user-configured model/baseUrl/limits on a pre-existing entry (F2,
    # only the apiKey is journaled).
    _assert_no_duplicates(models, path="use zai")
    _assert_no_managed_field_drift(models, region)
    # Index our existing entries by protocol so we can deep-merge into them.
    existing_by_proto: dict[str, dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    for m in models:
        if _is_our_entry(m):
            proto = _protocol_of(m)
            if proto is not None and proto not in existing_by_proto:
                existing_by_proto[proto] = dict(m)
            # other GLM entries (duplicate / different protocol we'll re-add)
            # are dropped here and re-added fresh below.
        else:
            kept.append(m)
    for entry in _our_entries(region, auth_token):
        proto = _protocol_of(entry)
        assert proto is not None  # our entries always carry a known protocol
        base = dict(existing_by_proto.get(proto) or {})
        base.update(entry)  # our managed fields win; user sibling keys survive
        kept.append(base)
    new_doc["customModels"] = kept
    return new_doc


def _plan_default_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Return the desired ``settings.json`` document after ``use default``.

    Filter out entries whose ``displayName`` contains the marker; drop
    ``customModels`` if it becomes empty. Preserve all other keys. Does NOT
    mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}
    models = list(new_doc.get("customModels") or [])
    # Refuse ambiguous duplicates (F3): with >1 GLM entry per protocol the blind
    # inverse cannot tell ours from a foreign GLM entry and would silently
    # destroy the wrong one.
    _assert_no_duplicates(models, path="use default")
    models = [m for m in models if not _is_our_entry(m)]
    if models:
        new_doc["customModels"] = models
    else:
        new_doc.pop("customModels", None)
    return new_doc


# ---------------------------------------------------------------------------
# Public planning API
# ---------------------------------------------------------------------------


def plan_zai(
    region: Region,
    *,
    factory_doc: dict[str, Any] | None = None,
    auth_token: str,
) -> PatchPlan:
    """Plan the ``use zai`` activation for Factory Droid (PURE).

    Args:
        region: Selects the protocol endpoints (anthropic + paas) by region.
        factory_doc: Parsed ``settings.json`` (or ``None`` if absent).
        auth_token: The Z.ai auth token for each entry's ``apiKey``.
            Resolved by the caller — never read from env here.

    Returns:
        A :class:`PatchPlan` with one delta for the Factory Droid config file.
        Idempotent: a second ``use zai`` on the post-state is a NOOP.
    """
    desired = _plan_zai_doc(factory_doc, region=region, auth_token=auth_token)
    kind = DeltaKind.NOOP if factory_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.FACTORY_DROID, kind, desired),))


def plan_default(
    *,
    factory_doc: dict[str, Any] | None = None,
) -> PatchPlan:
    """Plan the ``use default`` reversion for Factory Droid (PURE blind inverse).

    Removes every ``GLM Coding Plan`` entry; drops ``customModels`` if empty.
    The journal-aware path is :func:`plan_revert` (used by the CLI); this
    function is retained for callers that want the pure inverse.

    Idempotent: a second ``use default`` on the post-state is a NOOP.
    """
    desired = _plan_default_doc(factory_doc)
    kind = DeltaKind.NOOP if factory_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.FACTORY_DROID, kind, desired),))


# ---------------------------------------------------------------------------
# Ownership journal integration (ADR-004)
# ---------------------------------------------------------------------------
#
# Factory Droid owns the apiKey of each of its two protocol entries. The array
# index is NOT stable (filtering shifts it), so the journal keys are STABLE
# synthetic sub-keys addressed by protocol: ``customModels.anthropic.apiKey``
# and ``customModels.openai.apiKey``. The ManagedField descriptors
# (tools/factory_droid.py) resolve the actual array element by protocol at
# get/set time.

JOURNAL_KEY_ANTHROPIC_APIKEY = "customModels.anthropic.apiKey"
JOURNAL_KEY_OPENAI_APIKEY = "customModels.openai.apiKey"
JOURNAL_KEY_ANTHROPIC_DISPLAY_NAME = "customModels.anthropic.displayName"
JOURNAL_KEY_OPENAI_DISPLAY_NAME = "customModels.openai.displayName"
JOURNAL_KEY_ANTHROPIC_PROVIDER = "customModels.anthropic.provider"
JOURNAL_KEY_OPENAI_PROVIDER = "customModels.openai.provider"


def revert_key_set() -> tuple[str, ...]:
    """The closed set of journal keys ``use default`` must consider."""
    return (
        JOURNAL_KEY_ANTHROPIC_APIKEY,
        JOURNAL_KEY_OPENAI_APIKEY,
        JOURNAL_KEY_ANTHROPIC_DISPLAY_NAME,
        JOURNAL_KEY_OPENAI_DISPLAY_NAME,
        JOURNAL_KEY_ANTHROPIC_PROVIDER,
        JOURNAL_KEY_OPENAI_PROVIDER,
    )


def _protocol_for_journal_key(key: str) -> str | None:
    """Map a journal key to its protocol discriminator, or None."""
    if key == JOURNAL_KEY_ANTHROPIC_APIKEY:
        return PROVIDER_ANTHROPIC
    if key == JOURNAL_KEY_OPENAI_APIKEY:
        return PROVIDER_OPENAI
    if key in (JOURNAL_KEY_ANTHROPIC_DISPLAY_NAME, JOURNAL_KEY_ANTHROPIC_PROVIDER):
        return PROVIDER_ANTHROPIC
    if key in (JOURNAL_KEY_OPENAI_DISPLAY_NAME, JOURNAL_KEY_OPENAI_PROVIDER):
        return PROVIDER_OPENAI
    return None


def _metadata_field_for_journal_key(key: str) -> str | None:
    if key in (JOURNAL_KEY_ANTHROPIC_DISPLAY_NAME, JOURNAL_KEY_OPENAI_DISPLAY_NAME):
        return "displayName"
    if key in (JOURNAL_KEY_ANTHROPIC_PROVIDER, JOURNAL_KEY_OPENAI_PROVIDER):
        return "provider"
    return None


def apply_revert_decisions(
    doc: dict[str, Any] | None,
    *,
    decisions,
) -> dict[str, Any]:
    """Apply per-protocol apiKey :class:`RevertDecision` to a Factory Droid doc.

    RESTORE → set the entry's apiKey to the prior (or remove the entry if the
    prior was absent); CLEAR → drop our entry for that protocol; REFUSE → leave
    the current apiKey (entry kept). Foreign entries always round-trip
    untouched. ``customModels`` is dropped if it becomes empty.
    """
    from zai_python_helper.ownership import RevertAction

    # The complete set of fields Factory Droid writes into one of its entries.
    # Used by the collapse logic to tell a foreign field the user added (which
    # must survive revert) from one of our own (safe to drop).
    our_entry_fields = {"displayName", "provider", "model", "maxOutputTokens", "baseUrl", "apiKey"}

    def _remove_our_entry(proto_idx: int) -> None:
        """Remove our entry for a protocol, preserving any foreign field.

        If the entry has a foreign field the user added, strip only OUR fields
        and keep the (now-de-marked) entry so the user's field is not clobbered
        (ADR-004). Otherwise drop the entry entirely.
        """
        entry = models[proto_idx]
        foreign = {k: v for k, v in entry.items() if k not in our_entry_fields}
        if foreign:
            # Keep the entry but reduced to the user's foreign fields (drop our
            # managed + marker fields so it is no longer detected as "ours").
            models[proto_idx] = dict(foreign)
        else:
            models.pop(proto_idx)

    new_doc: dict[str, Any] = dict(doc) if doc else {}
    # Deep-copy each entry dict: the RESTORE branch below writes
    # ``models[idx]["apiKey"] = ...``, and a shallow list copy would share the
    # entry dicts with ``doc`` — mutating the planner's input in place. That
    # breaks ADR-001 (pure / no input mutation) AND makes ``plan_revert``'s
    # ``factory_doc == desired`` comparison see the already-mutated input,
    # emitting a false NOOP (the CLI then reports ``use default`` applied while
    # the on-disk Z.ai keys remain). Copying each dict keeps the input pristine.
    models: list[dict[str, Any]] = [
        dict(m) if isinstance(m, dict) else m
        for m in (new_doc.get("customModels") or [])
    ]
    # Refuse ambiguous duplicates (F3): ``next(...)`` below resolves the FIRST
    # our-entry per protocol — with >1 present it could remove a foreign GLM
    # entry inserted before ours instead of the helper's.
    #
    # No field-drift check here: ``region`` is not available on this path, so
    # the canonical baseUrl cannot be computed. NOTE this leaves an F2-class
    # gap that is PRE-EXISTING and NOT closed by this PR: ``_remove_our_entry``
    # strips every field in ``our_entry_fields`` (model/maxOutputTokens/baseUrl
    # included), so a managed field the user edited in place AFTER activation
    # is destroyed on revert with only the apiKey journaled. Verified identical
    # on the pre-PR baseline. Closing it needs either a refusal here or a wider
    # journal (ADR-004), both out of scope for these guards.
    _assert_no_duplicates(models, path="use default")

    for key, decision in decisions.items():
        proto = _protocol_for_journal_key(key)
        if proto is None:
            continue
        # Find our entry for this protocol (at most one).
        idx = next(
            (i for i, m in enumerate(models) if _is_our_entry(m) and _protocol_of(m) == proto),
            None,
        )
        metadata_field = _metadata_field_for_journal_key(key)
        if metadata_field is not None:
            if idx is not None and decision.action == RevertAction.RESTORE:
                if decision.prior_present:
                    models[idx][metadata_field] = decision.prior_value
            continue
        if decision.action == RevertAction.RESTORE:
            if decision.prior_present:
                # Prior had a key → ensure our entry exists with it.
                if idx is None:
                    # Re-creating an entry without region/auth context is not
                    # supported on the revert path; the prior_present case with
                    # a missing entry is unexpected (REFUSE covers external
                    # removal). Leave models unchanged.
                    continue
                models[idx]["apiKey"] = decision.prior_value
            else:
                # Prior was absent → remove our entry for this protocol
                # (preserving any foreign field the user added).
                if idx is not None:
                    _remove_our_entry(idx)
        elif decision.action == RevertAction.CLEAR:
            if idx is not None:
                _remove_our_entry(idx)
        # REFUSE: leave the current entry/apiKey untouched.

    if models:
        new_doc["customModels"] = models
    else:
        new_doc.pop("customModels", None)
    return new_doc


def plan_revert(
    decisions,
    *,
    factory_doc: dict[str, Any] | None = None,
) -> PatchPlan:
    """Plan the journal-aware ``use default`` reversion for Factory Droid (S3).

    PURE. Honors the ownership journal's per-protocol apiKey
    :class:`RevertDecision` so the reversion is non-destructive. See
    :func:`apply_revert_decisions` for the per-entry semantics.
    """
    desired = apply_revert_decisions(factory_doc, decisions=decisions)
    kind = DeltaKind.NOOP if factory_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.FACTORY_DROID, kind, desired),))


def postconditions(region: Region, *, factory_doc: dict[str, Any] | None) -> bool:
    """True iff ``settings.json`` reflects an active ``use zai`` for ``region``.

    PURE predicate used by ``status`` / ``doctor``. Checks that BOTH protocol
    entries are present with an ``apiKey`` and the correct region ``baseUrl``.
    Never inspects the token VALUE (may be redacted upstream).
    """
    doc: dict[str, Any] = factory_doc or {}
    models = doc.get("customModels") or []
    want = {
        PROVIDER_ANTHROPIC: anthropic_base_url_for_region(region),
        PROVIDER_OPENAI: paas_base_url_for_region(region),
    }
    found: dict[str, str | None] = {PROVIDER_ANTHROPIC: None, PROVIDER_OPENAI: None}
    for m in models:
        if not _is_our_entry(m):
            continue
        proto = _protocol_of(m)
        if proto and found.get(proto) is None:
            if "apiKey" in m and m.get("baseUrl") == want[proto]:
                found[proto] = m.get("baseUrl")
    return all(v is not None for v in found.values())
