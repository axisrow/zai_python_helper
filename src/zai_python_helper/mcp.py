"""Preset MCP servers + cross-tool install/uninstall (S7, issue #8).

The Z.ai GLM Coding Plan ships **four preset MCP servers** that augment a coding
assistant with vision, web search, web reading, and GitHub deep-read. This
module is the Python equivalent of the upstream ``@z_ai/coding-helper``
``MCPManager`` + ``PRESET_MCP_SERVICES`` (parity required on the *preset
definitions* and the *per-tool MCP-entry shape* — black-box behavior, not code).

Two concerns are kept strictly separate, mirroring the rest of the codebase:

- **Pure** — :data:`PRESET_MCP_SERVICES` (the preset table),
  :func:`build_mcp_entry` (one tool-specific MCP entry from a preset), and the
  deep-merge transforms :func:`install_into_doc` / :func:`uninstall_from_doc`.
  These take/return plain dicts and never touch the filesystem, so they are
  unit-testable in isolation and importable as a library (issue #18).
- **IO** — :func:`read_config` / :func:`write_config` resolve the tool's config
  file from a home dir and (de)serialize it atomically via
  :class:`~zai_python_helper.backends.JsonBackend`. The high-level
  :func:`install_mcp` / :func:`uninstall_mcp` compose the pure transforms with
  these IO helpers, so callers that already hold a parsed document can use the
  pure layer and callers that want the one-shot cycle use the high-level pair.

Per-tool MCP-entry shapes (parity with the upstream per-tool managers):

================  ==============================================  ============  ===========
Tool              config file (under ``home``)                   section key   http ``type``
================  ==============================================  ============  ===========
``claude-code``   ``.claude.json``                                ``mcpServers``  ``http``
``opencode``      ``.config/opencode/opencode.json``             ``mcp``         ``remote``
``crush``         ``.config/crush/crush.json``                   ``mcp``         ``http``
``factory-droid`` ``.factory/mcp.json``                           ``mcpServers``  ``http``
================  ==============================================  ============  ===========

stdio entries always carry ``type: stdio`` except OpenCode (``type: local`` +
``command`` as a single array + ``environment`` instead of ``env``). Factory
Droid adds ``disabled: false``. The auth header key is ``Authorization``
(capital A, matching the upstream) under ``headers`` for the http variants and
``Z_AI_API_KEY`` under ``env``/``environment`` for the stdio variant.

Region handling: the four presets are region-aware. GLOBAL uses the
``api.z.ai`` host; CHINA uses ``open.bigmodel.cn``. The stdio
``zai-mcp-server`` carries ``Z_AI_MODE`` (``ZAI`` global / ``ZHIPU`` china) in
its env template.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from zai_python_helper.regions import Region

__all__ = [
    "PRESET_MCP_SERVICES",
    "McpPreset",
    "Tool",
    "tool_config_path",
    "build_mcp_entry",
    "install_into_doc",
    "uninstall_from_doc",
    "list_installed",
    "is_installed",
    "read_config",
    "write_config",
    "install_mcp",
    "uninstall_mcp",
    "preset_by_id",
    "preset_ids",
]


# --------------------------------------------------------------------------- #
# Tool enum — the cross-tool install targets (S7). S6 (multitool) is not yet
# merged, so this mapping lives HERE rather than in a shared ``tools/base`` to
# avoid colliding with the parallel S6 worker on ``cli.py``. When S6 lands its
# own tool registry, this enum can delegate to it without changing the public
# MCP API (the Tool values are the stable contract).
# --------------------------------------------------------------------------- #


class Tool(str, Enum):
    """A coding assistant whose MCP config we can patch.

    ``str`` enum so CLI values (``--tool claude-code``) round-trip through
    :func:`Tool` directly. The value is the canonical tool id used in the
    upstream managers.
    """

    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    CRUSH = "crush"
    FACTORY_DROID = "factory-droid"


# The JSON object key under which each tool stores its MCP server table. Claude
# Code and Factory Droid use ``mcpServers``; OpenCode and Crush use ``mcp``.
# Single source of truth so install/uninstall/list/is_installed never disagree.
_MCP_SECTION: dict[Tool, str] = {
    Tool.CLAUDE_CODE: "mcpServers",
    Tool.OPENCODE: "mcp",
    Tool.CRUSH: "mcp",
    Tool.FACTORY_DROID: "mcpServers",
}


def tool_config_path(tool: Tool, home: str | Path) -> Path:
    """Resolve the config file path for ``tool`` under ``home`` (pure).

    Each tool stores its MCP config at a fixed, tool-specific location::

        claude-code    home/.claude.json
        opencode       home/.config/opencode/opencode.json
        crush          home/.config/crush/crush.json
        factory-droid  home/.factory/mcp.json

    Pure path arithmetic — no IO, no existence check (mirrors
    :meth:`Paths.from_home`).
    """
    h = Path(home)
    match tool:
        case Tool.CLAUDE_CODE:
            return h / ".claude.json"
        case Tool.OPENCODE:
            return h / ".config" / "opencode" / "opencode.json"
        case Tool.CRUSH:
            return h / ".config" / "crush" / "crush.json"
        case Tool.FACTORY_DROID:
            return h / ".factory" / "mcp.json"


# --------------------------------------------------------------------------- #
# Preset MCP service table — parity with upstream PRESET_MCP_SERVICES.
#
# Each preset is a plain dict (not a dataclass) so it serializes 1:1 to the
# shapes the upstream ships; the docstring of the stdio ``command``/``args`` and
# the http ``urlTemplate``/``envTemplate`` is the parity contract. The keys
# mirror the upstream field names verbatim (``protocol``, ``requiresAuth``,
# ``envTemplate`` keyed by the upstream plan id, ``urlTemplate`` likewise).
# --------------------------------------------------------------------------- #

#: Upstream plan ids used as the ``envTemplate``/``urlTemplate`` keys. The
#: GLOBAL region maps to the ``..._global`` plan; CHINA to ``..._china``. Kept
#: as literals (not derived from :class:`Region`) because they are an external
#: parity contract — the upstream string keys, not our enum names.
_PLAN_ID: dict[Region, str] = {
    Region.GLOBAL: "glm_coding_plan_global",
    Region.CHINA: "glm_coding_plan_china",
}

#: The four preset MCP servers, region-aware. Field names mirror the upstream
#: ``PRESET_MCP_SERVICES`` so a future parity test can diff them directly. The
#: stdio preset (``zai-mcp-server``) carries an ``envTemplate`` (``Z_AI_MODE``);
#: the three http presets carry a ``urlTemplate`` (the region-specific endpoint).
#: Auth is applied at entry-build time (``Z_AI_API_KEY`` env / ``Authorization``
#: header) so the secret never lives in this static table.
PRESET_MCP_SERVICES: list[dict[str, Any]] = [
    {
        "id": "zai-mcp-server",
        "name": "Vision MCP",
        "type": "builtin",
        "protocol": "stdio",
        "requiresAuth": True,
        "description": "Vision MCP Local Server",
        "command": "npx",
        "args": ["-y", "@z_ai/mcp-server"],
        "envTemplate": {
            "glm_coding_plan_global": {"Z_AI_MODE": "ZAI"},
            "glm_coding_plan_china": {"Z_AI_MODE": "ZHIPU"},
        },
    },
    {
        "id": "web-search-prime",
        "name": "Web Search MCP",
        "type": "builtin",
        "protocol": "streamable-http",
        "requiresAuth": True,
        "description": "Web Search Prime MCP Server",
        "urlTemplate": {
            "glm_coding_plan_global": "https://api.z.ai/api/mcp/web_search_prime/mcp",
            "glm_coding_plan_china": "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp",
        },
    },
    {
        "id": "web-reader",
        "name": "Web Reader MCP",
        "type": "builtin",
        "protocol": "streamable-http",
        "requiresAuth": True,
        "description": "Web URL Reader MCP Server",
        "urlTemplate": {
            "glm_coding_plan_global": "https://api.z.ai/api/mcp/web_reader/mcp",
            "glm_coding_plan_china": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
        },
    },
    {
        "id": "zread",
        "name": "ZRead MCP",
        "type": "builtin",
        "protocol": "streamable-http",
        "requiresAuth": True,
        "description": "ZRead Github MCP Server",
        "urlTemplate": {
            "glm_coding_plan_global": "https://api.z.ai/api/mcp/zread/mcp",
            "glm_coding_plan_china": "https://open.bigmodel.cn/api/mcp/zread/mcp",
        },
    },
]


#: A preset dict alias for type hints (the entries in :data:`PRESET_MCP_SERVICES`).
#: Each preset is a plain dict with the upstream's field names (``id``,
#: ``protocol``, ``requiresAuth``, ``envTemplate``/``urlTemplate``, ...).
McpPreset = dict[str, Any]


def preset_by_id(mcp_id: str) -> McpPreset | None:
    """Return the preset with id ``mcp_id``, or ``None`` if no such preset.

    Linear scan — the preset table is four entries, so a dict index would be
    over-engineering. ``None`` (not raising) lets the caller produce a clean
    "unknown MCP id" error at the CLI/IO boundary.
    """
    for preset in PRESET_MCP_SERVICES:
        if preset["id"] == mcp_id:
            return preset
    return None


def preset_ids() -> list[str]:
    """Return the ids of all preset MCPs, in declaration order."""
    return [p["id"] for p in PRESET_MCP_SERVICES]


def _resolve_template(template: dict[str, Any], region: Region) -> Any:
    """Pick the region-specific entry from a ``urlTemplate``/``envTemplate``.

    The templates are keyed by the upstream plan id (``glm_coding_plan_*``);
    we translate our :class:`Region` to that key once, here. A missing region
    key falls back to an empty value so a half-specified template degrades to a
    clear downstream "missing url/env" error rather than a ``KeyError``.
    """
    return template.get(_PLAN_ID[region])


# --------------------------------------------------------------------------- #
# Pure entry builders — one MCP server entry, shaped for a specific tool.
# --------------------------------------------------------------------------- #


def _stdio_env(preset: McpPreset, key: str | None, region: Region) -> dict[str, str]:
    """Build the env dict for a stdio MCP entry.

    Mirrors the upstream: start from the region-specific ``envTemplate`` entry
    (e.g. ``Z_AI_MODE``), then add ``Z_AI_API_KEY`` when the preset requires
    auth and a key was supplied. A preset with no env template starts empty.
    """
    env: dict[str, str] = {}
    template = preset.get("envTemplate")
    if template:
        region_env = _resolve_template(template, region)
        if isinstance(region_env, dict):
            env.update(region_env)
    # The upstream also honors a fixed ``env`` field; presets do not use it,
    # but apply it for parity if a caller-supplied preset carries one.
    fixed = preset.get("env")
    if isinstance(fixed, dict):
        env.update(fixed)
    if preset.get("requiresAuth") and key:
        env["Z_AI_API_KEY"] = key
    return env


def _http_url(preset: McpPreset, region: Region) -> str:
    """Resolve the region-specific URL for an http MCP entry.

    ``urlTemplate`` keyed by plan id first, then a fixed ``url`` fallback.
    Raises :class:`ValueError` if neither is present — the caller (the CLI)
    wraps it into the project error contract.
    """
    template = preset.get("urlTemplate")
    if template:
        url = _resolve_template(template, region)
        if isinstance(url, str) and url:
            return url
    url = preset.get("url")
    if isinstance(url, str) and url:
        return url
    raise ValueError(
        f"MCP {preset.get('id')} requires a URL but none was provided"
    )


def build_mcp_entry(
    tool: Tool,
    mcp_id: str,
    key: str | None,
    region: Region,
    *,
    presets: list[McpPreset] | None = None,
) -> dict[str, Any]:
    """Build ONE tool-specific MCP server entry for preset ``mcp_id`` (pure).

    This is the parity-critical transform: for a given tool and preset it
    produces exactly the dict the upstream's per-tool manager would write under
    ``<section>[mcp_id]``. Pure — returns the entry without touching the
    filesystem.

    Args:
        tool: the install target (shapes the entry — see module docstring).
        mcp_id: a preset id from :data:`PRESET_MCP_SERVICES`.
        key: the Z.ai API key. Added as ``Z_AI_API_KEY`` env (stdio) or
            ``Authorization: Bearer <key>`` header (http) when the preset
            requires auth. ``None`` omits auth (an install without a key).
        region: selects the URL host / ``Z_AI_MODE`` value.
        presets: optional preset override (tests inject a custom table).

    Raises:
        ValueError: if ``mcp_id`` is not a known preset, or an http preset has
            no resolvable URL for ``region``.
    """
    table = presets if presets is not None else PRESET_MCP_SERVICES
    preset = next((p for p in table if p["id"] == mcp_id), None)
    if preset is None:
        raise ValueError(f"Unknown MCP preset: {mcp_id}")

    protocol = preset["protocol"]
    if protocol == "stdio":
        return _build_stdio_entry(tool, preset, key, region)
    if protocol in ("sse", "streamable-http"):
        return _build_http_entry(tool, preset, key, region)
    raise ValueError(f"Unsupported protocol for {mcp_id}: {protocol}")


def _build_stdio_entry(
    tool: Tool, preset: McpPreset, key: str | None, region: Region
) -> dict[str, Any]:
    """Build a stdio MCP entry, tool-shaped (parity per-tool managers)."""
    env = _stdio_env(preset, key, region)
    command = preset.get("command") or "npx"
    args = preset.get("args") or []
    if tool is Tool.OPENCODE:
        # OpenCode uses ``local`` + a single ``command`` array + ``environment``.
        return {
            "type": "local",
            "command": [command, *args],
            "environment": env,
        }
    entry: dict[str, Any] = {
        "type": "stdio",
        "command": command,
        "args": list(args),
        "env": env,
    }
    if tool is Tool.FACTORY_DROID:
        entry["disabled"] = False
    return entry


def _build_http_entry(
    tool: Tool, preset: McpPreset, key: str | None, region: Region
) -> dict[str, Any]:
    """Build an http/sse MCP entry, tool-shaped (parity per-tool managers)."""
    url = _http_url(preset, region)
    # streamable-http -> "http" (OpenCode: "remote"); sse -> "sse". The presets
    # are all streamable-http, but the branch keeps sse parity if a custom
    # preset uses it.
    protocol = preset["protocol"]
    if tool is Tool.OPENCODE:
        http_type: Literal["remote", "sse"] = "sse" if protocol == "sse" else "remote"
    else:
        http_type = "sse" if protocol == "sse" else "http"  # type: ignore[assignment]
    headers: dict[str, str] = dict(preset.get("headers") or {})
    if preset.get("requiresAuth") and key:
        # Capital-A ``Authorization`` matches the upstream exactly (parity).
        headers["Authorization"] = f"Bearer {key}"
    entry: dict[str, Any] = {
        "type": http_type,
        "url": url,
        "headers": headers,
    }
    if tool is Tool.FACTORY_DROID:
        entry["disabled"] = False
    return entry


# --------------------------------------------------------------------------- #
# Pure document transforms — deep-merge install / surgical uninstall.
# --------------------------------------------------------------------------- #


def install_into_doc(
    doc: dict[str, Any] | None,
    tool: Tool,
    mcp_id: str,
    key: str | None,
    region: Region,
    *,
    presets: list[McpPreset] | None = None,
) -> dict[str, Any]:
    """Return a NEW config doc with ``mcp_id`` installed into ``tool``'s section.

    Pure: reads nothing, writes nothing. Foreign top-level keys and foreign MCP
    entries (other ``mcp_id``s, including ones the user added by hand) are
    preserved verbatim — only ``<section>[mcp_id]`` is set/overwritten. A
    missing section is created. The input ``doc`` is not mutated.

    Args:
        doc: the parsed config document, or ``None`` (treated as empty — the
            file does not exist yet).
        tool: the install target (selects the section + entry shape).
        mcp_id: the preset id to install.
        key: the Z.ai API key (``None`` to install without auth).
        region: region for URL / env-template selection.
        presets: optional preset override for tests.

    Returns:
        The new top-level document with the entry installed.
    """
    entry = build_mcp_entry(tool, mcp_id, key, region, presets=presets)
    # Shallow-copy the top level so foreign keys are preserved without mutation
    # of the caller's dict; the section itself is rebuilt (copy) so we never
    # alias the input's nested mapping.
    out: dict[str, Any] = dict(doc) if doc else {}
    section_key = _MCP_SECTION[tool]
    section = dict(out.get(section_key) or {})
    section[mcp_id] = entry
    out[section_key] = section
    return out


def uninstall_from_doc(
    doc: dict[str, Any] | None, tool: Tool, mcp_id: str
) -> dict[str, Any]:
    """Return a NEW config doc with ``mcp_id`` removed from ``tool``'s section.

    Pure. Removes ONLY ``<section>[mcp_id]`` — every other entry and every
    foreign top-level key is preserved. A missing doc, missing section, or
    absent ``mcp_id`` is a no-op (idempotent), returning a copy of the input.
    The section is dropped entirely when uninstalling leaves it empty, so we
    never write a stray ``"mcpServers": {}`` skeleton into a user's file.
    """
    if not doc:
        return {}
    out: dict[str, Any] = dict(doc)
    section_key = _MCP_SECTION[tool]
    section = out.get(section_key)
    if not isinstance(section, dict) or mcp_id not in section:
        return out
    section = dict(section)
    section.pop(mcp_id, None)
    if section:
        out[section_key] = section
    else:
        # An empty section would be noise in the user's config — drop it.
        out.pop(section_key, None)
    return out


def list_installed(doc: dict[str, Any] | None, tool: Tool) -> list[str]:
    """Return the ids installed in ``tool``'s MCP section (pure read).

    ``None`` doc / missing section → ``[]``. Order is the document's insertion
    order (Python dict preserves it; JSON round-trips it).
    """
    if not doc:
        return []
    section = doc.get(_MCP_SECTION[tool])
    if not isinstance(section, dict):
        return []
    return list(section.keys())


def is_installed(doc: dict[str, Any] | None, tool: Tool, mcp_id: str) -> bool:
    """True iff ``mcp_id`` is present in ``tool``'s MCP section (pure read)."""
    return mcp_id in list_installed(doc, tool)


