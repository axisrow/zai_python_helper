"""Claude Code tool adapter — wraps the PURE planner for the Tool protocol.

This module is the bridge between the Claude-Code-specific pure planner
(:mod:`zai_python_helper.core.planner.claude_code`) and the generic
:class:`~zai_python_helper.tools.base.Tool` protocol. Everything that used to
live in ``cli.py`` as Claude-Code-hardcoded assumptions — the flat
``settings.json::env`` block, the managed ``ANTHROPIC_*`` key set, the echo of
owned env vars — is expressed HERE as ``ManagedField`` instances and Tool
methods, so ``cli.py`` stays tool-agnostic.

Behavior is unchanged: the same pure ``plan_zai`` / ``plan_revert`` /
``revert_key_set`` functions drive the plan, and the ownership capture /
revert decision logic is the same code (moved verbatim into methods). The
existing Claude Code tests therefore pass through this adapter unchanged.
"""

from __future__ import annotations

from typing import Any

from zai_python_helper.core.domain import ProviderSpec
from zai_python_helper.core.planner import FileTag, PatchPlan
from zai_python_helper.core.planner.claude_code import (
    MANAGED_ZAI_KEYS,
    REMOVED_ON_ZAI_KEYS,
    _all_managed_model_keys,
    base_url_for_region,
)
from zai_python_helper.core.planner.claude_code import (
    plan_revert as cc_plan_revert,
)
from zai_python_helper.core.planner.claude_code import (
    plan_zai as cc_plan_zai,
)
from zai_python_helper.core.planner.claude_code import (
    postconditions as cc_postconditions,
)
from zai_python_helper.core.planner.claude_code import (
    revert_key_set as cc_revert_key_set,
)
from zai_python_helper.regions import Region
from zai_python_helper.tools.base import ManagedField, StatusRow, Tool


