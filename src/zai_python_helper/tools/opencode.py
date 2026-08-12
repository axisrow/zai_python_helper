"""OpenCode tool adapter — wraps the PURE planner for the Tool protocol.

Bridge between :mod:`zai_python_helper.core.planner.opencode` and the generic
:class:`~zai_python_helper.tools.base.Tool`. Owns the three OpenCode fields
(the provider apiKey + the two model strings) as :class:`ManagedField`
descriptors.

Ownership-key design (ADR-004): the provider NAME is region-dependent
(``zai-coding-plan`` global / ``zhipuai-coding-plan`` china), so the journal
key for the apiKey is a FIXED logical name (``provider.apiKey``), not the
region-specific provider name. The :class:`_ProviderApiKeyField` descriptor
resolves the actual provider entry at get/set time by finding the (single)
coding-plan provider in the doc — region-agnostic and stable across a
global↔china switch. ``model`` / ``small_model`` are flat top-level fields.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.domain import ProviderSpec
from zai_python_helper.core.planner import FileTag, PatchPlan
from zai_python_helper.core.planner import opencode as oc
from zai_python_helper.regions import Region
from zai_python_helper.tools.base import ManagedField, StatusRow, Tool


def _our_provider_name(
    doc: dict[str, Any] | None,
    journal_records: dict[str, Any] | None = None,
) -> str | None:
    """Return the coding-plan provider name in ``doc``, or None if absent.

    Normally at most one coding-plan provider exists at a time (``plan_zai``
    removes any prior one before adding the current region's), so the first
    match is authoritative.

    That invariant is NOT guaranteed for a hand-edited or migrated config: a
    duplicate-state doc carries both regional names at once (issue #50), and
    resolving by dict order there picks an ARBITRARY entry — which then feeds
    ``plan_revert`` (region inference), ``revert_decisions``,
    ``_ProviderApiKeyField`` and ``status_row`` (issue #61).

    So on a duplicate doc the resolution is OWNERSHIP-AWARE when a journal is
    supplied: :func:`~zai_python_helper.core.planner.opencode.owned_regional_provider_name`
    returns the entry whose apiKey still hashes to the active record's
    ``set_hash``, and that entry — the only one we can prove is ours — wins
    over dict order. When nothing is attributable (or no journal is passed)
    the doc is genuinely ambiguous and there is no better answer than the
    first match; callers that must not act on a guess check
    ``oc.has_duplicate_regional_providers`` themselves.
    """
    providers = (doc or {}).get("provider") or {}
    ours = [name for name in providers if oc._is_our_provider(name)]
    if not ours:
        return None
    if len(ours) > 1:
        owned = oc.owned_regional_provider_name(doc, journal_records)
        if owned is not None:
            return owned
    return ours[0]


class _ProviderApiKeyField:
    """The coding-plan provider's ``options.apiKey`` as a :class:`ManagedField`.

    Region-agnostic: resolves whichever coding-plan provider is present in the
    doc. The journal ``key`` is the fixed logical name ``provider.apiKey``.

    On a duplicate-state doc (both regional names at once) the resolution is
    ownership-aware when the descriptor was built with ``journal_records``:
    the entry the journal proves is ours wins over dict order, so the
    ownership capture and the revert read/write the SAME entry we wrote
    (issue #61). Without a journal it falls back to first-match.
    """

    key = oc.JOURNAL_KEY_APIKEY

    def __init__(self, journal_records: dict[str, Any] | None = None) -> None:
        self._journal_records = journal_records

    def get(self, doc: dict[str, Any] | None) -> tuple[bool, str | None]:
        name = _our_provider_name(doc, self._journal_records)
        if name is None:
            return False, None
        entry = ((doc or {}).get("provider") or {}).get(name) or {}
        options = entry.get("options") or {}
        present = "apiKey" in options
        return present, (options["apiKey"] if present else None)

    def set_value(self, doc: dict[str, Any], value: str | None) -> dict[str, Any]:
        new_doc = dict(doc)
        providers = dict(new_doc.get("provider") or {})
        name = _our_provider_name(new_doc, self._journal_records)
        if value is None:
            # Remove the apiKey; collapse empty options/entry.
            if name is not None:
                entry = dict(providers.get(name) or {})
                options = dict(entry.get("options") or {})
                options.pop("apiKey", None)
                if options:
                    entry["options"] = options
                    providers[name] = entry
                else:
                    entry.pop("options", None)
                    if entry:
                        providers[name] = entry
                    else:
                        providers.pop(name, None)
        else:
            # Ensure a coding-plan provider exists to hold the apiKey. If none
            # is present, this is a no-op set without a region — callers always
            # set via plan_zai which establishes the provider first.
            if name is None:
                # Cannot synthesize without a region; leave doc unchanged.
                pass
            else:
                entry = dict(providers.get(name) or {})
                options = dict(entry.get("options") or {})
                options["apiKey"] = value
                entry["options"] = options
                providers[name] = entry
        if providers:
            new_doc["provider"] = providers
        else:
            new_doc.pop("provider", None)
        return new_doc


class _TopLevelField:
    """A flat top-level OpenCode key (``model`` / ``small_model``)."""

    def __init__(self, key: str) -> None:
        self.key = key

    def get(self, doc: dict[str, Any] | None) -> tuple[bool, str | None]:
        doc = doc or {}
        present = self.key in doc
        return present, (doc[self.key] if present else None)

    def set_value(self, doc: dict[str, Any], value: str | None) -> dict[str, Any]:
        new_doc = dict(doc)
        if value is None:
            new_doc.pop(self.key, None)
        else:
            new_doc[self.key] = value
        return new_doc


class OpenCodeTool(Tool):
    """OpenCode ⇄ Z.ai integration as a :class:`Tool`."""

    name = "opencode"
    file_tags = (FileTag.OPENCODE,)

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------

    def read_state(self, paths) -> dict[FileTag, Any]:
        from zai_python_helper.backends import JsonBackend

        return {FileTag.OPENCODE: JsonBackend.read(paths.opencode)}

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan_zai(
        self,
        spec: ProviderSpec,
        region: Region,
        *,
        state: dict[FileTag, Any],
        auth_token: str,
        journal_records: dict[str, Any] | None = None,
    ) -> PatchPlan:
        return oc.plan_zai(
            region,
            opencode_doc=state.get(FileTag.OPENCODE),
            auth_token=auth_token,
            journal_records=journal_records,
        )

    def plan_revert(
        self,
        *,
        state: dict[FileTag, Any],
        decisions: dict[str, Any],
        journal_records: dict[str, Any] | None = None,
    ) -> PatchPlan:
        # plan_revert needs the region to map the apiKey decision onto the
        # region-specific provider name. The region is not on `state`; infer
        # it from whichever coding-plan provider is currently present, default
        # to GLOBAL when none (a clean revert with no prior provider). On a
        # duplicate-state doc the inference is ownership-aware (issue #61):
        # the entry the journal proves is ours selects the region, so the
        # revert acts on OUR entry rather than whichever came first.
        doc = state.get(FileTag.OPENCODE)
        name = _our_provider_name(doc, journal_records)
        region = (
            Region.CHINA
            if name == oc.PROVIDER_NAME_BY_REGION[Region.CHINA]
            else Region.GLOBAL
        )
        return oc.plan_revert(
            decisions, opencode_doc=doc, region=region
        )

    # ------------------------------------------------------------------
    # Ownership descriptor
    # ------------------------------------------------------------------

    def managed_fields(
        self,
        spec: ProviderSpec,
        journal_records: dict[str, Any] | None = None,
    ) -> list[ManagedField]:
        """The three OpenCode-owned fields.

        ``journal_records`` is threaded into :class:`_ProviderApiKeyField` so
        that on a duplicate-state doc the descriptor addresses the entry the
        journal proves is ours instead of whichever comes first in dict order
        (issue #61). The two model fields are flat top-level keys and have no
        such ambiguity.
        """
        return [
            _ProviderApiKeyField(journal_records),
            _TopLevelField(oc.JOURNAL_KEY_MODEL),
            _TopLevelField(oc.JOURNAL_KEY_SMALL_MODEL),
        ]

    def revert_key_set(self) -> tuple[str, ...]:
        return oc.revert_key_set()

    def extract_takeover(
        self,
        plan: PatchPlan,
        prior_state: dict[FileTag, Any],
        spec: ProviderSpec,
        *,
        journal_records: dict[str, Any] | None = None,
    ) -> list[tuple[str, str | None, bool, str | None]]:
        from zai_python_helper.ownership import hash_value

        prior_doc = prior_state.get(FileTag.OPENCODE)
        delta = plan.delta_for(FileTag.OPENCODE)
        desired_doc = delta.content if delta else (prior_doc or {})

        records: list[tuple[str, str | None, bool, str | None]] = []
        # Ownership-aware descriptors: on a duplicate-state prior doc the
        # apiKey field must read the PRIOR value off OUR entry — the one the
        # activation is about to fold away — not off whichever came first
        # (issue #61). The desired doc has a single regional entry by then.
        for field in self.managed_fields(spec, journal_records):
            prior_present, prior_value = field.get(prior_doc)
            set_present, set_value = field.get(desired_doc)
            if set_present:
                # We are SETTING this field → record hash of the value we set.
                records.append(
                    (field.key, prior_value, prior_present, hash_value(set_value))
                )
            elif prior_present:
                # The field was present before and is absent after → we are
                # REMOVING it → ownership-by-removal (set_hash None).
                records.append((field.key, prior_value, True, None))
            # else: absent before and after — not touched, skip.
        return records

    def revert_decisions(
        self,
        journal_records: dict[str, Any],
        state: dict[FileTag, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from zai_python_helper.ownership import revert

        doc = state.get(FileTag.OPENCODE)
        # Thread the journal through each revert so a RESTORE for one field
        # retires its record before the next field is evaluated (issue #48
        # cycle-state): the returned ``retired`` carries every retirement.
        retired: dict[str, Any] = journal_records
        out: dict[str, Any] = {}
        # Ownership-aware: on a duplicate-state doc the apiKey field reads the
        # entry the journal proves is ours, so the revert decision is computed
        # against OUR value rather than an arbitrary one (issue #61).
        for field in self.managed_fields(
            ProviderSpec(base_url="", model_mode=None),  # type: ignore[arg-type]
            journal_records,
        ):
            present, value = field.get(doc)
            current = value if present else None
            decision, retired = revert(retired, self.name, field.key, current)
            out[field.key] = decision
        return out, retired

    # ------------------------------------------------------------------
    # Status / postcondition / echo
    # ------------------------------------------------------------------

    def postconditions(self, region: Region, *, state: dict[FileTag, Any]) -> bool:
        return oc.postconditions(region, opencode_doc=state.get(FileTag.OPENCODE))

    def status_row(self, paths) -> StatusRow:
        from zai_python_helper.backends import JsonBackend
        from zai_python_helper.ownership import OwnershipJournal

        doc = JsonBackend.read(paths.opencode)
        active = False
        region: Region | None = None
        detail = ""
        # Read-only: the journal disambiguates WHICH regional entry is ours on
        # a duplicate-state doc, so the reported provider/region is the one we
        # actually manage rather than whichever came first (issue #61).
        journal_records = OwnershipJournal(paths.ownership_json).read()
        name = _our_provider_name(doc, journal_records)
        if name is not None:
            region = (
                Region.CHINA
                if name == oc.PROVIDER_NAME_BY_REGION[Region.CHINA]
                else Region.GLOBAL
            )
            # Active iff a coding-plan provider exists AND model references one.
            active = bool(doc) and oc._references_our_provider(doc.get("model"))
            has_key = bool(
                (((doc or {}).get("provider") or {}).get(name) or {}).get("options", {})
            )
            detail = f"provider={name} apiKey={'set' if has_key else 'missing'}"
            if oc.has_duplicate_regional_providers(doc):
                # The state that makes `use zai` fail. Surface it HERE rather
                # than only at activation time — a normal-looking status row
                # hid it until the command errored (issue #61).
                attributable = (
                    oc.owned_regional_provider_name(doc, journal_records) is not None
                )
                detail += (
                    " DUPLICATE-REGIONAL-PROVIDERS"
                    + ("" if attributable else " (unattributable — hand edit required)")
                )
        return StatusRow(
            tool=self.name,
            configured=doc is not None,
            zai_active=active,
            region=region,
            detail=detail,
        )

    def echo_lines(self, plan: PatchPlan, region: Region) -> list[str]:
        name = oc.provider_name_for_region(region)
        return [
            f"  provider: {name}",
            f"  model: {oc.model_main_for_region(region)}",
            f"  small_model: {oc.model_small_for_region(region)}",
            "  apiKey: <redacted>",
        ]
