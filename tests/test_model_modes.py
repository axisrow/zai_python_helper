"""
Tests for model selection modes.

Tests the 4 modes (ORIGINAL, DEFAULT, SELECT, CUSTOM) and their
configuration generation.
"""

import pytest

from zai_python_helper.constants import ZAI_MODEL_PRESETS, get_preset_model
from zai_python_helper.core.domain import ModelMode, ProviderSpec
from zai_python_helper.core.planner.models import (
    generate_model_overrides,
    plan_model_config,
)


class TestProviderSpec:
    """Tests for ProviderSpec dataclass."""

    def test_original_mode_valid(self):
        """ORIGINAL mode is always valid."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.ORIGINAL,
        )
        assert spec.validate()

    def test_select_mode_requires_model(self):
        """SELECT mode requires selected_model."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.SELECT,
        )
        assert not spec.validate()

        spec.selected_model = "glm-4-plus"
        assert spec.validate()

    def test_custom_mode_requires_model_id(self):
        """CUSTOM mode requires custom_model_id."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.CUSTOM,
        )
        assert not spec.validate()

        spec.custom_model_id = "my-model"
        assert spec.validate()


class TestOriginalMode:
    """Tests for ORIGINAL mode planning."""

    def test_original_mode_only_base_url(self):
        """ORIGINAL mode only sets ANTHROPIC_BASE_URL."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.ORIGINAL,
        )

        env = plan_model_config(spec)

        assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert env["API_TIMEOUT_MS"] == "3000000"
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
        assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in env

    def test_original_mode_no_overrides(self):
        """ORIGINAL mode generates no modelOverrides."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.ORIGINAL,
        )

        overrides = generate_model_overrides(spec)
        assert overrides == {}


class TestDefaultMode:
    """Tests for DEFAULT mode planning."""

    def test_default_mode_sets_presets(self):
        """DEFAULT mode sets ANTHROPIC_DEFAULT_*_MODEL vars."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.DEFAULT,
        )

        env = plan_model_config(spec)

        assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "zai/glm-4-plus"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "zai/glm-4.7"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "zai/glm-4-flash"
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "zai/glm-4-plus"

    def test_default_mode_generates_overrides(self):
        """DEFAULT mode generates modelOverrides."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.DEFAULT,
        )

        overrides = generate_model_overrides(spec)

        assert overrides["claude-opus-4-8"] == "zai/glm-4-plus"
        assert overrides["claude-sonnet-5"] == "zai/glm-4.7"
        assert overrides["claude-haiku-4-5"] == "zai/glm-4-flash"


class TestSelectMode:
    """Tests for SELECT mode planning."""

    def test_select_mode_opus_preset(self):
        """SELECT mode with opus preset sets correct env."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.SELECT,
            selected_model="glm-4-plus",
        )

        env = plan_model_config(spec)

        assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "zai/glm-4-plus"

    def test_select_mode_sonnet_preset(self):
        """SELECT mode with sonnet preset sets correct env."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.SELECT,
            selected_model="glm-4.7",
        )

        env = plan_model_config(spec)

        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "zai/glm-4.7"

    def test_select_mode_invalid_preset_raises(self):
        """SELECT mode with invalid preset raises ValueError."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.SELECT,
            selected_model="nonexistent-model",
        )

        with pytest.raises(ValueError, match="Unknown preset"):
            plan_model_config(spec)

    def test_select_mode_generates_single_override(self):
        """SELECT mode generates override only for selected tier."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.SELECT,
            selected_model="glm-4-plus",
        )

        overrides = generate_model_overrides(spec)

        assert overrides == {"claude-opus-4-8": "zai/glm-4-plus"}


class TestCustomMode:
    """Tests for CUSTOM mode planning."""

    def test_custom_mode_sets_custom_option(self):
        """CUSTOM mode sets ANTHROPIC_CUSTOM_MODEL_OPTION."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.CUSTOM,
            custom_model_id="my-custom-model",
            custom_model_name="My Custom Model",
            custom_model_description="A custom model",
        )

        env = plan_model_config(spec)

        assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "my-custom-model"
        assert env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "My Custom Model"
        assert (
            env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] == "A custom model"
        )

    def test_custom_mode_with_capabilities(self):
        """CUSTOM mode can set capabilities."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.CUSTOM,
            custom_model_id="my-model",
            custom_capabilities="effort,thinking",
        )

        env = plan_model_config(spec)

        assert env["ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES"] == (
            "effort,thinking"
        )

    def test_custom_mode_no_overrides(self):
        """CUSTOM mode generates no modelOverrides."""
        spec = ProviderSpec(
            base_url="https://api.z.ai/api/anthropic",
            model_mode=ModelMode.CUSTOM,
            custom_model_id="my-model",
        )

        overrides = generate_model_overrides(spec)
        assert overrides == {}


class TestModelPresets:
    """Tests for model preset constants."""

    def test_presets_have_required_fields(self):
        """All presets have required fields."""
        for preset_name, config in ZAI_MODEL_PRESETS.items():
            assert "model_id" in config
            assert "anthropic_alias" in config
            assert "name" in config
            assert "description" in config

    def test_get_preset_model(self):
        """get_preset_model returns correct config."""
        config = get_preset_model("glm-4-plus")
        assert config is not None
        assert config["model_id"] == "zai/glm-4-plus"
        assert config["anthropic_alias"] == "opus"

    def test_get_preset_model_unknown_returns_none(self):
        """get_preset_model returns None for unknown preset."""
        config = get_preset_model("unknown-model")
        assert config is None
