"""Pure tests for the Crush planner (ADR-001: no IO)."""

from __future__ import annotations

from zai_python_helper.core.planner import DeltaKind, FileTag
from zai_python_helper.core.planner import crush as cr
from zai_python_helper.regions import Region

TOKEN = "sk-test-token-abc"
GLOBAL_PAAS = "https://api.z.ai/api/coding/paas/v4"
CHINA_PAAS = "https://open.bigmodel.cn/api/coding/paas/v4"


class TestPlanZai:
    def test_global_adds_zai_provider(self):
        plan = cr.plan_zai(Region.GLOBAL, crush_doc=None, auth_token=TOKEN)
        delta = plan.delta_for(FileTag.CRUSH)
        assert delta.kind == DeltaKind.WRITE_JSON
        entry = delta.content["providers"]["zai"]
        assert entry == {
            "id": "zai",
            "name": "ZAI Provider",
            "base_url": GLOBAL_PAAS,
            "api_key": TOKEN,
        }

    def test_china_uses_china_paas_endpoint(self):
        plan = cr.plan_zai(Region.CHINA, crush_doc=None, auth_token=TOKEN)
        entry = plan.delta_for(FileTag.CRUSH).content["providers"]["zai"]
        assert entry["base_url"] == CHINA_PAAS

    def test_preserves_foreign_providers_and_keys(self):
        seed = {
            "providers": {"openai": {"base_url": "x", "api_key": "foreign"}},
            "theme": "dark",
        }
        plan = cr.plan_zai(Region.GLOBAL, crush_doc=seed, auth_token=TOKEN)
        doc = plan.delta_for(FileTag.CRUSH).content
        assert doc["providers"]["openai"] == {"base_url": "x", "api_key": "foreign"}
        assert doc["theme"] == "dark"
        assert doc["providers"]["zai"]["api_key"] == TOKEN

    def test_deep_merges_user_keys_into_zai_entry(self):
        """Bug 3 regression: activation must DEEP-MERGE the Z.ai provider
        fields into any existing ``providers.zai`` entry, preserving foreign
        sibling keys the user set on it — not replace the whole entry."""
        seed = {
            "providers": {
                "zai": {"custom": "keep-me", "options_extra": {"nested": True}},
                "openai": {"api_key": "foreign"},
            }
        }
        plan = cr.plan_zai(Region.GLOBAL, crush_doc=seed, auth_token=TOKEN)
        entry = plan.delta_for(FileTag.CRUSH).content["providers"]["zai"]
        # Our managed fields set.
        assert entry["api_key"] == TOKEN
        assert entry["base_url"] == GLOBAL_PAAS
        assert entry["id"] == "zai"
        # User's foreign sibling keys preserved (not clobbered).
        assert entry["custom"] == "keep-me"
        assert entry["options_extra"] == {"nested": True}
        # Foreign provider untouched.
        assert plan.delta_for(FileTag.CRUSH).content["providers"]["openai"] == {
            "api_key": "foreign"
        }

    def test_idempotent_on_post_state(self):
        first = cr.plan_zai(Region.GLOBAL, crush_doc=None, auth_token=TOKEN)
        post = first.delta_for(FileTag.CRUSH).content
        second = cr.plan_zai(Region.GLOBAL, crush_doc=post, auth_token=TOKEN)
        assert second.is_empty

    def test_token_rotation_is_not_idempotent(self):
        first = cr.plan_zai(Region.GLOBAL, crush_doc=None, auth_token="A")
        post = first.delta_for(FileTag.CRUSH).content
        rotated = cr.plan_zai(Region.GLOBAL, crush_doc=post, auth_token="B")
        assert rotated.has_writes


class TestPlanDefault:
    def test_removes_zai_provider(self):
        seed = {"providers": {"zai": {"id": "zai", "api_key": "x"}}}
        plan = cr.plan_default(crush_doc=seed)
        assert "providers" not in plan.delta_for(FileTag.CRUSH).content

    def test_preserves_foreign_providers(self):
        seed = {
            "providers": {
                "zai": {"id": "zai", "api_key": "x"},
                "openai": {"base_url": "f"},
            }
        }
        plan = cr.plan_default(crush_doc=seed)
        doc = plan.delta_for(FileTag.CRUSH).content
        assert doc["providers"] == {"openai": {"base_url": "f"}}

    def test_idempotent_on_post_state(self):
        first = cr.plan_default(crush_doc={"providers": {"zai": {}}})
        post = first.delta_for(FileTag.CRUSH).content
        assert cr.plan_default(crush_doc=post).is_empty


class TestPostconditions:
    def test_active_when_zai_provider_matches_region(self):
        doc = {"providers": {"zai": {"api_key": "tok", "base_url": GLOBAL_PAAS}}}
        assert cr.postconditions(Region.GLOBAL, crush_doc=doc) is True

    def test_inactive_for_wrong_region(self):
        doc = {"providers": {"zai": {"api_key": "tok", "base_url": GLOBAL_PAAS}}}
        assert cr.postconditions(Region.CHINA, crush_doc=doc) is False

    def test_inactive_when_apikey_missing(self):
        doc = {"providers": {"zai": {"base_url": GLOBAL_PAAS}}}
        assert cr.postconditions(Region.GLOBAL, crush_doc=doc) is False

    def test_inactive_when_provider_missing(self):
        assert cr.postconditions(Region.GLOBAL, crush_doc={}) is False


class TestRegionHelpers:
    def test_paas_lookup(self):
        assert cr.paas_base_url_for_region(Region.GLOBAL) == GLOBAL_PAAS
        assert cr.paas_base_url_for_region(Region.CHINA) == CHINA_PAAS

    def test_revert_key_set(self):
        assert set(cr.revert_key_set()) == {
            "providers.zai.api_key",
            "providers.zai.base_url",
        }
