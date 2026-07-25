"""Tool registry — the CLI's dispatch table (S6 foundation, issue #7).

``REGISTRY`` maps a tool name (the ``--tool`` value) to its :class:`Tool`.
The CLI looks up ``REGISTRY[args.tool]`` and drives ``use zai`` / ``use
default`` generically through the Tool protocol — no per-tool branches.

Claude Code is registered here (the v0.1 default). The S6 tools (OpenCode,
Crush, Factory Droid) register themselves in their own PRs by appending to
``REGISTRY``.
"""

from __future__ import annotations

from zai_python_helper.tools.base import Tool
from zai_python_helper.tools.claude_code import ClaudeCodeTool
from zai_python_helper.tools.crush import CrushTool
from zai_python_helper.tools.opencode import OpenCodeTool

#: ``{tool_name: Tool}``. The CLI dispatches on this. Add a tool by
#: registering an instance here (its ``name`` MUST match the key).
REGISTRY: dict[str, Tool] = {
    ClaudeCodeTool.name: ClaudeCodeTool(),
    CrushTool.name: CrushTool(),
    OpenCodeTool.name: OpenCodeTool(),
}


def get_tool(name: str) -> Tool:
    """Return the registered :class:`Tool` for ``name``.

    Raises :class:`KeyError` (surfaced by the caller as a CLI error) when the
    name is unknown — the argparse ``choices`` list is derived from the keys so
    this is unreachable in normal CLI use, but importable callers benefit from
    a clear error.
    """
    try:
        return REGISTRY[name]
    except KeyError as e:  # pragma: no cover - argparse choices guard this
        raise KeyError(f"Unknown tool: {name!r}. Known: {sorted(REGISTRY)}") from e


def tool_names() -> list[str]:
    """The sorted list of registered tool names (for ``--tool`` choices)."""
    return sorted(REGISTRY)


__all__ = ["REGISTRY", "Tool", "get_tool", "tool_names"]