# --------------------------------------------------------------------------- #
# IO layer — read/write the tool config file atomically.
# --------------------------------------------------------------------------- #


@runtime_checkable
class ConfigReader(Protocol):
    """:func:`read_config` seam: parse a tool config file → dict | None."""

    def __call__(self, path: Path) -> dict[str, Any] | None: ...


@runtime_checkable
class ConfigWriter(Protocol):
    """:func:`write_config` seam: persist a config dict atomically."""

    def __call__(self, path: Path, doc: dict[str, Any]) -> None: ...


def read_config(path: Path) -> dict[str, Any] | None:
    """Read a tool's MCP config file → parsed dict, or ``None`` if absent.

    Delegates to :class:`~zai_python_helper.backends.JsonBackend`, which treats
    an empty file as ``None`` and raises :class:`ConfigurationError` on a
    malformed document (rather than crashing with a bare ``JSONDecodeError``).
    """
    from zai_python_helper.backends import JsonBackend

    return JsonBackend.read(Path(path))


def write_config(path: Path, doc: dict[str, Any]) -> None:
    """Persist ``doc`` to ``path`` atomically (temp + fsync + replace).

    Delegates to :class:`~zai_python_helper.backends.JsonBackend`, which
    preserves insertion order and writes with a trailing newline. The parent
    directory is created if missing.
    """
    from zai_python_helper.backends import JsonBackend

    JsonBackend.write(Path(path), doc)


