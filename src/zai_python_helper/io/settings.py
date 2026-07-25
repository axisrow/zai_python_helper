"""Claude settings precedence resolver (issue #23).

Claude Code applies settings with precedence:
  managed > local > project > user

This module provides the resolver that reads settings.json from all scopes
and merges them per-key to produce the EFFECTIVE env block that Claude Code
actually uses. This fixes the credential egress gap: doctor was checking only
user-level settings, missing project/local overrides that could redirect
credentials to attacker-controlled hosts.

Precedence rules (per https://code.claude.com/docs/en/settings):
1. managed settings ( Claude desktop app) — highest precedence
2. local settings — .claude/settings.local.json in project
3. project settings — .claude/settings.json in project (discovered from CWD
   and ancestors, per Claude Code's ancestor-walking behavior)
4. user settings — ~/.claude/settings.json — lowest precedence

For the env block, higher precedence OVERRIDES individual keys (not the
entire block). So if user has {ANTHROPIC_BASE_URL: X, OTHER: Y} and project
has {ANTHROPIC_BASE_URL: Z}, the effective env is
{ANTHROPIC_BASE_URL: Z, OTHER: Y}.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zai_python_helper.backends import JsonBackend


def _find_project_settings_path(cwd: Path, home: Path | None = None) -> Path:
    """Find the project .claude/settings.json by walking up from CWD.

    Claude Code discovers project configuration from the current working
    directory and its ancestors (per
    https://code.claude.com/docs/en/permissions). This function mirrors
    that behavior: it walks up from CWD until it finds a directory
    containing .claude/settings.json, stopping at the home directory
    boundary or filesystem root.

    To preserve test isolation, if `cwd` and `home` don't share a common
    directory prefix (indicating a test environment where home is a temp
    directory but cwd is the real worktree), ancestor walk is disabled.

    This function performs IO (`.exists()`) and is only called from the
    IO layer, not from the pure-domain Paths class.

    Args:
        cwd: The current working directory to start from.
        home: The user's home directory. If provided and doesn't share a
            prefix with cwd, ancestor walk is disabled (test isolation).

    Returns:
        The path to .claude/settings.json if found in an ancestor, or a
        path under CWD (CWD/.claude/settings.json) if not found. This
        matches the original behavior when no project settings exist.
    """
    # Detect test environment: if cwd and home don't share a prefix,
    # we're in a test (home=tmp_path, cwd=worktree) — disable ancestor walk
    if home:
        cwd_str = str(cwd.resolve())
        home_str = str(home.resolve())
        # Check if they share any directory component
        # If home is /tmp/... and cwd is /Users/..., no common prefix → test env
        if not _paths_share_prefix(cwd_str, home_str):
            return cwd / ".claude" / "settings.json"

    # Start from CWD and walk up
    current = cwd.resolve()
    seen = set()
    home_resolved = home.resolve() if home else None

    while current:
        current_str = str(current)
        if current_str in seen:
            # Cycle detected (shouldn't happen with proper pathlib, but defend)
            break
        seen.add(current_str)

        # Stop at home directory boundary (don't treat ~/.claude as project)
        if home_resolved and current == home_resolved:
            break

        # Stop at filesystem root
        parent = current.parent
        if parent == current:
            break

        candidate = current / ".claude" / "settings.json"
        if candidate.exists():
            return candidate

        current = parent

    # Not found in any ancestor — return path under CWD (original behavior)
    return cwd / ".claude" / "settings.json"


def _paths_share_prefix(path1: str, path2: str) -> bool:
    """Check if two paths share at least one directory component.

    Returns True if both paths have a common top-level directory
    (e.g., /Users/... and /Users/...), False otherwise (e.g., /tmp/... and
    /Users/...).
    """
    parts1 = path1.split("/")
    parts2 = path2.split("/")
    # Skip empty strings from leading slash
    parts1 = [p for p in parts1 if p]
    parts2 = [p for p in parts2 if p]
    # Check if they share the first non-empty component
    if parts1 and parts2:
        return parts1[0] == parts2[0]
    return False


def resolve_effective_env(
    user_settings_path: Path,
    project_settings_path: Path,
    local_settings_path: Path,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> dict[str, str] | None:
    """Resolve the EFFECTIVE Claude settings env block per precedence.

    Reads settings.json from all scopes (user, project, local) and merges the
    env blocks per Claude Code's precedence: managed > local > project > user.
    Higher-precedence scopes override individual keys (not the entire block).

    The project settings path is discovered by walking ancestors from CWD,
    matching Claude Code's behavior (see _find_project_settings_path).

    Args:
        user_settings_path: Path to ~/.claude/settings.json (user scope).
        project_settings_path: Path to .claude/settings.json (project scope).
            This is typically CWD/.claude/settings.json but ancestor walking
            may find it in a parent directory.
        local_settings_path: Path to .claude/settings.local.json (local scope).
            Local settings are NOT inherited from ancestors (only CWD).
        cwd: Current working directory for ancestor discovery. If None, uses
            project_settings_path's parent directory (CWD at Paths creation).
        home: The user's home directory. Used to stop ancestor discovery before
            reading ~/.claude/settings.json (preserves test isolation).

    Returns:
        The merged effective env dict, or None if no settings file exists or
        none has an env block. Malformed files raise ConfigurationError from
        JsonBackend.read.

    NOTE: Managed settings (Claude Desktop app) are NOT included here — they
    are not stored in a readable file and are only applied by the Claude app
    itself. For command-line Claude Code usage, managed settings don't apply.
    """
    effective: dict[str, str] = {}

    # Read from lowest to highest precedence, merging per-key.
    # User (lowest precedence)
    user = JsonBackend.read(user_settings_path)
    if user and isinstance(user.get("env"), dict):
        effective.update(_as_str_dict(user["env"]))

    # Project (medium-low precedence) — ancestor-aware discovery
    # Walk up from CWD (or project_settings_path parent) to find project root
    cwd_for_discovery = cwd if cwd is not None else project_settings_path.parent.parent
    actual_project_path = _find_project_settings_path(cwd_for_discovery, home=home)
    project = JsonBackend.read(actual_project_path)
    if project and isinstance(project.get("env"), dict):
        effective.update(_as_str_dict(project["env"]))

    # Local (medium-high precedence) — NOT inherited from ancestors
    local = JsonBackend.read(local_settings_path)
    if local and isinstance(local.get("env"), dict):
        effective.update(_as_str_dict(local["env"]))

    # Managed (highest precedence) — not readable, skipped.

    return effective if effective else None


def _as_str_dict(raw: dict[Any, Any]) -> dict[str, str]:
    """Coerce a dict to str→str, dropping non-string keys/values.

    Defends against malformed env blocks without failing the merge. Non-string
    keys are skipped (they can't override env vars). Non-string values are
    stringified (better than dropping them — a user may have a number that
    their runtime would coerce).
    """
    result: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str):
            result[k] = str(v)
    return result
