"""Pure tests for the Claude Code planner (ADR-001: no IO).

Seeded parsed documents → :class:`PatchPlan` assertions. These tests never
touch the filesystem, never read the environment, and never prompt — they
exercise the pure transforms in
:mod:`zai_python_helper.core.planner.claude_code`.
"""

from __future__ import annotations

import pytest

from zai_python_helper.core.domain import ModelMode, ProviderSpec
from zai_python_helper.core.planner import DeltaKind, FileTag, plan_default, plan_zai
from zai_python_helper.core.planner.claude_code import (
    MANAGED_ZAI_KEYS,
    base_url_for_region,
    postconditions,
)
from zai_python_helper.regions import Region

TOKEN = "sk-test-token-abc"
GLOBAL_URL = "https://api.z.ai/api/anthropic"
CHINA_URL = "https://api.zai.cn/api/anthropic"


def _spec(mode: ModelMode = ModelMode.DEFAULT, **kw) -> ProviderSpec:
    base = {"base_url": GLOBAL_URL, "model_mode": mode}
    base.update(kw)
    return ProviderSpec(**base)


# ---------------------------------------------------------------------------
# plan_zai — env block
# ---------------------------------------------------------------------------


class TestPlanZaiSettingsEnv:
    """plan_zai builds the exact managed env block for settings.json."""

    def test_default_mode_sets_exact_env_block(self):
        """DEFAULT mode splices the full managed env over a seeded doc."""
        spec = _spec(ModelMode.DEFAULT)
        settings = {"env": {"SOME_FOREIGN_KEY": "keep"}, "topLevel": 1}

        plan = plan_zai(
            spec,
            Region.GLOBAL,
            settings_doc=settings,
            claude_json_doc={"theme": "dark"},
            zshrc_text="",
            auth_token=TOKEN,
        )

        delta = plan.delta_for(FileTag.SETTINGS)
        assert delta is not None
        assert delta.kind == DeltaKind.WRITE_JSON
        env = delta.content["env"]

        # The four always-managed keys, exact values.
        assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
        assert env["ANTHROPIC_BASE_URL"] == GLOBAL_URL
        assert env["API_TIMEOUT_MS"] == "3000000"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        # DEFAULT mode contributes the four preset vars.
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "zai/glm-4-plus"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "zai/glm-4.7"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "zai/glm-4-flash"
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "zai/glm-4-plus"
        # ANTHROPIC_API_KEY removed.
        assert "ANTHROPIC_API_KEY" not in env
        # Foreign key preserved (deep-merge).
        assert env["SOME_FOREIGN_KEY"] == "keep"
        # Foreign top-level key preserved.
        assert delta.content["topLevel"] == 1

    def test_original_mode_minimal_env(self):
        """ORIGINAL mode sets only base url + auth + timeout, no presets."""
        spec = _spec(ModelMode.ORIGINAL)
        plan = plan_zai(spec, Region.GLOBAL, auth_token=TOKEN)
        env = plan.delta_for(FileTag.SETTINGS).content["env"]

        assert env["ANTHROPIC_BASE_URL"] == GLOBAL_URL
        assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
        assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in env

    def test_select_mode_sets_only_selected_tier(self):
        """SELECT mode sets only the preset's tier env var."""
        spec = _spec(ModelMode.SELECT, selected_model="glm-4-plus")
        plan = plan_zai(spec, Region.GLOBAL, auth_token=TOKEN)
        env = plan.delta_for(FileTag.SETTINGS).content["env"]

        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "zai/glm-4-plus"
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env

    def test_custom_mode_sets_custom_option(self):
        """CUSTOM mode sets the custom model option vars."""
        spec = _spec(
            ModelMode.CUSTOM,
            custom_model_id="my-x",
            custom_model_name="My Model",
        )
        plan = plan_zai(spec, Region.GLOBAL, auth_token=TOKEN)
        env = plan.delta_for(FileTag.SETTINGS).content["env"]

        assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "my-x"
        assert env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "My Model"

    def test_china_region_uses_china_base_url(self):
        """Region.CHINA maps to the China Z.ai endpoint."""
        spec = _spec(ModelMode.ORIGINAL)
        plan = plan_zai(spec, Region.CHINA, auth_token=TOKEN)
        env = plan.delta_for(FileTag.SETTINGS).content["env"]
        assert env["ANTHROPIC_BASE_URL"] == CHINA_URL

    def test_base_url_for_region_lookup(self):
        assert base_url_for_region(Region.GLOBAL) == GLOBAL_URL
        assert base_url_for_region(Region.CHINA) == CHINA_URL

    def test_input_doc_not_mutated(self):
        """The planner must not mutate its input document (pure transform)."""
        spec = _spec(ModelMode.DEFAULT)
        settings = {"env": {"ANTHROPIC_API_KEY": "sk-old"}}
        import copy

        snapshot = copy.deepcopy(settings)
        plan_zai(spec, Region.GLOBAL, settings_doc=settings, auth_token=TOKEN)
        assert settings == snapshot


