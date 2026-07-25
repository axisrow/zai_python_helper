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
          "displayName": "Z.ai GLM Coding Plan (Anthropic)",  # contains marker
          "provider": "anthropic",                              # protocol
          "model": "glm-4.7",
          "maxOutputTokens": 131072,
          "baseUrl": "<anthropic-endpoint>",                    # region+proto
          "apiKey": "<key>"
        },
        { ...same with provider: "openai", baseUrl: <paas-endpoint>... }
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
PROVIDER_OPENAI = "openai"
OUR_PROTOCOLS: tuple[str, ...] = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI)

MODEL_ID = "glm-4.7"
MAX_OUTPUT_TOKENS = 131072


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
    return proto if proto in OUR_PROTOCOLS else None


def _entry(provider: str, region: Region, auth_token: str) -> dict[str, Any]:
    """One desired customModels entry for ``use zai`` (pure)."""
    if provider == PROVIDER_ANTHROPIC:
        base_url = anthropic_base_url_for_region(region)
        display = "Z.ai GLM Coding Plan (Anthropic)"
    else:  # PROVIDER_OPENAI
        base_url = paas_base_url_for_region(region)
        display = "Z.ai GLM Coding Plan (OpenAI)"
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
# Document transforms
# ---------------------------------------------------------------------------


def _plan_zai_doc(
    doc: dict[str, Any] | None,
    *,
    region: Region,
    auth_token: str,
) -> dict[str, Any]:
    """Return the desired ``settings.json`` document after ``use zai``.

    Deep-merge: drop every prior ``GLM Coding Plan`` entry from
    ``customModels``, then append our two protocol entries; preserve every
    foreign entry and all other top-level keys. Does NOT mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}
    models = list(new_doc.get("customModels") or [])
    models = [m for m in models if not _is_our_entry(m)]
    models.extend(_our_entries(region, auth_token))
    new_doc["customModels"] = models
    return new_doc


def _plan_default_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Return the desired ``settings.json`` document after ``use default``.

    Filter out entries whose ``displayName`` contains the marker; drop
    ``customModels`` if it becomes empty. Preserve all other keys. Does NOT
    mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}
    models = list(new_doc.get("customModels") or [])
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


def revert_key_set() -> tuple[str, ...]:
    """The closed set of journal keys ``use default`` must consider."""
    return (JOURNAL_KEY_ANTHROPIC_APIKEY, JOURNAL_KEY_OPENAI_APIKEY)


def _protocol_for_journal_key(key: str) -> str | None:
    """Map a journal key to its protocol discriminator, or None."""
    if key == JOURNAL_KEY_ANTHROPIC_APIKEY:
        return PROVIDER_ANTHROPIC
    if key == JOURNAL_KEY_OPENAI_APIKEY:
        return PROVIDER_OPENAI
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
    models: list[dict[str, Any]] = list(new_doc.get("customModels") or [])

    for key, decision in decisions.items():
        proto = _protocol_for_journal_key(key)
        if proto is None:
            continue
        # Find our entry for this protocol (at most one).
        idx = next(
            (i for i, m in enumerate(models) if _is_our_entry(m) and _protocol_of(m) == proto),
            None,
        )
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
