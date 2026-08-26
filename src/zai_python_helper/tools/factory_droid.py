"""Factory Droid tool adapter — wraps the PURE planner for the Tool protocol.

Bridge between :mod:`zai_python_helper.core.planner.factory_droid` and the
generic :class:`~zai_python_helper.tools.base.Tool`. Owns the two apiKey
fields (one per protocol entry in ``customModels[]``) as :class:`ManagedField`
descriptors.

Ownership-key design (ADR-004): the ``customModels`` array index is NOT stable
(filtering shifts it), so each protocol's apiKey is addressed by a STABLE
synthetic journal key — ``customModels.anthropic.apiKey`` and
``customModels.openai.apiKey``. The :class:`_CustomModelApiKeyField` descriptor
resolves the actual array element by matching ``displayName`` (marker) +
``provider`` (protocol) at get/set time.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.domain import ProviderSpec
from zai_python_helper.core.planner import FileTag, PatchPlan
from zai_python_helper.core.planner import factory_droid as fd
from zai_python_helper.regions import Region
from zai_python_helper.tools.base import ManagedField, StatusRow, Tool


def _find_our_index(models: list[dict[str, Any]], protocol: str) -> int | None:
    """Index of OUR entry for ``protocol`` in ``models``, or None.

    Matches on the displayName marker AND the provider protocol — at most one
    such entry exists (``plan_zai`` removes prior ours before appending).
    """
    for i, m in enumerate(models):
        if fd._is_our_entry(m) and fd._protocol_of(m) == protocol:
            return i
    return None


class _CustomModelField:
    """One protocol entry's ``apiKey`` in ``customModels[]`` as a ManagedField.

    ``protocol`` is the stable discriminator (anthropic/openai); ``key`` is the
    fixed synthetic journal key. Region- and index-agnostic.
    """

    def __init__(self, protocol: str, key: str, field: str) -> None:
        self.protocol = protocol
        self.key = key
        self.field = field

    def get(self, doc: dict[str, Any] | None) -> tuple[bool, str | None]:
        models = list((doc or {}).get("customModels") or [])
        idx = _find_our_index(models, self.protocol)
        if idx is None:
            return False, None
        entry = models[idx]
        present = self.field in entry
        return present, (entry[self.field] if present else None)

    def set_value(self, doc: dict[str, Any], value: str | None) -> dict[str, Any]:
        new_doc = dict(doc)
        models = list(new_doc.get("customModels") or [])
        idx = _find_our_index(models, self.protocol)
        if value is None:
            if idx is not None:
                models.pop(idx)
        else:
            if idx is None:
                # Cannot synthesize a full entry without region/auth context;
                # callers always set via plan_zai which establishes entries.
                pass
            else:
                models[idx][self.field] = value
        if models:
            new_doc["customModels"] = models
        else:
            new_doc.pop("customModels", None)
        return new_doc


class FactoryDroidTool(Tool):
    """Factory Droid ⇄ Z.ai integration as a :class:`Tool`."""

    name = "factory_droid"
    file_tags = (FileTag.FACTORY_DROID,)

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------

    def read_state(self, paths) -> dict[FileTag, Any]:
        from zai_python_helper.backends import JsonBackend

        return {FileTag.FACTORY_DROID: JsonBackend.read(paths.factory_droid)}

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
        return fd.plan_zai(
            region, factory_doc=state.get(FileTag.FACTORY_DROID), auth_token=auth_token
        )

    def plan_revert(
        self,
        *,
        state: dict[FileTag, Any],
        decisions: dict[str, Any],
        journal_records: dict[str, Any] | None = None,
    ) -> PatchPlan:
        return fd.plan_revert(decisions, factory_doc=state.get(FileTag.FACTORY_DROID))

    # ------------------------------------------------------------------
    # Ownership descriptor
    # ------------------------------------------------------------------

    def managed_fields(
        self,
        spec: ProviderSpec,
        journal_records: dict[str, Any] | None = None,
    ) -> list[ManagedField]:
        return [
            _CustomModelField(fd.PROVIDER_ANTHROPIC, fd.JOURNAL_KEY_ANTHROPIC_APIKEY, "apiKey"),
            _CustomModelField(fd.PROVIDER_OPENAI, fd.JOURNAL_KEY_OPENAI_APIKEY, "apiKey"),
            _CustomModelField(fd.PROVIDER_ANTHROPIC, fd.JOURNAL_KEY_ANTHROPIC_DISPLAY_NAME, "displayName"),
            _CustomModelField(fd.PROVIDER_OPENAI, fd.JOURNAL_KEY_OPENAI_DISPLAY_NAME, "displayName"),
            _CustomModelField(fd.PROVIDER_ANTHROPIC, fd.JOURNAL_KEY_ANTHROPIC_PROVIDER, "provider"),
            _CustomModelField(fd.PROVIDER_OPENAI, fd.JOURNAL_KEY_OPENAI_PROVIDER, "provider"),
        ]

    def revert_key_set(self) -> tuple[str, ...]:
        return fd.revert_key_set()

    def extract_takeover(
        self,
        plan: PatchPlan,
        prior_state: dict[FileTag, Any],
        spec: ProviderSpec,
        *,
        journal_records: dict[str, Any] | None = None,
    ) -> list[tuple[str, str | None, bool, str | None]]:
        from zai_python_helper.ownership import hash_value

        prior_doc = prior_state.get(FileTag.FACTORY_DROID)
        delta = plan.delta_for(FileTag.FACTORY_DROID)
        desired_doc = delta.content if delta else (prior_doc or {})

        records: list[tuple[str, str | None, bool, str | None]] = []
        for field in self.managed_fields(spec):
            prior_present, prior_value = field.get(prior_doc)
            set_present, set_value = field.get(desired_doc)
            if set_present:
                records.append(
                    (field.key, prior_value, prior_present, hash_value(set_value))
                )
            elif prior_present:
                # Entry existed before with a key, absent after → removal.
                records.append((field.key, prior_value, True, None))
            # else: absent before and after — not touched, skip.
        return records

    def revert_decisions(
        self,
        journal_records: dict[str, Any],
        state: dict[FileTag, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from zai_python_helper.ownership import revert

        doc = state.get(FileTag.FACTORY_DROID)
        # Thread the journal through each revert so a RESTORE for one field
        # retires its record before the next field is evaluated (issue #48
        # cycle-state): the returned ``retired`` carries every retirement.
        retired: dict[str, Any] = journal_records
        out: dict[str, Any] = {}
        for field in self.managed_fields(ProviderSpec(base_url="", model_mode=None)):  # type: ignore[arg-type]
            present, value = field.get(doc)
            current = value if present else None
            decision, retired = revert(retired, self.name, field.key, current)
            out[field.key] = decision
        return out, retired

    # ------------------------------------------------------------------
    # Status / postcondition / echo
    # ------------------------------------------------------------------

    def postconditions(self, region: Region, *, state: dict[FileTag, Any]) -> bool:
        return fd.postconditions(region, factory_doc=state.get(FileTag.FACTORY_DROID))

    def status_row(self, paths) -> StatusRow:
        from zai_python_helper.backends import JsonBackend

        doc = JsonBackend.read(paths.factory_droid)
        models = (doc or {}).get("customModels") or []
        ours = [m for m in models if fd._is_our_entry(m)]
        active = len(ours) == len(fd.OUR_PROTOCOLS) and all(
            "apiKey" in m for m in ours
        )
        # Region inference: match the anthropic entry's baseUrl against known.
        region: Region | None = None
        for m in ours:
            if fd._protocol_of(m) == fd.PROVIDER_ANTHROPIC:
                url = m.get("baseUrl")
                from zai_python_helper.regions import (
                    ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2,
                )

                for r, u in ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2.items():
                    if url == u:
                        region = r
                        break
        detail = f"entries={[fd._protocol_of(m) for m in ours]}"
        return StatusRow(
            tool=self.name,
            configured=doc is not None,
            zai_active=active,
            region=region,
            detail=detail,
        )

    def echo_lines(self, plan: PatchPlan, region: Region) -> list[str]:
        entries = fd._our_entries(region, "<redacted>")
        lines = ["  customModels:"]
        for e in entries:
            lines.append(f"    - {e['provider']}: model={e['model']} baseUrl={e['baseUrl']}")
        lines.append("      apiKey: <redacted> (per entry)")
        return lines
