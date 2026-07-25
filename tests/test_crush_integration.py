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
        paths = Paths.from_home(tmp_path)
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)
        entry = _read(paths)["providers"]["zai"]
        assert entry["api_key"] == TOKEN
        assert entry["base_url"] == "https://api.z.ai/api/coding/paas/v4"
        assert entry["id"] == "zai"

    def test_use_zai_is_idempotent(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            apply_plan_locked(paths, tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN))
            state2 = tool.read_state(paths)
            plan2 = tool.plan_zai(spec, Region.GLOBAL, state=state2, auth_token=TOKEN)
        assert plan2.is_empty

    def test_use_default_restores_prior_via_journal(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        seed = {
            "providers": {"openai": {"base_url": "f", "api_key": "foreign"}},
            "theme": "dark",
        }
        JsonBackend.write(paths.crush, seed)
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read(paths)
        # zai gone; foreign provider + theme restored.
        assert "zai" not in doc.get("providers", {})
        assert doc["providers"] == {"openai": {"base_url": "f", "api_key": "foreign"}}
        assert doc["theme"] == "dark"

    def test_use_default_refuses_when_key_changed_externally(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan)

        doc = _read(paths)
        doc["providers"]["zai"]["api_key"] = "user-rotated"
        JsonBackend.write(paths.crush, doc)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read(paths)
        assert doc["providers"]["zai"]["api_key"] == "user-rotated"

    def test_independent_of_opencode_and_claude_code(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        JsonBackend.write(paths.opencode, {"$schema": "keep", "provider": {}})
        JsonBackend.write(paths.claude_settings, {"env": {"ANTHROPIC_AUTH_TOKEN": "cc"}})

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        assert JsonBackend.read(paths.opencode) == {"$schema": "keep", "provider": {}}
        assert JsonBackend.read(paths.claude_settings) == {"env": {"ANTHROPIC_AUTH_TOKEN": "cc"}}
