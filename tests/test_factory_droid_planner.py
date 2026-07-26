"""Pure tests for the Factory Droid planner (ADR-001: no IO)."""

from __future__ import annotations

from zai_python_helper.core.planner import DeltaKind, FileTag
from zai_python_helper.core.planner import factory_droid as fd
from zai_python_helper.regions import Region

TOKEN = "sk-test-token-abc"
GLOBAL_ANTHROPIC = "https://api.z.ai/api/anthropic"
GLOBAL_PAAS = "https://api.z.ai/api/coding/paas/v4"
CHINA_ANTHROPIC = "https://open.bigmodel.cn/api/anthropic"
CHINA_PAAS = "https://open.bigmodel.cn/api/coding/paas/v4"


def _entry_for(proto: str, models):
    for m in models:
        if fd._is_our_entry(m) and fd._protocol_of(m) == proto:
            return m
    return None


class TestPlanZai:
    def test_global_adds_two_protocol_entries(self):
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=None, auth_token=TOKEN)
        delta = plan.delta_for(FileTag.FACTORY_DROID)
        assert delta.kind == DeltaKind.WRITE_JSON
        models = delta.content["customModels"]
        assert len(models) == 2

        anth = _entry_for(fd.PROVIDER_ANTHROPIC, models)
        oai = _entry_for(fd.PROVIDER_OPENAI, models)
        assert anth is not None and oai is not None

        for e in (anth, oai):
            assert "GLM Coding Plan" in e["displayName"]
            assert e["model"] == "glm-4.7"
            assert e["maxOutputTokens"] == 131072
            assert e["apiKey"] == TOKEN
        assert anth["baseUrl"] == GLOBAL_ANTHROPIC
        assert oai["baseUrl"] == GLOBAL_PAAS

    def test_china_uses_china_endpoints(self):
        plan = fd.plan_zai(Region.CHINA, factory_doc=None, auth_token=TOKEN)
        models = plan.delta_for(FileTag.FACTORY_DROID).content["customModels"]
        assert _entry_for(fd.PROVIDER_ANTHROPIC, models)["baseUrl"] == CHINA_ANTHROPIC
        assert _entry_for(fd.PROVIDER_OPENAI, models)["baseUrl"] == CHINA_PAAS

    def test_preserves_foreign_entries_and_keys(self):
        seed = {
            "customModels": [
                {"displayName": "My Custom", "provider": "openai", "model": "gpt-4"},
            ],
            "theme": "dark",
        }
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.FACTORY_DROID).content
        models = doc["customModels"]
        # Foreign entry preserved + our two appended.
        assert any(m["displayName"] == "My Custom" for m in models)
        assert len(models) == 3
        assert doc["theme"] == "dark"

    def test_removes_prior_glm_entries_before_appending(self):
        """A repeat with stale GLM entries replaces, not duplicates."""
        seed = {
            "customModels": [
                {"displayName": "Old GLM Coding Plan (Anthropic)", "provider": "anthropic"},
                {"displayName": "Old GLM Coding Plan (OpenAI)", "provider": "openai"},
            ]
        }
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        models = plan.delta_for(FileTag.FACTORY_DROID).content["customModels"]
        ours = [m for m in models if fd._is_our_entry(m)]
        assert len(ours) == 2  # not 4
        assert all(m["apiKey"] == TOKEN for m in ours)

    def test_deep_merges_user_keys_into_our_entry(self):
        """Bug 3 regression: activation must DEEP-MERGE our managed fields
        into an existing GLM entry of the SAME protocol, preserving foreign
        sibling keys the user set on it — not replace the whole entry."""
        seed = {
            "customModels": [
                {
                    "displayName": "Z.ai GLM Coding Plan (Anthropic)",
                    "provider": "anthropic",
                    "custom": "keep-me",
                    "tools": ["shell", "edit"],
                }
            ]
        }
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        anth = next(
            m
            for m in plan.delta_for(FileTag.FACTORY_DROID).content["customModels"]
            if fd._is_our_entry(m) and fd._protocol_of(m) == fd.PROVIDER_ANTHROPIC
        )
        # Our managed fields set.
        assert anth["apiKey"] == TOKEN
        assert anth["baseUrl"] == GLOBAL_ANTHROPIC
        assert anth["model"] == "glm-4.7"
        # User's foreign sibling keys preserved (not clobbered).
        assert anth["custom"] == "keep-me"
        assert anth["tools"] == ["shell", "edit"]

    def test_idempotent_on_post_state(self):
        first = fd.plan_zai(Region.GLOBAL, factory_doc=None, auth_token=TOKEN)
        post = first.delta_for(FileTag.FACTORY_DROID).content
        assert fd.plan_zai(Region.GLOBAL, factory_doc=post, auth_token=TOKEN).is_empty

    def test_token_rotation_is_not_idempotent(self):
        first = fd.plan_zai(Region.GLOBAL, factory_doc=None, auth_token="A")
        post = first.delta_for(FileTag.FACTORY_DROID).content
        assert fd.plan_zai(Region.GLOBAL, factory_doc=post, auth_token="B").has_writes


