"""Integration tests for the Factory Droid tool: HOME-isolated apply → revert
cycle through the Tool interface and the ownership journal.
"""

from __future__ import annotations

import pytest

from zai_python_helper.backends import JsonBackend
from zai_python_helper.core.domain import ModelMode, ProviderSpec
from zai_python_helper.core.planner import factory_droid as fd
from zai_python_helper.ownership import OwnershipJournal, take_over
from zai_python_helper.patchplan import ProcessLock, apply_plan_locked
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.tools import get_tool
from zai_python_helper.tools.factory_droid import FactoryDroidTool

TOKEN = "sk-integration-token"


@pytest.fixture
def tool() -> FactoryDroidTool:
    return get_tool("factory_droid")  # type: ignore[return-value]


def _spec() -> ProviderSpec:
    return ProviderSpec(base_url="https://api.z.ai/api/anthropic", model_mode=ModelMode.ORIGINAL)


def _read(paths):
    return JsonBackend.read(paths.factory_droid)


def _merge(tool, current, records):
    merged = current
    for key, prior_value, prior_present, set_hash in records:
        merged = take_over(merged, tool.name, key, prior_value, prior_present, set_hash)
    return merged


class TestApplyAndRevert:
    def test_use_zai_writes_two_entries(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)
        models = _read(paths)["customModels"]
        ours = [m for m in models if fd._is_our_entry(m)]
        assert len(ours) == 2
        assert all(m["apiKey"] == TOKEN for m in ours)
        assert all(m["model"] == "glm-4.7" for m in ours)

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
            "customModels": [{"displayName": "My Custom", "provider": "openai", "model": "gpt"}],
            "theme": "dark",
        }
        JsonBackend.write(paths.factory_droid, seed)
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
        # Our two entries gone; foreign entry + theme preserved.
        models = doc["customModels"]
        assert [m["displayName"] for m in models] == ["My Custom"]
        assert doc["theme"] == "dark"

    def test_use_default_refuses_when_key_changed_externally(self, tool, tmp_path):
        """External rotation of an entry's apiKey → REFUSE keeps the new value."""
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
        for m in doc["customModels"]:
            if fd._is_our_entry(m):
                m["apiKey"] = "user-rotated"
        JsonBackend.write(paths.factory_droid, doc)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read(paths)
        ours = [m for m in doc["customModels"] if fd._is_our_entry(m)]
        # Externally-set keys preserved (REFUSE); entries kept.
        assert all(m["apiKey"] == "user-rotated" for m in ours)
        assert len(ours) == 2

    def test_use_default_preserves_foreign_field_in_our_entry(self, tool, tmp_path):
        """Regression (cycle-review #39): a foreign field the user added into
        one of OUR customModels entries must survive revert (analogous to the
        Crush #38 collapse fix). The entry is de-marked (our fields stripped)
        but the user's field is kept, not clobbered."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan)

        # User adds a foreign field into the anthropic entry.
        doc = _read(paths)
        for m in doc["customModels"]:
            if fd._is_our_entry(m) and fd._protocol_of(m) == fd.PROVIDER_ANTHROPIC:
                m["extra"] = "keep-me"
        JsonBackend.write(paths.factory_droid, doc)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read(paths)
        models = doc["customModels"]
        # The anthropic entry is gone as "ours", but the foreign field survives
        # as a de-marked entry; the openai entry is fully removed (no foreign).
        flat = [item for m in models for item in m.items()]
        assert ("extra", "keep-me") in flat
        assert not any(fd._is_our_entry(m) for m in models)

    def test_independent_of_other_tools(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        JsonBackend.write(paths.crush, {"providers": {"openai": {"api_key": "x"}}})
        JsonBackend.write(paths.claude_settings, {"env": {"ANTHROPIC_AUTH_TOKEN": "cc"}})

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        assert JsonBackend.read(paths.crush) == {"providers": {"openai": {"api_key": "x"}}}
        assert JsonBackend.read(paths.claude_settings) == {"env": {"ANTHROPIC_AUTH_TOKEN": "cc"}}
