"""Pure tests for the Factory Droid planner (ADR-001: no IO)."""

from __future__ import annotations

import pytest

from zai_python_helper.core.planner import DeltaKind, FileTag
from zai_python_helper.core.planner import factory_droid as fd
from zai_python_helper.errors import ValidationError
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

    # --- entry-identity guards (issue #53) — fail closed on lossy state ---

    def test_activation_refuses_conflicting_managed_model(self):
        """F2: pre-existing GLM entry with a DIFFERENT model → raise, not
        silently overwrite it (the journal cannot restore model)."""
        seed = {"customModels": [{
            "displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
            "model": "user-custom-model",  # drift (canonical: glm-4.7)
            "baseUrl": GLOBAL_ANTHROPIC, "apiKey": "USER",
        }]}
        with pytest.raises(ValidationError, match="model"):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_activation_refuses_conflicting_managed_baseurl(self):
        """F2: pre-existing GLM entry with a genuinely foreign baseUrl → raise
        (a known regional URL would be a cross-region switch, not drift)."""
        seed = {"customModels": [{
            "displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
            "model": "glm-4.7",
            "baseUrl": "https://user.custom.endpoint",  # foreign → drift
            "apiKey": "USER",
        }]}
        with pytest.raises(ValidationError, match="baseUrl"):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_activation_refuses_conflicting_managed_maxtokens(self):
        """F2: pre-existing GLM entry with a different maxOutputTokens → raise
        (canonical is 131072)."""
        seed = {"customModels": [{
            "displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
            "model": "glm-4.7", "baseUrl": GLOBAL_ANTHROPIC,
            "maxOutputTokens": 8192,  # drift
            "apiKey": "USER",
        }]}
        with pytest.raises(ValidationError, match="maxOutputTokens"):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_activation_refuses_duplicate_marker_protocol(self):
        """F3: two GLM anthropic entries → raise (which is ours is ambiguous)."""
        seed = {"customModels": [
            {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic"},
            {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic"},
        ]}
        with pytest.raises(ValidationError, match="multiple"):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_activation_foreign_glm_before_helper_raises(self):
        """F3 + F2 combined worst case: a foreign GLM entry (with drift)
        inserted BEFORE our helper entry must raise rather than silently
        overwrite the wrong entry."""
        seed = {"customModels": [
            {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
             "model": "foreign-model", "baseUrl": "https://foreign"},
            {"displayName": "Z.ai GLM Coding Plan (Anthropic)", "provider": "anthropic",
             "model": "glm-4.7", "baseUrl": GLOBAL_ANTHROPIC, "apiKey": "old"},
        ]}
        with pytest.raises(ValidationError):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_activation_region_switch_is_not_drift(self):
        """A baseUrl that is ANOTHER region's canonical URL is a cross-region
        re-activation, not user customization — must NOT raise (drift only on
        genuinely foreign URLs). Regression guard for the global↔china switch.
        """
        # Activate GLOBAL, then re-activate CHINA on the GLOBAL post-state.
        first = fd.plan_zai(Region.GLOBAL, factory_doc=None, auth_token=TOKEN)
        post = first.delta_for(FileTag.FACTORY_DROID).content
        # post holds GLOBAL urls; CHINA canonical differs — but GLOBAL url is a
        # known regional url → no drift → CHINA activation proceeds (not raises).
        again = fd.plan_zai(Region.CHINA, factory_doc=post, auth_token=TOKEN)
        assert again.has_writes
        anth = _entry_for(
            fd.PROVIDER_ANTHROPIC,
            again.delta_for(FileTag.FACTORY_DROID).content["customModels"],
        )
        assert anth["baseUrl"] == CHINA_ANTHROPIC

    def test_activation_post_state_idempotent_not_raise(self):
        """The drift guard must NOT fire on our own post-state (canonical
        managed values) — companion to test_idempotent_on_post_state."""
        first = fd.plan_zai(Region.GLOBAL, factory_doc=None, auth_token=TOKEN)
        post = first.delta_for(FileTag.FACTORY_DROID).content
        again = fd.plan_zai(Region.GLOBAL, factory_doc=post, auth_token=TOKEN)
        assert again.is_empty

    def test_activation_rotated_token_not_raise(self):
        """apiKey drift is token rotation (apiKey is journaled), NOT a managed-
        field conflict — must not raise; it's a legitimate write."""
        first = fd.plan_zai(Region.GLOBAL, factory_doc=None, auth_token="A")
        post = first.delta_for(FileTag.FACTORY_DROID).content
        again = fd.plan_zai(Region.GLOBAL, factory_doc=post, auth_token="B")
        assert again.has_writes


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

    def test_default_refuses_duplicate_marker_protocol(self):
        """F3: use default with duplicate GLM entries → raise (the blind
        inverse cannot tell ours from a foreign GLM entry and would destroy
        the wrong one)."""
        seed = {"customModels": [
            {"displayName": "GLM Coding Plan (OpenAI)", "provider": "openai"},
            {"displayName": "GLM Coding Plan (OpenAI)", "provider": "openai"},
        ]}
        with pytest.raises(ValidationError, match="multiple"):
            fd.plan_default(factory_doc=seed)


class TestPlanRevert:
    def _restore(self, key, prior):
        from zai_python_helper.ownership import RevertAction, RevertDecision

        return RevertDecision(
            action=RevertAction.RESTORE,
            key=key,
            prior_value=prior,
            prior_present=True,
            reason="restore",
        )

    def test_restore_does_not_mutate_input_and_emits_write(self):
        """RESTORE on pre-existing entries must (a) NOT mutate the input doc
        in place (planner input immutability, ADR-001) and (b) emit a real
        WRITE_JSON delta when the apiKey actually changes — not a false NOOP.

        Regression: ``apply_revert_decisions`` did a shallow list copy, so the
        entry dicts stayed shared with the input; setting ``apiKey`` mutated
        the input, and ``plan_revert`` then compared the (already-mutated)
        input against ``desired`` and emitted NOOP — the CLI reported ``use
        default`` applied while the on-disk Z.ai keys remained."""
        decisions = {
            fd.JOURNAL_KEY_ANTHROPIC_APIKEY: self._restore(
                fd.JOURNAL_KEY_ANTHROPIC_APIKEY, "PRIOR-ANTHROPIC"
            ),
            fd.JOURNAL_KEY_OPENAI_APIKEY: self._restore(
                fd.JOURNAL_KEY_OPENAI_APIKEY, "PRIOR-OPENAI"
            ),
        }
        # Entries currently hold HELPER keys; RESTORE must write PRIOR keys.
        doc = {
            "customModels": [
                {
                    "displayName": "Z.ai GLM Coding Plan (Anthropic)",
                    "provider": "anthropic",
                    "model": "glm-4.7",
                    "maxOutputTokens": 131072,
                    "baseUrl": GLOBAL_ANTHROPIC,
                    "apiKey": "HELPER-ANTHROPIC",
                },
                {
                    "displayName": "Z.ai GLM Coding Plan (OpenAI)",
                    "provider": "openai",
                    "model": "glm-4.7",
                    "maxOutputTokens": 131072,
                    "baseUrl": GLOBAL_PAAS,
                    "apiKey": "HELPER-OPENAI",
                },
            ]
        }
        import copy

        doc_before = copy.deepcopy(doc)
        plan = fd.plan_revert(decisions, factory_doc=doc)
        delta = plan.delta_for(FileTag.FACTORY_DROID)
        # (a) input immutability
        assert doc == doc_before, "plan_revert mutated its input doc in place"
        # (b) real write — the apiKeys genuinely changed
        assert delta.kind == DeltaKind.WRITE_JSON, "plan_revert emitted a false NOOP"
        restored = delta.content["customModels"]
        assert _entry_for("anthropic", restored)["apiKey"] == "PRIOR-ANTHROPIC"
        assert _entry_for("openai", restored)["apiKey"] == "PRIOR-OPENAI"

    def test_revert_refuses_duplicate_marker_protocol(self):
        """F3: apply_revert_decisions with duplicate GLM entries → raise.
        ``next(...)`` would otherwise remove the first, which could be a
        foreign GLM entry inserted before ours."""
        decisions = {
            fd.JOURNAL_KEY_ANTHROPIC_APIKEY: self._restore(
                fd.JOURNAL_KEY_ANTHROPIC_APIKEY, "PRIOR"
            ),
        }
        doc = {"customModels": [
            {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
             "apiKey": "first"},
            {"displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
             "apiKey": "second"},
        ]}
        with pytest.raises(ValidationError, match="multiple"):
            fd.plan_revert(decisions, factory_doc=doc)

    def test_revert_single_entry_still_restores(self):
        """Regression: the new dup guard must NOT break the normal
        single-entry restore path."""
        decisions = {
            fd.JOURNAL_KEY_ANTHROPIC_APIKEY: self._restore(
                fd.JOURNAL_KEY_ANTHROPIC_APIKEY, "PRIOR"
            ),
        }
        doc = {"customModels": [{
            "displayName": "Z.ai GLM Coding Plan (Anthropic)", "provider": "anthropic",
            "apiKey": "HELPER",
        }]}
        plan = fd.plan_revert(decisions, factory_doc=doc)
        delta = plan.delta_for(FileTag.FACTORY_DROID)
        assert delta.kind == DeltaKind.WRITE_JSON
        assert _entry_for("anthropic", delta.content["customModels"])["apiKey"] == "PRIOR"


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


class TestFailClosedGuards:
    """Cross-cutting entry-identity guard coverage (issue #53)."""

    def test_dup_guard_ignores_marker_with_unknown_provider(self):
        """A GLM-marker entry whose provider is NOT anthropic/openai is not a
        recognized protocol entry, so it is not counted by the dup guard
        (detection requires marker AND a recognized provider) — two such must
        NOT trip the duplicate refusal. The guard is exercised directly to
        decouple this from the (separate, out-of-scope) merge behavior for
        marker+unknown-provider entries."""
        models = [
            {"displayName": "GLM Coding Plan (X)", "provider": "azure"},
            {"displayName": "GLM Coding Plan (X)", "provider": "azure"},
        ]
        # No raise: azure is not a recognized protocol, so neither counts.
        fd._assert_no_duplicates(models, path="use zai")

    def test_drift_check_missing_managed_field_is_no_drift(self):
        """An entry that OMITS the managed fields (rather than setting them to
        a conflicting value) is not drift — activation just adds them. This is
        the deep-merge / foreign-sibling-keys scenario (Bug 3)."""
        seed = {"customModels": [{
            "displayName": "GLM Coding Plan (Anthropic)", "provider": "anthropic",
            "custom": "keep-me",  # no model/baseUrl/maxOutputTokens keys at all
        }]}
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        anth = _entry_for(
            fd.PROVIDER_ANTHROPIC,
            plan.delta_for(FileTag.FACTORY_DROID).content["customModels"],
        )
        assert anth["custom"] == "keep-me"  # foreign sibling key preserved
        assert anth["model"] == "glm-4.7"  # managed field added

    def test_known_china_baseurl_is_not_drift_under_global(self):
        """A china baseUrl under a GLOBAL activation is a known regional URL,
        but the allow-list applies ONLY to an entry the helper itself wrote
        (canonical displayName). Our own cross-region post-state must NOT trip
        the guard — the region rewrite is us overwriting us."""
        seed = {"customModels": [{
            "displayName": "Z.ai GLM Coding Plan (Anthropic)",  # canonical: ours
            "provider": "anthropic",
            "model": "glm-4.7", "baseUrl": CHINA_ANTHROPIC, "apiKey": "USER",
        }]}
        # No raise: helper-written entry + known regional URL → cross-region.
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        anth = _entry_for(
            fd.PROVIDER_ANTHROPIC,
            plan.delta_for(FileTag.FACTORY_DROID).content["customModels"],
        )
        assert anth["baseUrl"] == GLOBAL_ANTHROPIC  # rewritten to GLOBAL

    def test_user_written_entry_on_known_regional_url_raises(self):
        """F1 regression: the known-regional allow-list must NOT cover an entry
        the USER hand-wrote (non-canonical displayName). Such an entry pointed
        at a known regional endpoint is user config — activation would rewrite
        baseUrl and displayName, and only the apiKey is journaled, so the
        overwrite is irreversible. Must refuse."""
        seed = {"customModels": [{
            "displayName": "My GLM Coding Plan china",  # NOT our canonical name
            "provider": "anthropic",
            "model": "glm-4.7", "baseUrl": CHINA_ANTHROPIC, "apiKey": "USER_OWN_KEY",
        }]}
        with pytest.raises(ValidationError, match="baseUrl"):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_stale_helper_model_is_not_drift(self):
        """F4 regression: an entry the helper wrote at a PREVIOUS MODEL_ID must
        not be classified as user config. Otherwise the first bump of the
        constant turns routine activation into a hard refusal for every
        existing user."""
        seed = {"customModels": [{
            "displayName": "Z.ai GLM Coding Plan (Anthropic)",  # canonical: ours
            "provider": "anthropic",
            "model": "glm-4.6",  # a value the helper itself wrote earlier
            "maxOutputTokens": fd.MAX_OUTPUT_TOKENS,
            "baseUrl": GLOBAL_ANTHROPIC, "apiKey": "OLD",
        }]}
        # No raise: ours to rewrite; activation upgrades the model in place.
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        anth = _entry_for(
            fd.PROVIDER_ANTHROPIC,
            plan.delta_for(FileTag.FACTORY_DROID).content["customModels"],
        )
        assert anth["model"] == fd.MODEL_ID

    def test_user_written_entry_with_custom_model_still_raises(self):
        """The F4 relaxation must not reopen F2: a USER-written entry carrying
        its own model is still user config and must refuse."""
        seed = {"customModels": [{
            "displayName": "My GLM Coding Plan", "provider": "anthropic",
            "model": "user-custom-model", "baseUrl": GLOBAL_ANTHROPIC,
        }]}
        with pytest.raises(ValidationError, match="model"):
            fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)

    def test_explicit_null_managed_field_is_not_drift(self):
        """F5: an explicitly-null managed field means UNSET, not user config —
        it takes the same path as an absent key (activation just writes it)."""
        seed = {"customModels": [{
            "displayName": "My GLM Coding Plan", "provider": "anthropic",
            "model": None, "baseUrl": None, "maxOutputTokens": None,
        }]}
        # No raise: null == unset, so activation fills the fields in.
        plan = fd.plan_zai(Region.GLOBAL, factory_doc=seed, auth_token=TOKEN)
        anth = _entry_for(
            fd.PROVIDER_ANTHROPIC,
            plan.delta_for(FileTag.FACTORY_DROID).content["customModels"],
        )
        assert anth["model"] == fd.MODEL_ID
        assert anth["baseUrl"] == GLOBAL_ANTHROPIC