# ---------------------------------------------------------------------------
# plan_zai — .claude.json + .zshrc
# ---------------------------------------------------------------------------


class TestPlanZaiOtherFiles:
    def test_claude_json_sets_onboarding_when_absent(self):
        spec = _spec(ModelMode.ORIGINAL)
        plan = plan_zai(
            spec,
            Region.GLOBAL,
            claude_json_doc={"theme": "dark"},
            auth_token=TOKEN,
        )
        delta = plan.delta_for(FileTag.CLAUDE_JSON)
        assert delta.kind == DeltaKind.WRITE_JSON
        assert delta.content["hasCompletedOnboarding"] is True
        assert delta.content["theme"] == "dark"  # foreign key preserved

    def test_claude_json_noop_when_onboarding_already_true(self):
        spec = _spec(ModelMode.ORIGINAL)
        plan = plan_zai(
            spec,
            Region.GLOBAL,
            claude_json_doc={"hasCompletedOnboarding": True, "theme": "dark"},
            auth_token=TOKEN,
        )
        delta = plan.delta_for(FileTag.CLAUDE_JSON)
        assert delta.kind == DeltaKind.NOOP

    def test_zshrc_block_installed_when_absent(self):
        spec = _spec(ModelMode.ORIGINAL)
        plan = plan_zai(
            spec, Region.GLOBAL, zshrc_text="export PATH=/bin\n", auth_token=TOKEN
        )
        delta = plan.delta_for(FileTag.ZSHRC)
        assert delta.kind == DeltaKind.WRITE_TEXT
        assert "zai-python-helper managed" in delta.content
        # Foreign line preserved.
        assert "export PATH=/bin" in delta.content

    def test_zshrc_noop_when_block_present(self):
        spec = _spec(ModelMode.ORIGINAL)
        text = (
            "export PATH=/bin\n\n"
            "# >>> zai-python-helper managed >>>\n"
            "# body\n"
            "# <<< zai-python-helper managed <<<\n"
        )
        plan = plan_zai(spec, Region.GLOBAL, zshrc_text=text, auth_token=TOKEN)
        assert plan.delta_for(FileTag.ZSHRC).kind == DeltaKind.NOOP


# ---------------------------------------------------------------------------
# Idempotency — plan on post-state = all NOOP
# ---------------------------------------------------------------------------


class TestPlanZaiIdempotency:
    def test_second_plan_on_post_state_is_all_noop(self):
        """Feeding plan_zai's own output back in yields an all-NOOP plan."""
        spec = _spec(ModelMode.DEFAULT)
        plan1 = plan_zai(spec, Region.GLOBAL, auth_token=TOKEN)

        post_settings = plan1.delta_for(FileTag.SETTINGS).content
        post_claude_json = plan1.delta_for(FileTag.CLAUDE_JSON).content
        post_zshrc = plan1.delta_for(FileTag.ZSHRC).content

        plan2 = plan_zai(
            spec,
            Region.GLOBAL,
            settings_doc=post_settings,
            claude_json_doc=post_claude_json,
            zshrc_text=post_zshrc,
            auth_token=TOKEN,
        )
        assert plan2.is_empty
        assert all(d.kind == DeltaKind.NOOP for d in plan2.deltas)


# ---------------------------------------------------------------------------
# plan_default — exact inverse
# ---------------------------------------------------------------------------