def _is_secret_key(key: str) -> bool:
    """Heuristic: is ``key`` a credential that must be redacted in echo output?

    Mirrors the CLI's former ``_is_secret_key`` (conservative — errs on the
    side of redacting). Moved here so the tool owns its own echo redaction.
    """
    upper = key.upper()
    if key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        return True
    if any(upper.endswith(suf) for suf in ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD")):
        return True
    return any(sub in upper for sub in ("SECRET", "PASSWORD", "CREDENTIAL", "TOKEN", "API_KEY"))


class _EnvField:
    """A Claude Code ``settings.json::env`` key as a :class:`ManagedField`.

    Claude Code owns a flat set of env-var names inside the ``env`` object of
    ``settings.json``. This descriptor reads/writes one such key, hiding the
    ``doc["env"][key]`` nesting behind the generic field interface.
    """

    def __init__(self, key: str) -> None:
        self.key = key

    def get(self, doc: dict[str, Any] | None) -> tuple[bool, str | None]:
        env = (doc or {}).get("env") or {}
        present = self.key in env
        return present, (env[self.key] if present else None)

    def set_value(self, doc: dict[str, Any], value: str | None) -> dict[str, Any]:
        new_doc = dict(doc)
        env = dict(new_doc.get("env") or {})
        if value is None:
            env.pop(self.key, None)
        else:
            env[self.key] = value
        if env:
            new_doc["env"] = env
        else:
            new_doc.pop("env", None)
        return new_doc


class ClaudeCodeTool(Tool):
    """Claude Code ⇄ Z.ai integration as a :class:`Tool` (default tool)."""

    name = "claude_code"
    file_tags = (FileTag.SETTINGS, FileTag.CLAUDE_JSON, FileTag.ZSHRC)

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------

    def read_state(self, paths) -> dict[FileTag, Any]:
        from zai_python_helper.backends import JsonBackend, ShellBackend

        return {
            FileTag.SETTINGS: JsonBackend.read(paths.claude_settings),
            FileTag.CLAUDE_JSON: JsonBackend.read(paths.claude_json),
            FileTag.ZSHRC: ShellBackend.read(paths.zshrc),
        }

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
        return cc_plan_zai(
            spec,
            region,
            settings_doc=state.get(FileTag.SETTINGS),
            claude_json_doc=state.get(FileTag.CLAUDE_JSON),
            zshrc_text=state.get(FileTag.ZSHRC, ""),
            auth_token=auth_token,
        )

    def plan_revert(
        self,
        *,
        state: dict[FileTag, Any],
        decisions: dict[str, Any],
        journal_records: dict[str, Any] | None = None,
    ) -> PatchPlan:
        return cc_plan_revert(
            decisions,
            settings_doc=state.get(FileTag.SETTINGS),
            zshrc_text=state.get(FileTag.ZSHRC, ""),
        )

    # ------------------------------------------------------------------
    # Ownership descriptor
    # ------------------------------------------------------------------

    def managed_fields(
        self,
        spec: ProviderSpec,
        journal_records: dict[str, Any] | None = None,
    ) -> list[ManagedField]:
        # The always-managed ZAI keys plus whatever the model mode contributes.
        keys = set(MANAGED_ZAI_KEYS) | set(_all_managed_model_keys())
        return [_EnvField(k) for k in sorted(keys)]

    def revert_key_set(self) -> tuple[str, ...]:
        return cc_revert_key_set()

    def extract_takeover(
        self,
        plan: PatchPlan,
        prior_state: dict[FileTag, Any],
        spec: ProviderSpec,
        *,
        journal_records: dict[str, Any] | None = None,
    ) -> list[tuple[str, str | None, bool, str | None]]:
        from zai_python_helper.ownership import hash_value

        settings_delta = plan.delta_for(FileTag.SETTINGS)
        desired_env = settings_delta.content.get("env", {}) if settings_delta else {}
        prior_doc = prior_state.get(FileTag.SETTINGS)
        prior_env = (prior_doc or {}).get("env") or {}

        managed = set(MANAGED_ZAI_KEYS) | set(_all_managed_model_keys())
        removed = set(REMOVED_ON_ZAI_KEYS)

        records: list[tuple[str, str | None, bool, str | None]] = []
        for key in sorted(managed):
            if key not in desired_env:
                continue  # this mode does not set this key
            set_hash = hash_value(desired_env[key])
            prior_present = key in prior_env
            prior_value = prior_env[key] if prior_present else None
            records.append((key, prior_value, prior_present, set_hash))
        for key in sorted(removed):
            # Ownership of a removal: set_hash None signals "we removed it".
            prior_present = key in prior_env
            prior_value = prior_env[key] if prior_present else None
            records.append((key, prior_value, prior_present, None))
        return records

    def revert_decisions(
        self,
        journal_records: dict[str, Any],
        state: dict[FileTag, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from zai_python_helper.ownership import revert

        settings_doc = state.get(FileTag.SETTINGS)
        current_env = (settings_doc or {}).get("env") or {}
        # Thread the journal through each revert so a RESTORE for one key
        # retires its record before the next key is evaluated (issue #48
        # cycle-state): the returned ``retired`` carries every retirement.
        retired: dict[str, Any] = journal_records
        decisions: dict[str, Any] = {}
        for key in self.revert_key_set():
            decision, retired = revert(
                retired,
                self.name,
                key,
                current_env.get(key) if key in current_env else None,
            )
            decisions[key] = decision
        return decisions, retired

    # ------------------------------------------------------------------
    # Status / postcondition / echo
    # ------------------------------------------------------------------

    def postconditions(self, region: Region, *, state: dict[FileTag, Any]) -> bool:
        return cc_postconditions(
            region,
            settings_doc=state.get(FileTag.SETTINGS),
            zshrc_text=state.get(FileTag.ZSHRC, ""),
        )

    def status_row(self, paths) -> StatusRow:
        # status rendering for Claude Code is owned by zai_python_helper.status
        # (S4). The Tool exposes a minimal row; full rendering stays there.
        from zai_python_helper.status import detect_status

        report = detect_status(paths)
        cc = report.claude_code
        if cc is None:
            return StatusRow(tool=self.name, configured=False, zai_active=False, region=None)
        return StatusRow(
            tool=self.name,
            configured=cc.settings_present,
            zai_active=cc.zai_active,
            region=cc.region,
            detail=f"base_url={cc.base_url or '-'} key={cc.key_var or '-'}",
        )

    def echo_lines(self, plan: PatchPlan, region: Region) -> list[str]:
        settings_delta = plan.delta_for(FileTag.SETTINGS)
        desired_env = settings_delta.content.get("env", {}) if settings_delta else {}
        managed = set(MANAGED_ZAI_KEYS) | set(_all_managed_model_keys())
        owned = {k: desired_env[k] for k in desired_env if k in managed}
        lines = [f"  base_url: {base_url_for_region(region)}"]
        if owned:
            lines.append("  env (managed):")
            for key in sorted(owned):
                val = "<redacted>" if _is_secret_key(key) else owned[key]
                lines.append(f"    {key}={val}")
        return lines
