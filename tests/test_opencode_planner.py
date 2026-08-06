"""Pure tests for the OpenCode planner (ADR-001: no IO).

Seeded parsed documents → :class:`PatchPlan` assertions. These tests never
touch the filesystem, never read the environment, and never prompt — they
exercise the pure transforms in :mod:`zai_python_helper.core.planner.opencode`.
"""

from __future__ import annotations

import pytest

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
        # Bug 2 regression: model MUST reference the configured provider
        # (zai-coding-plan), not a bare "zai" — else OpenCode can't resolve it
        # and postcondition fails on our own output.
        assert doc["model"] == "zai-coding-plan/glm-4.6"
        assert doc["small_model"] == "zai-coding-plan/glm-4.5-air"
        # And the postcondition holds on the planned doc (Bug 2 end-to-end).
        assert oc.postconditions(Region.GLOBAL, opencode_doc=doc) is True

    def test_china_uses_zhipuai_provider_name(self):
        plan = oc.plan_zai(Region.CHINA, opencode_doc=None, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert doc["provider"][CHINA_NAME] == {"options": {"apiKey": TOKEN}}
        assert GLOBAL_NAME not in doc["provider"]
        assert doc["model"] == "zhipuai-coding-plan/glm-4.6"
        assert oc.postconditions(Region.CHINA, opencode_doc=doc) is True

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
            "model": "zai-coding-plan/glm-4.6",
            "small_model": "zai-coding-plan/glm-4.5-air",
        }
        plan = oc.plan_zai(Region.CHINA, opencode_doc=seed, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.OPENCODE).content
        assert GLOBAL_NAME not in doc["provider"]  # stale global removed
        assert doc["provider"][CHINA_NAME] == {"options": {"apiKey": TOKEN}}

    def test_preserves_foreign_provider_with_coding_plan_substring(self):
        """A foreign provider whose name merely CONTAINS ``coding-plan``
        (e.g. ``my-coding-plan-proxy``) must NOT be treated as ours. Only the
        two exact regional names are managed — a substring match would
        destroy the user's foreign provider (ADR-004: do not clobber)."""
        seed = {
            "provider": {
                "my-coding-plan-proxy": {
                    "options": {"apiKey": "user-foreign"},
                    "baseURL": "https://user.proxy",
                },
            },
        }
        plan = oc.plan_zai(Region.GLOBAL, opencode_doc=seed, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.OPENCODE).content
        # Foreign provider round-trips untouched.
        assert doc["provider"]["my-coding-plan-proxy"]["options"]["apiKey"] == "user-foreign"
        assert doc["provider"]["my-coding-plan-proxy"]["baseURL"] == "https://user.proxy"
        # And the real coding-plan provider is added alongside it.
        assert doc["provider"][GLOBAL_NAME] == {"options": {"apiKey": TOKEN}}

    def test_refuses_duplicate_regional_state_global(self):
        """Issue #50 / Bug 4 edge: a duplicate-state seed (BOTH regional
        provider names present at once with distinct credentials) is
        ambiguous, and a region switch would silently destroy one entry's
        identity (the journal keys the apiKey under a single fixed logical
        name and cannot round-trip two regional names through revert). So
        ``plan_zai`` REFUSES the activation (fail-closed) rather than guess
        and lose data. The user resolves the duplicate by hand. Both
        insertion orders must trip the guard (issue #50 acceptance)."""
        from zai_python_helper.errors import ConfigurationError

        seed = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "user-global-key"}},
                CHINA_NAME: {"options": {"apiKey": "user-china-key"}},
            },
        }
        assert oc.has_duplicate_regional_providers(seed) is True
        with pytest.raises(ConfigurationError):
            oc.plan_zai(Region.GLOBAL, opencode_doc=seed, auth_token=TOKEN)

    def test_refuses_duplicate_regional_state_china(self):
        """The duplicate-state guard fires for EITHER target region — the
        ambiguity is in the seed (two managed names), not in which name we
        are activating. Reversed insertion order too (issue #50 acceptance:
        both insertion orders)."""
        from zai_python_helper.errors import ConfigurationError

        seed = {
            "provider": {
                CHINA_NAME: {"options": {"apiKey": "user-china-key"}},
                GLOBAL_NAME: {"options": {"apiKey": "user-global-key"}},
            },
        }
        assert oc.has_duplicate_regional_providers(seed) is True
        with pytest.raises(ConfigurationError):
            oc.plan_zai(Region.CHINA, opencode_doc=seed, auth_token=TOKEN)

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
            "model": "zai-coding-plan/glm-4.6",
            "small_model": "zai-coding-plan/glm-4.5-air",
        }
        first = oc.plan_zai(Region.GLOBAL, opencode_doc=seed, auth_token=TOKEN)
        post = first.delta_for(FileTag.OPENCODE).content
        second = oc.plan_zai(Region.GLOBAL, opencode_doc=post, auth_token=TOKEN)
        assert second.is_empty

    def test_deep_merges_user_keys_into_provider_entry(self):
        """Bug 3 regression: activation must DEEP-MERGE the apiKey into the
        provider entry, preserving foreign keys the user set on it (timeout,
        concurrency, nested models) — not replace the whole entry."""
        seed = {
            "provider": {
                GLOBAL_NAME: {
                    "options": {"apiKey": "old", "timeout": 30},
                    "models": {"glm-4.6": {"toolCallStreaming": True}},
                    "npm": None,
                }
            },
        }
        plan = oc.plan_zai(Region.GLOBAL, opencode_doc=seed, auth_token=TOKEN)
        entry = plan.delta_for(FileTag.OPENCODE).content["provider"][GLOBAL_NAME]
        # apiKey updated; user's timeout preserved (deep-merge under options).
        assert entry["options"]["apiKey"] == TOKEN
        assert entry["options"]["timeout"] == 30
        # Sibling user keys preserved (not clobbered by the apiKey write).
        assert entry["models"] == {"glm-4.6": {"toolCallStreaming": True}}
        assert entry["npm"] is None

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


