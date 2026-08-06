"""Integration tests for the OpenCode tool: HOME-isolated apply → revert cycle
through the Tool interface and the ownership journal (ADR-004 / ADR-005).

These exercise the OpenCodeTool adapter end-to-end against a tmp HOME: read
state → plan → capture ownership → commit (via the same apply_plan_locked the
CLI uses) → then journal-aware revert → assert restored. No real $HOME, no
network.
"""

from __future__ import annotations

import pytest

from zai_python_helper.backends import JsonBackend
from zai_python_helper.ownership import OwnershipJournal
from zai_python_helper.patchplan import ProcessLock, apply_plan_locked
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.tools import get_tool
from zai_python_helper.tools.opencode import OpenCodeTool

TOKEN = "sk-integration-token"
GLOBAL_NAME = "zai-coding-plan"
CHINA_NAME = "zhipuai-coding-plan"


@pytest.fixture
def tool() -> OpenCodeTool:
    return get_tool("opencode")  # type: ignore[return-value]


def _spec():
    # OpenCode ignores model-mode; pass a minimal valid spec.
    from zai_python_helper.core.domain import ModelMode, ProviderSpec

    return ProviderSpec(base_url="https://api.z.ai/api/anthropic", model_mode=ModelMode.ORIGINAL)


def _read_doc(paths):
    return JsonBackend.read(paths.opencode)


class TestApplyAndRevert:
    def test_use_zai_writes_exact_config(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        doc = _read_doc(paths)
        assert doc["provider"][GLOBAL_NAME] == {"options": {"apiKey": TOKEN}}
        assert doc["model"] == "zai-coding-plan/glm-4.6"
        assert doc["small_model"] == "zai-coding-plan/glm-4.5-air"

    def test_use_zai_is_idempotent(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan1 = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan1)
            state2 = tool.read_state(paths)
            plan2 = tool.plan_zai(spec, Region.GLOBAL, state=state2, auth_token=TOKEN)
        assert plan2.is_empty  # second activation: nothing to do

    def test_use_default_restores_prior_via_journal(self, tool, tmp_path):
        """use zai then use default restores the pre-activation state."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        # Seed a foreign provider + a foreign top-level key.
        seed = {
            "$schema": "keep",
            "provider": {"openai": {"options": {"apiKey": "foreign"}}},
            "theme": "dark",
        }
        JsonBackend.write(paths.opencode, seed)

        journal = OwnershipJournal(paths.ownership_json)

        # 1) use zai
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            current = journal.read()
            journal.write(_merge(tool, current, records))
            apply_plan_locked(paths, plan)

        # 2) use default (journal-aware)
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions, _retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read_doc(paths)
        # Coding-plan provider gone; foreign provider + schema + theme restored.
        assert "zai-coding-plan" not in doc.get("provider", {})
        assert doc["provider"] == {"openai": {"options": {"apiKey": "foreign"}}}
        assert doc["$schema"] == "keep"
        assert doc["theme"] == "dark"
        # Our model strings removed (they referenced a coding-plan provider
        # prefix only if it contained "coding-plan"; "zai/glm-4.6" does NOT,
        # so the blind planner keeps them — but journal revert RESTOREs the
        # prior which was absent → they are removed).
        assert "model" not in doc
        assert "small_model" not in doc

    def test_use_default_refuses_when_key_changed_externally(self, tool, tmp_path):
        """If the apiKey changed externally since activation, revert leaves it."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan)

        # External edit: rotate the apiKey out from under us.
        doc = _read_doc(paths)
        doc["provider"][GLOBAL_NAME]["options"]["apiKey"] = "user-rotated"
        JsonBackend.write(paths.opencode, doc)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions, _retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read_doc(paths)
        # The externally-set key is NOT clobbered.
        assert doc["provider"][GLOBAL_NAME]["options"]["apiKey"] == "user-rotated"

    def test_independent_of_claude_code(self, tool, tmp_path):
        """Disabling OpenCode does not affect Claude Code files (and vice versa)."""
        paths = Paths.from_home(tmp_path)
        # Claude Code settings exist and are untouched.
        JsonBackend.write(paths.claude_settings, {"env": {"ANTHROPIC_AUTH_TOKEN": "cc-tok"}})

        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        # Claude Code settings round-trip unchanged.
        cc = JsonBackend.read(paths.claude_settings)
        assert cc == {"env": {"ANTHROPIC_AUTH_TOKEN": "cc-tok"}}

    def test_duplicate_state_activation_refused(self, tool, tmp_path):
        """Issue #50 / Bug 4 edge (integration): a duplicate-state seed (BOTH
        regional providers with distinct credentials) must NOT proceed through
        ``use zai``. A region switch would silently destroy one entry because
        the ownership journal keys the apiKey under a single fixed logical name
        and cannot round-trip two regional names. ``plan_zai`` refuses the
        activation (ConfigurationError) instead of guessing; the on-disk doc is
        left untouched (non-destructive). Both insertion orders refused."""
        from zai_python_helper.errors import ConfigurationError

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        seed = {
            "$schema": "keep",
            "provider": {
                GLOBAL_NAME: {
                    "options": {"apiKey": "user-global-key"},
                    "baseURL": "https://user.global",
                },
                CHINA_NAME: {
                    "options": {"apiKey": "user-china-key"},
                    "baseURL": "https://user.china",
                    "models": {"glm-4.6": {}},
                },
            },
        }
        JsonBackend.write(paths.opencode, seed)

        # Activating EITHER region is refused from a dual-provider seed.
        for region in (Region.GLOBAL, Region.CHINA):
            with ProcessLock(paths.lock_file):
                state = tool.read_state(paths)
                with pytest.raises(ConfigurationError):
                    tool.plan_zai(spec, region, state=state, auth_token=TOKEN)

        # The seed is left exactly as-is — no silent data loss.
        assert _read_doc(paths) == seed

    def test_use_default_does_not_clear_duplicate_state(self, tool, tmp_path):
        """Pins the documented recovery contract: ``use default`` does NOT
        resolve a duplicate-state doc — a hand edit is the only exit.

        The CLI's ``use default`` routes through the journal-aware
        ``plan_revert``, which infers ONE region by first-match and therefore
        only ever touches that entry. On an unowned duplicate seed every
        decision is REFUSE, so the doc round-trips byte-identical and a
        following ``use zai`` still hits the guard. This test exists so the
        docstring/error-message claim cannot silently drift back to the false
        'run use default then use zai' recovery."""
        from zai_python_helper.errors import ConfigurationError

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        seed = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "user-global-key"}},
                CHINA_NAME: {"options": {"apiKey": "user-china-key"}},
            },
        }
        JsonBackend.write(paths.opencode, seed)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            journal_records = OwnershipJournal(paths.ownership_json).read()
            decisions, _ = tool.revert_decisions(journal_records, state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        # `use default` changed nothing — the duplicate state survives.
        assert _read_doc(paths) == seed

        # ...and `use zai` is therefore still refused: the user is not
        # unstuck by `use default`, exactly as the error message now states.
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            with pytest.raises(ConfigurationError):
                tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)


# ---------------------------------------------------------------------------
# helper: merge takeover records into the journal (mirrors cli._merge_takeover)
# ---------------------------------------------------------------------------


def _merge(tool, current, records):
    from zai_python_helper.ownership import take_over

    merged = current
    for key, prior_value, prior_present, set_hash in records:
        merged = take_over(merged, tool.name, key, prior_value, prior_present, set_hash)
    return merged