class TestPlanDefault:
    def test_removes_glm_entries(self):
        seed = {
            "customModels": [
                {"displayName": "Z.ai GLM Coding Plan (Anthropic)", "provider": "anthropic"},
                {"displayName": "Z.ai GLM Coding Plan (OpenAI)", "provider": "openai"},
            ]
        }
        plan = fd.plan_default(factory_doc=seed)
        assert "customModels" not in plan.delta_for(FileTag.FACTORY_DROID).content

    def test_preserves_foreign_entries(self):
        seed = {
            "customModels": [
                {"displayName": "Z.ai GLM Coding Plan (Anthropic)", "provider": "anthropic"},
                {"displayName": "My Custom", "provider": "openai"},
            ]
        }
        plan = fd.plan_default(factory_doc=seed)
        models = plan.delta_for(FileTag.FACTORY_DROID).content["customModels"]
        assert [m["displayName"] for m in models] == ["My Custom"]

    def test_idempotent_on_post_state(self):
        first = fd.plan_default(
            factory_doc={"customModels": [{"displayName": "GLM Coding Plan (X)"}]}
        )
        post = first.delta_for(FileTag.FACTORY_DROID).content
        assert fd.plan_default(factory_doc=post).is_empty


class TestPostconditions:
    def test_active_when_both_entries_match_region(self):
        doc = {
            "customModels": [
                {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
                 "apiKey": "k", "baseUrl": GLOBAL_ANTHROPIC},
                {"displayName": "GLM Coding Plan (OpenAI)", "provider": "openai",
                 "apiKey": "k", "baseUrl": GLOBAL_PAAS},
            ]
        }
        assert fd.postconditions(Region.GLOBAL, factory_doc=doc) is True

    def test_inactive_for_wrong_region(self):
        doc = {
            "customModels": [
                {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
                 "apiKey": "k", "baseUrl": GLOBAL_ANTHROPIC},
                {"displayName": "GLM Coding Plan (OpenAI)", "provider": "openai",
                 "apiKey": "k", "baseUrl": GLOBAL_PAAS},
            ]
        }
        assert fd.postconditions(Region.CHINA, factory_doc=doc) is False

    def test_inactive_when_entry_missing(self):
        doc = {"customModels": [
            {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
             "apiKey": "k", "baseUrl": GLOBAL_ANTHROPIC}
        ]}
        assert fd.postconditions(Region.GLOBAL, factory_doc=doc) is False


class TestRegionHelpers:
    def test_endpoint_lookups(self):
        assert fd.anthropic_base_url_for_region(Region.GLOBAL) == GLOBAL_ANTHROPIC
        assert fd.anthropic_base_url_for_region(Region.CHINA) == CHINA_ANTHROPIC
        assert fd.paas_base_url_for_region(Region.GLOBAL) == GLOBAL_PAAS
        assert fd.paas_base_url_for_region(Region.CHINA) == CHINA_PAAS

    def test_revert_key_set_stable_by_protocol(self):
        assert set(fd.revert_key_set()) == {
            "customModels.anthropic.apiKey",
            "customModels.openai.apiKey",
        }