# ---------------------------------------------------------------------------
# Duplicate-state detection (issue #50, Bug 4 edge)
# ---------------------------------------------------------------------------


class TestDuplicateRegionalState:
    """``has_duplicate_regional_providers`` gates the fail-closed activation
    refusal (issue #50). The condition is "BOTH managed regional names present"
    — symmetric in insertion order and independent of content equality, because
    two coexisting managed names is itself the state we cannot round-trip."""

    def test_true_when_both_regional_names_present(self):
        doc = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "g"}},
                CHINA_NAME: {"options": {"apiKey": "c"}},
            }
        }
        assert oc.has_duplicate_regional_providers(doc) is True

    def test_true_when_both_present_reversed_order(self):
        doc = {
            "provider": {
                CHINA_NAME: {"options": {"apiKey": "c"}},
                GLOBAL_NAME: {"options": {"apiKey": "g"}},
            }
        }
        assert oc.has_duplicate_regional_providers(doc) is True

    def test_false_when_only_global_present(self):
        doc = {"provider": {GLOBAL_NAME: {"options": {"apiKey": "g"}}}}
        assert oc.has_duplicate_regional_providers(doc) is False

    def test_false_when_only_china_present(self):
        doc = {"provider": {CHINA_NAME: {"options": {"apiKey": "c"}}}}
        assert oc.has_duplicate_regional_providers(doc) is False

    def test_false_when_one_regional_plus_foreign(self):
        """A foreign provider alongside ONE regional name is not duplicate-state."""
        doc = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "g"}},
                "openai": {"options": {"apiKey": "f"}},
            }
        }
        assert oc.has_duplicate_regional_providers(doc) is False

    def test_false_for_foreign_provider_with_coding_plan_substring(self):
        """The detector uses exact-name matching: a foreign provider whose
        name merely contains ``coding-plan`` is NOT a second managed regional
        provider (ADR-004 exact-match contract)."""
        doc = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "g"}},
                "my-coding-plan-proxy": {"options": {"apiKey": "p"}},
            }
        }
        assert oc.has_duplicate_regional_providers(doc) is False

    def test_false_for_empty_or_absent_doc(self):
        assert oc.has_duplicate_regional_providers({}) is False
        assert oc.has_duplicate_regional_providers(None) is False