# --------------------------------------------------------------------------- #
# High-level importable cycle — compose pure transforms with IO.
# --------------------------------------------------------------------------- #


def install_mcp(
    tool: Tool | str,
    mcp_id: str,
    key: str | None,
    region: Region,
    *,
    home: str | Path | None = None,
    presets: list[McpPreset] | None = None,
    reader: ConfigReader = read_config,
    writer: ConfigWriter = write_config,
) -> bool:
    """Install preset ``mcp_id`` into ``tool``'s MCP config (importable, S7).

    The one-shot IO cycle: read the tool config, deep-merge the preset entry
    in via :func:`install_into_doc`, and write it back atomically. Foreign keys
    and foreign MCP entries are preserved.

    IMPORTABLE (issue #18): the pure transform (:func:`install_into_doc`) and
    the IO (:func:`read_config`/:func:`write_config`) are separately
    injectable, so a caller that already holds the parsed document (or wants a
    fake filesystem in tests) can bypass the defaults.

    Args:
        tool: the install target (a :class:`Tool` or its string value, e.g.
            ``"claude-code"``).
        mcp_id: the preset id to install.
        key: the Z.ai API key (``None`` installs without auth).
        region: region for URL / env-template selection.
        home: the user home the tool config lives under. Defaults to
            :func:`pathlib.Path.home` (production); tests inject a tmp dir.
        presets: optional preset-table override (tests).
        reader: injectable config reader (tests fake the FS).
        writer: injectable config writer (tests fake the FS).

    Returns:
        ``True`` if the entry was added/updated, ``False`` if it was already
        present with the identical value (idempotent no-op).

    Raises:
        ValueError: if ``mcp_id`` is not a known preset (propagated from
            :func:`build_mcp_entry`).
    """
    tool_enum = tool if isinstance(tool, Tool) else Tool(tool)
    h = Path(home) if home is not None else Path.home()
    path = tool_config_path(tool_enum, h)
    current = reader(path)
    new_doc = install_into_doc(current, tool_enum, mcp_id, key, region, presets=presets)
    if current is not None and current == new_doc:
        return False
    writer(path, new_doc)
    return True


