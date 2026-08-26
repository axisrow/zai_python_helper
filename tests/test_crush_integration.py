"""Integration tests for the Crush tool: HOME-isolated apply → revert cycle
through the Tool interface and the ownership journal.
"""

from __future__ import annotations

import pytest

from zai_python_helper.backends import JsonBackend
from zai_python_helper.core.domain import ModelMode, ProviderSpec
from zai_python_helper.ownership import OwnershipJournal, take_over
from zai_python_helper.patchplan import ProcessLock, apply_plan_locked
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.tools import get_tool
from zai_python_helper.tools.crush import CrushTool

TOKEN = "sk-integration-token"


@pytest.fixture
def tool() -> CrushTool:
    return get_tool("crush")  # type: ignore[return-value]


def _spec() -> ProviderSpec:
    return ProviderSpec(base_url="https://api.z.ai/api/anthropic", model_mode=ModelMode.ORIGINAL)


def _read(paths):
    return JsonBackend.read(paths.crush)


def _merge(tool, current, records):
    merged = current
    for key, prior_value, prior_present, set_hash in records:
        merged = take_over(merged, tool.name, key, prior_value, prior_present, set_hash)
    return merged


class TestApplyAndRevert:
    def test_use_zai_writes_exact_config(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan, state=lock.state)
        entry = _read(paths)["providers"]["zai"]
        assert entry["api_key"] == TOKEN
        assert entry["base_url"] == "https://api.z.ai/api/coding/paas/v4"
        assert entry["id"] == "zai"

    def test_use_zai_is_idempotent(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        spec = _spec()
        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            apply_plan_locked(
                paths,
                tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN),
                state=lock.state,
            )
            state2 = tool.read_state(paths)
            plan2 = tool.plan_zai(spec, Region.GLOBAL, state=state2, auth_token=TOKEN)
        assert plan2.is_empty

    def test_use_default_restores_prior_via_journal(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        spec = _spec()
        seed = {
            "providers": {"openai": {"base_url": "f", "api_key": "foreign"}},
            "theme": "dark",
        }
        JsonBackend.write(paths.crush, seed)
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan, state=lock.state)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            decisions, retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan, state=lock.state)

        doc = _read(paths)
        # zai gone; foreign provider + theme restored.
        assert "zai" not in doc.get("providers", {})
        assert doc["providers"] == {"openai": {"base_url": "f", "api_key": "foreign"}}
        assert doc["theme"] == "dark"

    def test_use_default_refuses_when_key_changed_externally(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan, state=lock.state)

        doc = _read(paths)
        doc["providers"]["zai"]["api_key"] = "user-rotated"
        JsonBackend.write(paths.crush, doc)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            decisions, retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan, state=lock.state)

        doc = _read(paths)
        assert doc["providers"]["zai"]["api_key"] == "user-rotated"

    def test_use_default_preserves_external_base_url_edit(self, tool, tmp_path):
        """Regression (cycle-review #38): if the user edits base_url externally
        while api_key is still ours, ``use default`` must REFUSE on base_url
        (keep the edit) and NOT collapse the entry — even though api_key is
        RESTORE'd to absent. The entry survives with the user's base_url."""
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan, state=lock.state)

        # External edit: only base_url changes; api_key still ours.
        doc = _read(paths)
        doc["providers"]["zai"]["base_url"] = "https://user.custom/v4"
        JsonBackend.write(paths.crush, doc)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            decisions, retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan, state=lock.state)

        doc = _read(paths)
        # base_url REFUSE'd → preserved; api_key RESTORE'd-to-absent → gone;
        # entry KEPT (has the value-carrying base_url), not collapsed.
        entry = doc["providers"]["zai"]
        assert entry["base_url"] == "https://user.custom/v4"
        assert "api_key" not in entry

    def test_use_default_preserves_foreign_field_in_zai_entry(self, tool, tmp_path):
        """Regression (cycle-review #38): a foreign field the user added into
        ``providers.zai`` must survive revert even when both managed fields are
        RESTORE'd to absent."""
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan, state=lock.state)

        # User adds a foreign field into the zai entry.
        doc = _read(paths)
        doc["providers"]["zai"]["custom"] = "keep-me"
        JsonBackend.write(paths.crush, doc)

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            decisions, retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan, state=lock.state)

        doc = _read(paths)
        # Entry survives with the foreign field; managed fields gone.
        entry = doc["providers"]["zai"]
        assert entry["custom"] == "keep-me"
        assert "api_key" not in entry
        assert "base_url" not in entry

    def test_independent_of_opencode_and_claude_code(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        JsonBackend.write(paths.opencode, {"$schema": "keep", "provider": {}})
        JsonBackend.write(paths.claude_settings, {"env": {"ANTHROPIC_AUTH_TOKEN": "cc"}})

        with ProcessLock(paths) as lock:
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan, state=lock.state)

        assert JsonBackend.read(paths.opencode) == {"$schema": "keep", "provider": {}}
        assert JsonBackend.read(paths.claude_settings) == {"env": {"ANTHROPIC_AUTH_TOKEN": "cc"}}
