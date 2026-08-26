"""
core/planner — Pure planning functions (v1 domain).

Per ADR-001, this module contains PURE functions that transform
parsed config documents into PatchPlans. No IO, no env access, no file
operations, no ``getpass``. The planner is responsible for:

- Understanding the structure of tool config files
- Generating deltas (PatchPlans) to achieve a desired state
- Validating postconditions

A :class:`PatchPlan` is an ordered list of :class:`FileDelta` records. Each
delta is keyed by a semantic *file tag* (``settings`` / ``claude_json`` /
``zshrc``), NOT by a concrete ``pathlib.Path``: the planner never resolves
paths — that is the CLI layer's job (via :class:`~zai_python_helper.paths.Paths`).
The CLI maps each tag to its resolved path before handing the delta to the IO
layer (:mod:`zai_python_helper.backends`), which turns it into an atomic write.

This module re-exports the public planning surface so callers can import
everything from a single place::

    from zai_python_helper.core.planner import (
        PatchPlan, plan_zai, plan_default, postconditions,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeltaKind(Enum):
    """The kind of mutation a :class:`FileDelta` represents.

    - ``WRITE_JSON``: write parsed JSON (``content`` is a ``dict``). The
      backend uses the upstream indentation for the target file and no
      trailing newline.
    - ``WRITE_TEXT``: write raw text (``content`` is a ``str``).
    - ``NOOP``: the file already matches the desired state; nothing to do.
      Kept (rather than omitted) so ``--dry-run`` can report that the file was
      considered and intentionally left untouched.
    """

    WRITE_JSON = "write_json"
    WRITE_TEXT = "write_text"
    NOOP = "noop"


class FileTag(Enum):
    """Semantic identifier for a managed file (decoupled from its path).

    The planner addresses files by tag; the CLI maps a tag to a concrete
    :class:`~pathlib.Path` via :class:`~zai_python_helper.paths.Paths`. This
    keeps path resolution out of the pure planner (ADR-001) and out of the
    PatchPlan data structure, so a plan is independent of ``$HOME``.
    """

    SETTINGS = "settings"  # ~/.claude/settings.json
    CLAUDE_JSON = "claude_json"  # ~/.claude.json
    ZSHRC = "zshrc"  # ~/.zshrc
    # S6 tools (issue #7). Each planner addresses its own config file by tag;
    # the CLI maps a tag to a concrete path via Paths (paths.py).
    OPENCODE = "opencode"  # ~/.config/opencode/opencode.json
    CRUSH = "crush"  # ~/.config/crush/crush.json
    FACTORY_DROID = "factory_droid"  # ~/.factory/settings.json


@dataclass(frozen=True)
class FileDelta:
    """A single file's intended mutation, addressed by semantic tag.

    Attributes:
        tag: Which managed file this delta targets (see :class:`FileTag`).
        kind: What the backend should do (see :class:`DeltaKind`).
        content: The desired post-state of the file. For ``WRITE_JSON`` a
            ``dict`` (the full merged document — the planner computes the
            merge so the IO layer stays dumb). For ``WRITE_TEXT`` the full
            file text. For ``NOOP`` the unchanged content.
    """

    tag: FileTag
    kind: DeltaKind
    content: Any


@dataclass(frozen=True)
class PatchPlan:
    """An ordered list of file deltas describing a complete activation.

    Per ADR-005, activating a tool touches up to three files and the plan is
    fully validated BEFORE any write. The ordering is preserved so logging
    and ``--dry-run`` output are deterministic; deltas do not depend on each
    other's write order within a single plan.

    Attributes:
        deltas: Ordered tuple of :class:`FileDelta`. A tuple (not a list) so
            the plan is hashable and frozen — once built it cannot be
            tampered with before execution.
    """

    deltas: tuple[FileDelta, ...] = field(default_factory=tuple)

    def delta_for(self, tag: FileTag) -> FileDelta | None:
        """Return the delta for ``tag``, or ``None`` if the plan omits it."""
        for d in self.deltas:
            if d.tag == tag:
                return d
        return None

    @property
    def is_empty(self) -> bool:
        """True if every delta is a NOOP (nothing would change on disk)."""
        return all(d.kind == DeltaKind.NOOP for d in self.deltas)

    @property
    def has_writes(self) -> bool:
        """True if at least one delta would write to disk."""
        return any(d.kind != DeltaKind.NOOP for d in self.deltas)


# Re-export the public planning surface for Claude Code. The submodule only
# defines pure functions, so importing it here is safe under ADR-001.
from zai_python_helper.core.planner.claude_code import (  # noqa: E402
    plan_default,
    plan_revert,
    plan_zai,
    postconditions,
    revert_key_set,
)

__all__ = [
    "DeltaKind",
    "FileDelta",
    "FileTag",
    "PatchPlan",
    "plan_default",
    "plan_revert",
    "plan_zai",
    "postconditions",
    "revert_key_set",
]
