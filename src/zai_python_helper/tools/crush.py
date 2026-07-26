"""Crush tool adapter — wraps the PURE planner for the Tool protocol.

Bridge between :mod:`zai_python_helper.core.planner.crush` and the generic
:class:`~zai_python_helper.tools.base.Tool`. Owns the two fields inside
``providers.zai`` (api_key + base_url) as :class:`ManagedField` descriptors.

Ownership-key design (ADR-004): the provider KEY is the fixed string ``"zai"``
(not region-dependent), so the journal keys are the straightforward dotted
paths ``providers.zai.api_key`` and ``providers.zai.base_url``. Only the
``base_url`` VALUE varies by region.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.domain import ProviderSpec
from zai_python_helper.core.planner import FileTag, PatchPlan
from zai_python_helper.core.planner import crush as cr
from zai_python_helper.regions import Region
from zai_python_helper.tools.base import ManagedField, StatusRow, Tool


def _zai_entry(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ``providers.zai`` entry dict (possibly empty) from ``doc``."""
    return (((doc or {}).get("providers") or {}).get(cr.PROVIDER_KEY) or {})


class _ZaiField:
    """One field inside Crush's ``providers.zai`` entry as a :class:`ManagedField`.

    ``field_name`` is the key inside the entry (``api_key`` / ``base_url``);
    ``key`` is the stable dotted journal key.
    """

    def __init__(self, field_name: str, key: str) -> None:
        self.field_name = field_name
        self.key = key

    def get(self, doc: dict[str, Any] | None) -> tuple[bool, str | None]:
        entry = _zai_entry(doc)
        present = self.field_name in entry
        return present, (entry[self.field_name] if present else None)

    def set_value(self, doc: dict[str, Any], value: str | None) -> dict[str, Any]:
        new_doc = dict(doc)
        providers = dict(new_doc.get("providers") or {})
        entry = dict(providers.get(cr.PROVIDER_KEY) or {})
        if value is None:
            entry.pop(self.field_name, None)
        else:
            entry[self.field_name] = value
        if entry:
            providers[cr.PROVIDER_KEY] = entry
        else:
            providers.pop(cr.PROVIDER_KEY, None)
        if providers:
            new_doc["providers"] = providers
        else:
            new_doc.pop("providers", None)
        return new_doc


class CrushTool(Tool):
    """Crush ⇄ Z.ai integration as a :class:`Tool`."""

    name = "crush"
    file_tags = (FileTag.CRUSH,)

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------

    def read_state(self, paths) -> dict[FileTag, Any]:
        from zai_python_helper.backends import JsonBackend

        return {FileTag.CRUSH: JsonBackend.read(paths.crush)}

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
    ) -> PatchPlan:
        return cr.plan_zai(
            region, crush_doc=state.get(FileTag.CRUSH), auth_token=auth_token
        )

    def plan_revert(
        self,
        *,
        state: dict[FileTag, Any],
        decisions: dict[str, Any],
    ) -> PatchPlan:
        # Crush's provider key is fixed ("zai"); region is unused by the planner.
        return cr.plan_revert(
            decisions, crush_doc=state.get(FileTag.CRUSH), region=Region.GLOBAL
        )

    # ------------------------------------------------------------------
    # Ownership descriptor
    # ------------------------------------------------------------------

    def managed_fields(self, spec: ProviderSpec) -> list[ManagedField]:
        return [
            _ZaiField("api_key", cr.JOURNAL_KEY_APIKEY),
            _ZaiField("base_url", cr.JOURNAL_KEY_BASE_URL),
        ]

    def revert_key_set(self) -> tuple[str, ...]:
        return cr.revert_key_set()

    def extract_takeover(
        self,
        plan: PatchPlan,
        prior_state: dict[FileTag, Any],
        spec: ProviderSpec,
    ) -> list[tuple[str, str | None, bool, str | None]]:
        from zai_python_helper.ownership import hash_value

        prior_doc = prior_state.get(FileTag.CRUSH)
        delta = plan.delta_for(FileTag.CRUSH)
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
                # Field was present before, absent after → ownership-by-removal.
                records.append((field.key, prior_value, True, None))
            # else: absent before and after — not touched, skip.
        return records

    def revert_decisions(
        self,
        journal_records: dict[str, Any],
        state: dict[FileTag, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from zai_python_helper.ownership import revert

        doc = state.get(FileTag.CRUSH)
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
        return cr.postconditions(region, crush_doc=state.get(FileTag.CRUSH))

    def status_row(self, paths) -> StatusRow:
        from zai_python_helper.backends import JsonBackend

        doc = JsonBackend.read(paths.crush)
        entry = _zai_entry(doc)
        active = False
        region: Region | None = None
        detail = ""
        if entry:
            # Infer region from the configured base_url, if it matches a known
            # paas endpoint.
            base_url = entry.get("base_url")
            from zai_python_helper.regions import ZAI_PAAS_BASE_URL_BY_REGION

            for r, url in ZAI_PAAS_BASE_URL_BY_REGION.items():
                if base_url == url:
                    region = r
                    break
            active = "api_key" in entry and region is not None
            has_key = "api_key" in entry
            detail = f"base_url={base_url or '-'} api_key={'set' if has_key else 'missing'}"
        return StatusRow(
            tool=self.name,
            configured=doc is not None,
            zai_active=active,
            region=region,
            detail=detail,
        )

    def echo_lines(self, plan: PatchPlan, region: Region) -> list[str]:
        entry = cr._provider_entry(region, "<redacted>")
        return [
            f"  providers.{cr.PROVIDER_KEY}:",
            f"    id: {entry['id']}",
            f"    name: {entry['name']}",
            f"    base_url: {entry['base_url']}",
            "    api_key: <redacted>",
        ]
