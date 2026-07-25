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
3. project settings — .claude/settings.json in project
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


def resolve_effective_env(
    user_settings_path: Path,
    project_settings_path: Path,
    local_settings_path: Path,
) -> dict[str, str] | None:
    """Resolve the EFFECTIVE Claude settings env block per precedence.

    Reads settings.json from all scopes (user, project, local) and merges the
    env blocks per Claude Code's precedence: managed > local > project > user.
    Higher-precedence scopes override individual keys (not the entire block).

    Args:
        user_settings_path: Path to ~/.claude/settings.json (user scope).
        project_settings_path: Path to .claude/settings.json (project scope).
        local_settings_path: Path to .claude/settings.local.json (local scope).

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

    # Project (medium-low precedence)
    project = JsonBackend.read(project_settings_path)
    if project and isinstance(project.get("env"), dict):
        effective.update(_as_str_dict(project["env"]))

    # Local (medium-high precedence)
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