def uninstall_mcp(
    tool: Tool | str,
    mcp_id: str,
    *,
    home: str | Path | None = None,
    reader: ConfigReader = read_config,
    writer: ConfigWriter = write_config,
) -> bool:
    """Remove preset ``mcp_id`` from ``tool``'s MCP config (importable, S7).

    The one-shot inverse of :func:`install_mcp`: read, surgically remove just
    ``<section>[mcp_id]`` via :func:`uninstall_from_doc`, and write back
    atomically. Idempotent — removing an absent id is a no-op that writes
    nothing and returns ``False``.

    Args:
        tool: the install target (a :class:`Tool` or its string value).
        mcp_id: the preset id to remove.
        home: the user home the tool config lives under (default: ``Path.home``).
        reader: injectable config reader (tests).
        writer: injectable config writer (tests).

    Returns:
        ``True`` if the entry was removed, ``False`` if it was absent.
    """
    tool_enum = tool if isinstance(tool, Tool) else Tool(tool)
    h = Path(home) if home is not None else Path.home()
    path = tool_config_path(tool_enum, h)
    current = reader(path)
    if current is None:
        return False
    new_doc = uninstall_from_doc(current, tool_enum, mcp_id)
    if new_doc == current:
        return False
    writer(path, new_doc)
    return True
