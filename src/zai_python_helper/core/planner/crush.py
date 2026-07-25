"""Crush planning functions (PURE, per ADR-001).

These functions transform a *parsed* ``~/.config/crush/crush.json`` document
into a :class:`PatchPlan`. They never open a file, never read the environment,
and never prompt. The CLI layer parses the file (or supplies an empty seed)
and passes the parsed structure in; the IO layer
(:mod:`zai_python_helper.backends`) turns the delta into an atomic write.

Crush config shape (per issue #7 / epic #1 spec C)::

    {
      "providers": {
        "zai": {                       # fixed provider key (NOT region-dependent)
          "id": "zai",
          "name": "ZAI Provider",
          "base_url": "<paas-endpoint>",   # region-dependent value
          "api_key": "<key>"
        },
        "<other-provider>": {...}      # foreign providers preserved
      }
    }

Contract (ADR-005):

- ``plan_zai`` produces ONE delta — ``crush.json`` (deep-merge: set
  ``providers.zai`` to the Z.ai provider entry with the region's paas base URL
  and the auth token; preserve all foreign providers and other top-level keys).
- ``plan_default`` produces the inverse — remove ``providers.zai``; drop
  ``providers`` if it becomes empty.
- Both are IDEMPOTENT: a second call on the post-state of the first yields a
  NOOP delta.

Unlike OpenCode, the provider KEY is the fixed string ``"zai"`` (not
region-dependent); only the ``base_url`` VALUE varies by region. This keeps
the ownership-journal keys simple and stable.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.regions import ZAI_PAAS_BASE_URL_BY_REGION, Region

# ---------------------------------------------------------------------------
# Provider entry (closed, explicit)
# ---------------------------------------------------------------------------
#
# The fixed key under ``providers`` where Crush stores the Z.ai provider.
# Unlike OpenCode's region-dependent provider name, Crush always uses "zai".
PROVIDER_KEY = "zai"
PROVIDER_NAME = "ZAI Provider"
PROVIDER_ID = "zai"


def paas_base_url_for_region(region: Region) -> str:
    """Return the Z.ai paas base URL for ``region`` (pure lookup).

    Crush speaks the OpenAI/paas protocol, so its ``base_url`` points at the
    Z.ai paas gateway (not the Anthropic-compatible one).
    """
    try:
        return ZAI_PAAS_BASE_URL_BY_REGION[region]
    except KeyError as e:  # pragma: no cover - enum-closed, unreachable
        raise ValueError(f"Unknown region: {region!r}") from e


def _provider_entry(region: Region, auth_token: str) -> dict[str, Any]:
    """The desired ``providers.zai`` entry for ``use zai`` (pure)."""
    return {
        "id": PROVIDER_ID,
        "name": PROVIDER_NAME,
        "base_url": paas_base_url_for_region(region),
        "api_key": auth_token,
    }


# ---------------------------------------------------------------------------
# Document transforms
# ---------------------------------------------------------------------------


def _plan_zai_doc(
    doc: dict[str, Any] | None,
    *,
    region: Region,
    auth_token: str,
) -> dict[str, Any]:
    """Return the desired ``crush.json`` document after ``use zai``.

    Deep-merge: set ``providers.zai`` to the Z.ai provider entry; preserve all
    foreign providers and every other top-level key. Does NOT mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}
    providers = dict(new_doc.get("providers") or {})
    providers[PROVIDER_KEY] = _provider_entry(region, auth_token)
    new_doc["providers"] = providers
    return new_doc


