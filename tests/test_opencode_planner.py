"""Pure tests for the OpenCode planner (ADR-001: no IO).

Seeded parsed documents → :class:`PatchPlan` assertions. These tests never
touch the filesystem, never read the environment, and never prompt — they
exercise the pure transforms in :mod:`zai_python_helper.core.planner.opencode`.
"""

from __future__ import annotations

from zai_python_helper.core.planner import DeltaKind, FileTag
from zai_python_helper.core.planner import opencode as oc
from zai_python_helper.regions import Region

TOKEN = "sk-test-token-abc"
GLOBAL_NAME = "zai-coding-plan"
CHINA_NAME = "zhipuai-coding-plan"


# ---------------------------------------------------------------------------
# plan_zai
# ---------------------------------------------------------------------------


class TestPlanZai:
    def test_global_adds_coding_plan_provider_and_models(self):
        plan = oc.plan_zai(Region.GLOBAL, opencode_doc=None, auth_token=TOKEN)
        delta = plan.delta_for(FileTag.OPENCODE)
        assert delta.kind == DeltaKind.WRITE_JSON
        doc = delta.content
        assert doc["provider"][GLOBAL_NAME] == {"options": {"apiKey": TOKEN}}
        assert doc["model"] == "zai/glm-4.6"
        assert doc["small_model"] == "zai/glm-4.5-air"

    def test_china_uses_zhipuai_provider_name(self):
        plan = oc.plan_zai(Region.CHINA, opencode_doc=None, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert doc["provider"][CHINA_NAME] == {"options": {"apiKey": TOKEN}}
        assert GLOBAL_NAME not in doc["provider"]

    def test_preserves_schema_and_foreign_providers(self):
        seed = {
            "$schema": "https://example/schema.json",
            "provider": {"openai": {"options": {"apiKey": "foreign-key"}}},
            "theme": "dark",
        }
        plan = oc.plan_zai(Region.GLOBAL, opencode_doc=seed, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert doc["$schema"] == "https://example/schema.json"
        assert doc["provider"]["openai"] == {"options": {"apiKey": "foreign-key"}}
        assert doc["theme"] == "dark"
        # And the coding-plan provider is added.
        assert doc["provider"][GLOBAL_NAME] == {"options": {"apiKey": TOKEN}}

    def test_removes_prior_coding_plan_provider_on_region_switch(self):
        """A global→china switch must not leave the stale global provider."""
        seed = {
            "provider": {GLOBAL_NAME: {"options": {"apiKey": "old"}}},
            "model": "zai/glm-4.6",
            "small_model": "zai/glm-4.5-air",
        }
        plan = oc.plan_zai(Region.CHINA, opencode_doc=seed, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert GLOBAL_NAME not in doc["provider"]  # stale global removed
        assert doc["provider"][CHINA_NAME] == {"options": {"apiKey": TOKEN}}

    def test_idempotent_on_post_state(self):
        """A second plan_zai on the first plan's post-state is a NOOP."""
        first = oc.plan_zai(Region.GLOBAL, opencode_doc=None, auth_token=TOKEN)
        post = first.delta_for(FileTag.OPENCODE).content
        second = oc.plan_zai(Region.GLOBAL, opencode_doc=post, auth_token=TOKEN)
        assert second.is_empty

    def test_idempotent_with_foreign_content(self):
        """Idempotent even with foreign providers/keys present."""
        seed = {
            "$schema": "x",
            "provider": {"openai": {"options": {"apiKey": "f"}}},
            "model": "zai/glm-4.6",
            "small_model": "zai/glm-4.5-air",
        }
        first = oc.plan_zai(Region.GLOBAL, opencode_doc=seed, auth_token=TOKEN)
        post = first.delta_for(FileTag.OPENCODE).content
        second = oc.plan_zai(Region.GLOBAL, opencode_doc=post, auth_token=TOKEN)
        assert second.is_empty

    def test_token_rotation_is_not_idempotent(self):
        """A different token is a genuine change → non-NOOP."""
        first = oc.plan_zai(Region.GLOBAL, opencode_doc=None, auth_token="tok-A")
        post = first.delta_for(FileTag.OPENCODE).content
        rotated = oc.plan_zai(Region.GLOBAL, opencode_doc=post, auth_token="tok-B")
        assert rotated.has_writes


# ---------------------------------------------------------------------------
# plan_default (blind inverse)
# ---------------------------------------------------------------------------


class TestPlanDefault:
    def test_removes_coding_plan_provider(self):
        seed = {"provider": {GLOBAL_NAME: {"options": {"apiKey": "x"}}}}
        plan = oc.plan_default(opencode_doc=seed)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert "provider" not in doc  # providers empty → dropped

    def test_preserves_foreign_providers(self):
        seed = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "x"}},
                "openai": {"options": {"apiKey": "f"}},
            }
        }
        plan = oc.plan_default(opencode_doc=seed)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert doc["provider"] == {"openai": {"options": {"apiKey": "f"}}}

    def test_clears_model_referencing_coding_plan(self):
        seed = {"model": "zai-coding-plan/glm-4.6"}
        plan = oc.plan_default(opencode_doc=seed)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert "model" not in doc

    def test_keeps_model_pointing_at_foreign_provider(self):
        """A model string that does NOT reference a coding-plan provider stays."""
        seed = {"model": "openai/gpt-4", "small_model": "openai/gpt-3.5"}
        plan = oc.plan_default(opencode_doc=seed)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert doc["model"] == "openai/gpt-4"
        assert doc["small_model"] == "openai/gpt-3.5"

    def test_idempotent_on_post_state(self):
        first = oc.plan_default(opencode_doc={"provider": {GLOBAL_NAME: {}}})
        post = first.delta_for(FileTag.OPENCODE).content
        second = oc.plan_default(opencode_doc=post)
        assert second.is_empty

    def test_both_provider_names_removed(self):
        """A doc with BOTH coding-plan names (edge) → both removed."""
        seed = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "a"}},
                CHINA_NAME: {"options": {"apiKey": "b"}},
            }
        }
        plan = oc.plan_default(opencode_doc=seed)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert "provider" not in doc


