"""
Model planning functions for Claude Code configuration.

This module implements the logic for generating PatchPlans for each
of the 4 model modes (ORIGINAL, DEFAULT, SELECT, CUSTOM).
"""

from typing import Any, Dict

from zai_python_helper.constants import (
    ANTHROPIC_MODEL_ENV_VARS,
    ZAI_MODEL_PRESETS,
)
from zai_python_helper.core.domain import ModelMode, ProviderSpec


def plan_model_config(provider_spec: ProviderSpec) -> Dict[str, Any]:
    """
    Generate environment variable configuration for a given ProviderSpec.

    This is a PURE function — it takes a ProviderSpec and returns
    the env dict that should be merged into settings.json.

    Args:
        provider_spec: The provider specification

    Returns:
        Dict of environment variables to set in Claude Code settings
    """
    mode = provider_spec.model_mode

    if mode == ModelMode.ORIGINAL:
        return _plan_original_mode(provider_spec)
    elif mode == ModelMode.DEFAULT:
        return _plan_default_mode(provider_spec)
    elif mode == ModelMode.SELECT:
        return _plan_select_mode(provider_spec)
    elif mode == ModelMode.CUSTOM:
        return _plan_custom_mode(provider_spec)
    else:
        raise ValueError(f"Unknown ModelMode: {mode}")


def _plan_original_mode(provider_spec: ProviderSpec) -> Dict[str, Any]:
    """
    Plan for ORIGINAL mode — only ANTHROPIC_BASE_URL.

    This is the behavior of the original @z_ai/coding-helper utility.
    We only set the base URL and let the server decide which model to use.

    Args:
        provider_spec: The provider specification

    Returns:
        Dict with ANTHROPIC_BASE_URL and API key
    """
    return {
        "ANTHROPIC_BASE_URL": provider_spec.base_url,
        "API_TIMEOUT_MS": "3000000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _plan_default_mode(provider_spec: ProviderSpec) -> Dict[str, Any]:
    """
    Plan for DEFAULT mode — preset models via ANTHROPIC_DEFAULT_*_MODEL.

    This mode sets environment variables to override Anthropic's default
    models with Z.ai equivalents. We map each alias (opus, sonnet, etc.)
    to the best available Z.ai model.

    Args:
        provider_spec: The provider specification

    Returns:
        Dict with ANTHROPIC_BASE_URL and ANTHROPIC_DEFAULT_*_MODEL vars
    """
    env = _plan_original_mode(provider_spec)

    # Map each Anthropic alias to its Z.ai equivalent
    # We use the latest/best model for each tier
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "zai/glm-4-plus"
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "zai/glm-4.7"
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "zai/glm-4-flash"
    env["ANTHROPIC_DEFAULT_FABLE_MODEL"] = "zai/glm-4-plus"

    return env


def _plan_select_mode(provider_spec: ProviderSpec) -> Dict[str, Any]:
    """
    Plan for SELECT mode — user-selected model.

    The user has selected a specific model from our presets. We:
    1. Set the ANTHROPIC_DEFAULT_*_MODEL for the selected model's tier
    2. Optionally set modelOverrides for explicit mapping

    Args:
        provider_spec: The provider specification with selected_model set

    Returns:
        Dict with ANTHROPIC_BASE_URL and model-specific config
    """
    if not provider_spec.selected_model:
        raise ValueError("SELECT mode requires selected_model")

    preset = ZAI_MODEL_PRESETS.get(provider_spec.selected_model)
    if not preset:
        raise ValueError(f"Unknown preset: {provider_spec.selected_model}")

    env = _plan_original_mode(provider_spec)

    # Set the default model for the selected tier
    alias = preset["anthropic_alias"]
    env_var = ANTHROPIC_MODEL_ENV_VARS.get(alias)
    if env_var:
        env[env_var] = preset["model_id"]

    return env


def _plan_custom_mode(provider_spec: ProviderSpec) -> Dict[str, Any]:
    """
    Plan for CUSTOM mode — user-provided model ID.

    The user has provided a custom model ID. We use ANTHROPIC_CUSTOM_MODEL_OPTION
    to make it available in Claude Code's model selector.

    Args:
        provider_spec: The provider specification with custom_model_* fields

    Returns:
        Dict with ANTHROPIC_BASE_URL and custom model configuration
    """
    if not provider_spec.custom_model_id:
        raise ValueError("CUSTOM mode requires custom_model_id")

    env = _plan_original_mode(provider_spec)

    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = provider_spec.custom_model_id

    if provider_spec.custom_model_name:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = provider_spec.custom_model_name

    if provider_spec.custom_model_description:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] = (
            provider_spec.custom_model_description
        )

    if provider_spec.custom_capabilities:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES"] = (
            provider_spec.custom_capabilities
        )

    return env


def generate_model_overrides(provider_spec: ProviderSpec) -> Dict[str, str]:
    """
    Generate modelOverrides dict for settings.json.

    This provides explicit mapping from Anthropic model IDs to
    provider-specific model IDs. Used in SELECT mode primarily.

    Args:
        provider_spec: The provider specification

    Returns:
        Dict mapping Anthropic model IDs to provider model IDs
    """
    if provider_spec.model_mode == ModelMode.ORIGINAL:
        return {}

    if provider_spec.model_mode == ModelMode.DEFAULT:
        # Map all Anthropic models to Z.ai equivalents
        return {
            "claude-opus-4-8": "zai/glm-4-plus",
            "claude-sonnet-5": "zai/glm-4.7",
            "claude-haiku-4-5": "zai/glm-4-flash",
            "claude-fable-5": "zai/glm-4-plus",
        }

    if provider_spec.model_mode == ModelMode.SELECT:
        if not provider_spec.selected_model:
            return {}

        preset = ZAI_MODEL_PRESETS.get(provider_spec.selected_model)
        if not preset:
            # Consistent with plan_model_config: raise ValueError for unknown preset
            raise ValueError(f"Unknown preset: {provider_spec.selected_model}")

        # Map based on which tier the selected model belongs to
        alias = preset["anthropic_alias"]
        if alias == "opus":
            return {"claude-opus-4-8": preset["model_id"]}
        elif alias == "sonnet":
            return {"claude-sonnet-5": preset["model_id"]}
        elif alias == "haiku":
            return {"claude-haiku-4-5": preset["model_id"]}
        elif alias == "fable":
            return {"claude-fable-5": preset["model_id"]}

    if provider_spec.model_mode == ModelMode.CUSTOM:
        # For custom mode, we don't generate modelOverrides
        # The custom model appears as a separate option
        return {}

    return {}