class TestPlanDefault:
    def test_default_removes_all_managed_keys_keeps_foreign(self):
        spec = _spec(ModelMode.DEFAULT)
        settings = {
            "env": {
                "SOME_FOREIGN_KEY": "keep",
                "ANTHROPIC_AUTH_TOKEN": TOKEN,
                "ANTHROPIC_BASE_URL": GLOBAL_URL,
                "API_TIMEOUT_MS": "3000000",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "zai/glm-4-plus",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "zai/glm-4.7",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "zai/glm-4-flash",
                "ANTHROPIC_DEFAULT_FABLE_MODEL": "zai/glm-4-plus",
            },
            "topLevel": 1,
        }
        plan = plan_default(spec, settings_doc=settings)

        delta = plan.delta_for(FileTag.SETTINGS)
        assert delta.kind == DeltaKind.WRITE_JSON
        env = delta.content["env"]
        for key in MANAGED_ZAI_KEYS:
            assert key not in env
        for key in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
        ):
            assert key not in env
        assert env == {"SOME_FOREIGN_KEY": "keep"}
        assert delta.content["topLevel"] == 1

    def test_default_mode_agnostic_strips_all_model_keys(self):
        """Regression: revert must strip the UNION of all-mode keys regardless
        of the revert invocation's mode. ``use zai --mode default`` sets the
        four DEFAULT tier vars; a bare ``use default`` (ORIGINAL mode) must
        still remove them — revert is mode-agnostic.
        """
        # Revert carries ORIGINAL mode (contributes no model keys of its own).
        revert_spec = _spec(ModelMode.ORIGINAL)
        settings = {
            "env": {
                "FOREIGN": "keep",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "zai/glm-4-plus",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "zai/glm-4.7",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "zai/glm-4-flash",
                "ANTHROPIC_DEFAULT_FABLE_MODEL": "zai/glm-4-plus",
                "ANTHROPIC_CUSTOM_MODEL_OPTION": "some-custom",
            }
        }
        plan = plan_default(revert_spec, settings_doc=settings)
        env = plan.delta_for(FileTag.SETTINGS).content["env"]
        # Every model-mode key any activation could set is stripped.
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in env
        assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in env
        assert env == {"FOREIGN": "keep"}

    def test_default_drops_env_when_empty(self):
        """If env has only managed keys, default removes the env key entirely."""
        spec = _spec(ModelMode.DEFAULT)
        settings = {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": TOKEN,
                "ANTHROPIC_BASE_URL": GLOBAL_URL,
                "API_TIMEOUT_MS": "3000000",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        }
        plan = plan_default(spec, settings_doc=settings)
        delta = plan.delta_for(FileTag.SETTINGS)
        assert delta.kind == DeltaKind.WRITE_JSON
        assert "env" not in delta.content

    def test_default_does_not_touch_claude_json(self):
        """plan_default must not emit a delta for .claude.json."""
        spec = _spec(ModelMode.DEFAULT)
        plan = plan_default(spec, settings_doc={"env": {}})
        assert plan.delta_for(FileTag.CLAUDE_JSON) is None

    def test_default_removes_zshrc_block_keeps_foreign(self):
        spec = _spec(ModelMode.DEFAULT)
        text = (
            "export PATH=/bin\n\n"
            "# >>> zai-python-helper managed >>>\n"
            "# body\n"
            "# <<< zai-python-helper managed <<<\n"
        )
        plan = plan_default(spec, zshrc_text=text)
        delta = plan.delta_for(FileTag.ZSHRC)
        assert delta.kind == DeltaKind.WRITE_TEXT
        assert "zai-python-helper managed" not in delta.content
        assert "export PATH=/bin" in delta.content

    def test_default_noop_on_already_default(self):
        spec = _spec(ModelMode.DEFAULT)
        plan = plan_default(spec, settings_doc={"env": {"FOREIGN": "x"}}, zshrc_text="")
        assert plan.is_empty

    def test_default_is_inverse_of_zai_round_trip(self):
        """use zai then use default restores the original foreign env."""
        spec = _spec(ModelMode.DEFAULT)
        original = {"env": {"FOREIGN": "keep", "ANTHROPIC_API_KEY": "sk-old"}}

        zai = plan_zai(spec, Region.GLOBAL, settings_doc=original, auth_token=TOKEN)
        after_zai = zai.delta_for(FileTag.SETTINGS).content

        default = plan_default(spec, settings_doc=after_zai)
        after_default = default.delta_for(FileTag.SETTINGS).content

        # Foreign key survives the round-trip; managed keys gone; API_KEY
        # stays removed (we never owned its value — see REMOVED_ON_ZAI_KEYS).
        assert after_default["env"] == {"FOREIGN": "keep"}


# ---------------------------------------------------------------------------
# postconditions
# ---------------------------------------------------------------------------


class TestPostconditions:
    def test_active_state_passes(self):
        spec = _spec(ModelMode.DEFAULT)
        plan = plan_zai(spec, Region.GLOBAL, auth_token=TOKEN)
        settings = plan.delta_for(FileTag.SETTINGS).content
        zshrc = plan.delta_for(FileTag.ZSHRC).content
        assert postconditions(
            Region.GLOBAL, settings_doc=settings, zshrc_text=zshrc
        )

    def test_missing_token_fails(self):
        assert not postconditions(
            Region.GLOBAL,
            settings_doc={"env": {"ANTHROPIC_BASE_URL": GLOBAL_URL}},
            zshrc_text="",
        )

    def test_wrong_region_url_fails(self):
        assert not postconditions(
            Region.CHINA,
            settings_doc={
                "env": {"ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_BASE_URL": GLOBAL_URL}
            },
            zshrc_text="",
        )

    def test_api_key_present_fails(self):
        assert not postconditions(
            Region.GLOBAL,
            settings_doc={
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "x",
                    "ANTHROPIC_BASE_URL": GLOBAL_URL,
                    "ANTHROPIC_API_KEY": "leak",
                }
            },
            zshrc_text="",
        )

    def test_missing_block_fails(self):
        assert not postconditions(
            Region.GLOBAL,
            settings_doc={
                "env": {"ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_BASE_URL": GLOBAL_URL}
            },
            zshrc_text="",
        )


@pytest.mark.parametrize("mode", list(ModelMode))
def test_plan_zai_emits_three_deltas_for_every_mode(mode):
    """Every mode produces a settings + claude_json + zshrc delta (sanity)."""
    spec = _spec(mode, selected_model="glm-4-plus", custom_model_id="x")
    plan = plan_zai(spec, Region.GLOBAL, auth_token=TOKEN)
    tags = {d.tag for d in plan.deltas}
    assert tags == {FileTag.SETTINGS, FileTag.CLAUDE_JSON, FileTag.ZSHRC}