# ---------------------------------------------------------------------------
# postconditions
# ---------------------------------------------------------------------------


class TestPostconditions:
    def test_active_when_provider_and_model_present(self):
        doc = {
            "provider": {GLOBAL_NAME: {"options": {"apiKey": "tok"}}},
            "model": "zai-coding-plan/glm-4.6",
        }
        assert oc.postconditions(Region.GLOBAL, opencode_doc=doc) is True

    def test_inactive_when_provider_missing(self):
        doc = {"model": "zai-coding-plan/glm-4.6"}
        assert oc.postconditions(Region.GLOBAL, opencode_doc=doc) is False

    def test_inactive_when_model_does_not_reference_coding_plan(self):
        doc = {
            "provider": {GLOBAL_NAME: {"options": {"apiKey": "tok"}}},
            "model": "openai/gpt-4",
        }
        assert oc.postconditions(Region.GLOBAL, opencode_doc=doc) is False

    def test_inactive_for_wrong_region(self):
        doc = {
            "provider": {GLOBAL_NAME: {"options": {"apiKey": "tok"}}},
            "model": "zai-coding-plan/glm-4.6",
        }
        # Active for GLOBAL but the china provider name is absent.
        assert oc.postconditions(Region.CHINA, opencode_doc=doc) is False


# ---------------------------------------------------------------------------
# region helpers
# ---------------------------------------------------------------------------


class TestRegionHelpers:
    def test_provider_name_lookup(self):
        assert oc.provider_name_for_region(Region.GLOBAL) == GLOBAL_NAME
        assert oc.provider_name_for_region(Region.CHINA) == CHINA_NAME

    def test_all_provider_names_covers_both(self):
        assert set(oc.ALL_PROVIDER_NAMES) == {GLOBAL_NAME, CHINA_NAME}

    def test_revert_key_set_is_stable_logical_names(self):
        """Journal keys are fixed logical names (not region-specific)."""
        assert set(oc.revert_key_set()) == {
            "provider.apiKey",
            "model",
            "small_model",
        }
