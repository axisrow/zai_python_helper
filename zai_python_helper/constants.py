"""
Constants for zai_python_helper.

This module contains constant values used throughout the application,
including Z.ai model presets and configuration defaults.
"""

# Z.ai API endpoints
ZAI_ANTHROPIC_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_OPENAI_BASE_URL = "https://api.z.ai/api/openai"

# Z.ai model presets
# Each preset defines a model available through Z.ai and how it maps
# to Anthropic's model aliases (opus, sonnet, haiku, fable)
ZAI_MODEL_PRESETS = {
    "glm-4-plus": {
        "model_id": "zai/glm-4-plus",
        "anthropic_alias": "opus",  # Maps opus → glm-4-plus
        "name": "GLM-4 Plus",
        "description": "Latest flagship GLM model via Z.ai (Claude Opus class)",
    },
    "glm-4.7": {
        "model_id": "zai/glm-4.7",
        "anthropic_alias": "sonnet",  # Maps sonnet → glm-4.7
        "name": "GLM-4.7",
        "description": "Previous generation GLM flagship (Claude Sonnet class)",
    },
    "glm-4-flash": {
        "model_id": "zai/glm-4-flash",
        "anthropic_alias": "haiku",  # Maps haiku → glm-4-flash
        "name": "GLM-4 Flash",
        "description": "Fast, efficient GLM model (Claude Haiku class)",
    },
    "glm-4-plus-1m": {
        "model_id": "zai/glm-4-plus-1m",
        "anthropic_alias": "opus",  # Maps opus → glm-4-plus-1m
        "name": "GLM-4 Plus (1M Context)",
        "description": "GLM-4 Plus with 1M token context window",
    },
    "glm-4.7-1m": {
        "model_id": "zai/glm-4.7-1m",
        "anthropic_alias": "sonnet",  # Maps sonnet → glm-4.7-1m
        "name": "GLM-4.7 (1M Context)",
        "description": "GLM-4.7 with 1M token context window",
    },
}

# Anthropic default model environment variable names
# These are used in mode 2 (DEFAULT) to override model aliases
ANTHROPIC_MODEL_ENV_VARS = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}

# Default timeout for API requests (ms)
DEFAULT_API_TIMEOUT_MS = "3000000"

# MCP servers provided by Z.ai
ZAI_MCP_SERVERS = {
    "zai-mcp-server": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@zai/mcp-server"],
        "description": "Z.ai Vision MCP server",
    },
    "web-search-prime": {
        "type": "streamable-http",
        "url": "https://api.z.ai/mcp/web-search",
        "description": "Z.ai Web Search MCP",
    },
    "web-reader": {
        "type": "streamable-http",
        "url": "https://api.z.ai/mcp/web-reader",
        "description": "Z.ai Web URL Reader MCP",
    },
    "zread": {
        "type": "streamable-http",
        "url": "https://api.z.ai/mcp/github-reader",
        "description": "Z.ai GitHub Reader MCP",
    },
}


def get_preset_model(preset_name: str) -> dict | None:
    """
    Get a preset model configuration by name.

    Args:
        preset_name: The name of the preset (e.g., "glm-4-plus")

    Returns:
        The preset configuration dict, or None if not found
    """
    return ZAI_MODEL_PRESETS.get(preset_name)


def list_available_presets() -> list[str]:
    """
    Get a list of all available preset names.

    Returns:
        List of preset names (sorted)
    """
    return sorted(ZAI_MODEL_PRESETS.keys())
