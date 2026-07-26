"""zai_python_helper: manage Claude Code ⇄ Z.ai integration.

This package provides tools to configure and manage the integration between
Claude Code and Z.ai services without hand-editing configuration files.

It is designed to be **importable** (not just a CLI): the pure planning core
(``plan_zai`` / ``plan_default`` / ``postconditions``) and the domain types
(``ProviderSpec``, ``ModelMode``, ``Region``) can be imported into other Python
projects. See :data:`__all__` for the public, semantic-versioned surface.

This module re-exports the **already-working** public surface (issue #18
design). The high-level IO cycle (``use_zai`` / ``use_default``) is added in a
follow-up (S2.5); until then, callers compose the pure planner with the IO
backends themselves, e.g.::

    from zai_python_helper import (
        ProviderSpec, ModelMode, Region, plan_zai,
        JsonBackend, ShellBackend, Paths, base_url_for_region,
    )

    spec = ProviderSpec(base_url=base_url_for_region(Region.GLOBAL),
                        model_mode=ModelMode.ORIGINAL)
    paths = Paths.default()          # or Paths.from_home(tmp) in tests
    token = "<your Z.ai auth token>"  # resolve via io.secrets.resolve_key
    plan = plan_zai(spec, Region.GLOBAL,
                    settings_doc=JsonBackend.read(paths.claude_settings),
                    claude_json_doc=JsonBackend.read(paths.claude_json),
                    zshrc_text=ShellBackend.read(paths.zshrc),
                    auth_token=token)
    # ... apply plan.deltas via the backends ...

Everything not in :data:`__all__` is internal and may change without notice.
"""

# --- Version -----------------------------------------------------------------

__version__ = "0.1.0"

# --- Public surface ----------------------------------------------------------
# Imports are kept in ruff's isort order (module path). ``__all__`` below groups
# them semantically; that grouping is the authoritative taxonomy, not the
# import order here.

from zai_python_helper.backends import JsonBackend, ShellBackend
from zai_python_helper.core.domain import ModelMode, ProviderSpec
from zai_python_helper.core.planner import (
    DeltaKind,
    FileDelta,
    FileTag,
    PatchPlan,
    plan_default,
    plan_zai,
    postconditions,
)

# Canonical Z.ai base URL for a region. Pure lookup — needed by callers that
# build a ``ProviderSpec`` so they need not reach into planner internals.
from zai_python_helper.core.planner.claude_code import base_url_for_region
from zai_python_helper.errors import (
    ConfigurationError,
    ProviderError,
    ValidationError,
    ZaiPythonHelperError,
)

# NOTE (variant C, Tool name): the S6 Tool ABC (``tools.base.Tool``) owns the
# top-level ``Tool`` name (see ``__all__`` below). The S7 MCP module defines its
# OWN ``Tool`` enum (``mcp.Tool``) — a different concept (which coding tool an
# MCP is installed into). To keep the two from colliding on the public surface,
# the MCP ``Tool`` is NOT re-exported here: callers reach it as
# ``zai_python_helper.mcp.Tool``. Only the MCP preset/install surface is lifted.
from zai_python_helper.mcp import (
    PRESET_MCP_SERVICES,
    McpPreset,
    build_mcp_entry,
    install_into_doc,
    install_mcp,
    is_installed,
    list_installed,
    preset_by_id,
    preset_ids,
    tool_config_path,
    uninstall_from_doc,
    uninstall_mcp,
)
from zai_python_helper.ownership import (
    RevertAction,
    RevertDecision,
    revert,
    take_over,
)
from zai_python_helper.paths import Paths
from zai_python_helper.regions import (
    ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2,
    ZAI_PAAS_BASE_URL_BY_REGION,
    Region,
)
from zai_python_helper.status import (
    ClaudeCodeStatus,
    StatusReport,
    detect_status,
    render_status,
)
from zai_python_helper.tools import REGISTRY, get_tool, tool_names
from zai_python_helper.tools.base import ManagedField, StatusRow, Tool

__all__ = [
    "__version__",
    # domain
    "ModelMode",
    "ProviderSpec",
    "Region",
    # planning (pure)
    "DeltaKind",
    "FileDelta",
    "FileTag",
    "PatchPlan",
    "plan_default",
    "plan_zai",
    "postconditions",
    "base_url_for_region",
    # ownership journal (ADR-004, pure ops)
    "RevertAction",
    "RevertDecision",
    "take_over",
    "revert",
    # tools layer (S6): Tool protocol + registry (importable, per #18)
    "Tool",
    "ManagedField",
    "StatusRow",
    "REGISTRY",
    "get_tool",
    "tool_names",
    # region endpoints (paas + V2 anthropic for S6 tools)
    "ZAI_PAAS_BASE_URL_BY_REGION",
    "ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2",
    # infra
    "Paths",
    "JsonBackend",
    "ShellBackend",
    # status (read-only)
    "ClaudeCodeStatus",
    "StatusReport",
    "detect_status",
    "render_status",
    # preset MCP servers (S7, issue #8) — cross-tool install/uninstall.
    # NOTE: the MCP ``Tool`` enum is intentionally NOT here — it lives at
    # ``zai_python_helper.mcp.Tool`` (variant C), so it cannot shadow the S6
    # Tool ABC that owns the top-level ``Tool`` name above.
    "McpPreset",
    "PRESET_MCP_SERVICES",
    "preset_by_id",
    "preset_ids",
    "build_mcp_entry",
    "install_into_doc",
    "uninstall_from_doc",
    "list_installed",
    "is_installed",
    "tool_config_path",
    "install_mcp",
    "uninstall_mcp",
    # errors
    "ConfigurationError",
    "ProviderError",
    "ValidationError",
    "ZaiPythonHelperError",
]
