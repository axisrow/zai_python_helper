"""
Shared domain types (per ADR-001).

These types are IO-free and shared between core/planner and core/router.
They represent the pure data structures that drive the configuration logic.
"""

from dataclasses import dataclass
from enum import Enum


class ModelMode(Enum):
    """
    Mode for model selection in Claude Code configuration.

    Each mode represents a different strategy for specifying which model
    Claude Code should use when talking to a provider.

    Modes (by priority):
    - ORIGINAL: Only ANTHROPIC_BASE_URL, let server decide (like @z_ai/coding-helper)
    - DEFAULT: Use preset ANTHROPIC_DEFAULT_*_MODEL variables
    - SELECT: User selects from predefined list of models
    - CUSTOM: User provides custom model ID manually
    """
    ORIGINAL = "original"
    DEFAULT = "default"
    SELECT = "select"
    CUSTOM = "custom"


@dataclass
class ProviderSpec:
    """
    Specification for a provider configuration.

    This is a shared domain type that captures all information needed
    to generate configuration patches for any tool (Claude Code, OpenCode, etc.).

    Per ADR-001, this is IO-free — it's just data. The planner layer
    transforms this into PatchPlans, and the IO layer applies them.

    Attributes:
        base_url: The API base URL for the provider
        model_mode: Which mode to use for model selection
        api_key: Optional API key (if not using env var)

        For SELECT mode:
        selected_model: Which preset model the user selected

        For CUSTOM mode:
        custom_model_id: The custom model ID to use
        custom_model_name: Display name for the custom model
        custom_model_description: Description of the custom model
    """
    base_url: str
    model_mode: ModelMode

    # Optional API key (if not using environment variable)
    api_key: str | None = None

    # For SELECT mode
    selected_model: str | None = None

    # For CUSTOM mode
    custom_model_id: str | None = None
    custom_model_name: str | None = None
    custom_model_description: str | None = None
    custom_capabilities: str | None = None  # e.g. "effort,thinking"

    def validate(self) -> bool:
        """
        Validate that the ProviderSpec is consistent for its mode.

        Returns True if valid, False otherwise.
        """
        if self.model_mode == ModelMode.SELECT and not self.selected_model:
            return False

        if self.model_mode == ModelMode.CUSTOM and not self.custom_model_id:
            return False

        return True