def _plan_default_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Return the desired ``crush.json`` document after ``use default``.

    Remove ``providers.zai``; drop ``providers`` if it becomes empty. Preserve
    all other keys. Does NOT mutate the input.
    """
    new_doc: dict[str, Any] = dict(doc) if doc else {}
    providers = dict(new_doc.get("providers") or {})
    providers.pop(PROVIDER_KEY, None)
    if providers:
        new_doc["providers"] = providers
    else:
        new_doc.pop("providers", None)
    return new_doc


# ---------------------------------------------------------------------------
# Public planning API
# ---------------------------------------------------------------------------


def plan_zai(
    region: Region,
    *,
    crush_doc: dict[str, Any] | None = None,
    auth_token: str,
) -> PatchPlan:
    """Plan the ``use zai`` activation for Crush (PURE).

    Args:
        region: Selects the paas base URL written into ``providers.zai.base_url``.
        crush_doc: Parsed ``crush.json`` (or ``None`` if absent).
        auth_token: The Z.ai auth token for ``providers.zai.api_key``.
            Resolved by the caller — never read from env here.

    Returns:
        A :class:`PatchPlan` with one delta for the Crush config file.
        Idempotent: a second ``use zai`` on the post-state is a NOOP.
    """
    desired = _plan_zai_doc(crush_doc, region=region, auth_token=auth_token)
    kind = DeltaKind.NOOP if crush_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.CRUSH, kind, desired),))


def plan_default(
    *,
    crush_doc: dict[str, Any] | None = None,
) -> PatchPlan:
    """Plan the ``use default`` reversion for Crush (PURE blind inverse).

    Removes ``providers.zai`` and drops ``providers`` if empty. The
    journal-aware path is :func:`plan_revert` (used by the CLI); this function
    is retained for callers that want the pure inverse.

    Idempotent: a second ``use default`` on the post-state is a NOOP.
    """
    desired = _plan_default_doc(crush_doc)
    kind = DeltaKind.NOOP if crush_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.CRUSH, kind, desired),))


# ---------------------------------------------------------------------------
# Ownership journal integration (ADR-004)
# ---------------------------------------------------------------------------
#
# Crush owns the two fields inside ``providers.zai`` that carry a value we set:
# the api_key (secret) and the base_url (region-dependent). The provider KEY is
# fixed ("zai"), so the journal keys are straightforward dotted paths.

JOURNAL_KEY_APIKEY = "providers.zai.api_key"
JOURNAL_KEY_BASE_URL = "providers.zai.base_url"


def revert_key_set() -> tuple[str, ...]:
    """The closed set of journal keys ``use default`` must consider for Crush."""
    return (JOURNAL_KEY_APIKEY, JOURNAL_KEY_BASE_URL)


def apply_revert_decisions(
    doc: dict[str, Any] | None,
    *,
    decisions,
    region: Region,
) -> dict[str, Any]:
    """Apply per-field :class:`RevertDecision` to a Crush doc (PURE).

    RESTORE → put back the prior value (or re-absent it); CLEAR → drop the
    field; REFUSE → leave the current value. The ``providers.zai`` entry is
    collapsed (and ``providers`` dropped if empty) ONLY when it would be left
    as an inert stub — i.e. it has neither ``api_key`` nor ``base_url`` AND
    every remaining field is one we own (id/name). A REFUSE'd ``base_url`` (a
    user's external edit) or any foreign field the user added into the entry
    is preserved (ADR-004: never clobber an externally-changed value).
    """
    from zai_python_helper.ownership import RevertAction

    new_doc: dict[str, Any] = dict(doc) if doc else {}
    providers = dict(new_doc.get("providers") or {})
    entry = dict(providers.get(PROVIDER_KEY) or {})

    def _set_field(field_name: str, decision) -> None:
        if decision.action == RevertAction.RESTORE:
            if decision.prior_present:
                entry[field_name] = decision.prior_value
            else:
                entry.pop(field_name, None)
        elif decision.action == RevertAction.CLEAR:
            entry.pop(field_name, None)
        # REFUSE: leave current value (already in `entry`).

    apikey_decision = decisions.get(JOURNAL_KEY_APIKEY)
    baseurl_decision = decisions.get(JOURNAL_KEY_BASE_URL)
    if apikey_decision is not None:
        _set_field("api_key", apikey_decision)
    if baseurl_decision is not None:
        _set_field("base_url", baseurl_decision)

    # Collapse: the ``providers.zai`` entry is our artifact (we create it whole
    # in ``_provider_entry``: id/name/base_url/api_key). After applying the
    # decisions, drop the entry ONLY if it is an inert stub — no value-carrying
    # field (api_key/base_url) remains AND every leftover field is one of our
    # stub markers (id/name). This preserves a REFUSE'd base_url (external user
    # edit) and any foreign field the user added into the entry (ADR-004).
    our_fields = {"id", "name", "base_url", "api_key"}
    has_value_field = "api_key" in entry or "base_url" in entry
    has_foreign_field = any(k not in our_fields for k in entry)
    if has_value_field or has_foreign_field:
        providers[PROVIDER_KEY] = entry
    else:
        providers.pop(PROVIDER_KEY, None)
    if providers:
        new_doc["providers"] = providers
    else:
        new_doc.pop("providers", None)
    return new_doc


def plan_revert(
    decisions,
    *,
    crush_doc: dict[str, Any] | None = None,
    region: Region,
) -> PatchPlan:
    """Plan the journal-aware ``use default`` reversion for Crush (S3).

    PURE. Honors the ownership journal's per-field :class:`RevertDecision` so
    the reversion is non-destructive. ``region`` is accepted for signature
    symmetry with OpenCode; Crush's provider key is fixed so it is unused.
    """
    del region  # provider key is fixed ("zai"); no region-dependent resolution
    desired = apply_revert_decisions(crush_doc, decisions=decisions, region=Region.GLOBAL)
    kind = DeltaKind.NOOP if crush_doc == desired else DeltaKind.WRITE_JSON
    return PatchPlan(deltas=(FileDelta(FileTag.CRUSH, kind, desired),))


def postconditions(region: Region, *, crush_doc: dict[str, Any] | None) -> bool:
    """True iff ``crush.json`` reflects an active ``use zai`` for ``region``.

    PURE predicate used by ``status`` / ``doctor``. Checks that ``providers.zai``
    exists with an ``api_key`` and that ``base_url`` matches the region's paas
    endpoint. Never inspects the token VALUE (may be redacted upstream).
    """
    doc: dict[str, Any] = crush_doc or {}
    entry = (doc.get("providers") or {}).get(PROVIDER_KEY) or {}
    if "api_key" not in entry:
        return False
    if entry.get("base_url") != paas_base_url_for_region(region):
        return False
    return True
