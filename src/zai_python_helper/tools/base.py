"""Tool protocol + ownership-field descriptor (S6 foundation, issue #7).

A ``Tool`` is the composition root the CLI dispatches on. It owns, for one
coding tool (Claude Code, OpenCode, Crush, Factory Droid):

- **which files** it manages (semantic :class:`~zai_python_helper.core.planner.FileTag`
  set + how to read them off :class:`~zai_python_helper.paths.Paths`);
- **how to plan** an activation / reversion (delegating to the PURE planner in
  :mod:`zai_python_helper.core.planner`);
- **which fields it owns** — a closed set of :class:`ManagedField` descriptors
  that know how to get/set a value at a flat or nested location in a parsed
  config document, and a stable string ``key`` for the ownership journal;
- **status / postcondition** reporting.

The CLI is therefore a GENERIC dispatcher over :data:`~zai_python_helper.tools.REGISTRY`:
it never branches on the tool name. Every Claude-Code-specific assumption that
previously lived in ``cli.py`` (the flat ``settings.json::env`` block, the
``ANTHROPIC_*`` key set) moves into :class:`~zai_python_helper.tools.claude_code.ClaudeCodeTool`,
expressed as ``ManagedField`` instances.

Layering (ADR-001): this module is IO-free at the planning level (``plan_zai``
etc. delegate to the pure planner), but :meth:`Tool.read_state` and
:meth:`Tool.status_row` DO read files — that is intentional, because a Tool is
the *composition root* above the pure core, not part of the pure core itself.
The pure transforms stay pure and importable via
``zai_python_helper.core.planner.<tool>``.

Ownership for nested fields
---------------------------
The ownership journal (:mod:`zai_python_helper.ownership`) is keyed
``{tool: {key: record}}`` where ``key`` is a plain string. Claude Code uses
flat env-var names (``ANTHROPIC_AUTH_TOKEN``). The S6 tools own fields nested
inside their JSON docs (``providers.zai.api_key``, ``customModels[...].apiKey``).
We adopt **dotted-path string keys** for nested dict fields and **stable
synthetic sub-keys** for array elements (by protocol, never by index — an
index shifts on filter and would break idempotent revert). The ``ManagedField``
descriptor hides the nesting from the CLI: it exposes ``get`` / ``set_value``
over a parsed doc, so the CLI only ever deals with ``(key, value)`` pairs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from zai_python_helper.core.domain import ProviderSpec
from zai_python_helper.core.planner import FileTag, PatchPlan
from zai_python_helper.regions import Region

if TYPE_CHECKING:
    from zai_python_helper.ownership import RevertDecision
    from zai_python_helper.paths import Paths


@runtime_checkable
class ManagedField(Protocol):
    """One field a tool owns, addressable for the ownership journal.

    A field may live at a flat top-level key or deep inside a nested JSON
    document (or inside an array element). The descriptor hides that structure
    behind two operations so the CLI's ownership capture / revert logic stays
    tool-agnostic.

    Attributes:
        key: The STABLE string key used as the ownership-journal sub-key
            (``{tool: {key: record}}``). Flat names for top-level fields
            (``ANTHROPIC_AUTH_TOKEN``, ``model``); dotted paths for nested
            dict fields (``providers.zai.api_key``); dotted synthetic paths
            for array elements addressed by a stable discriminator rather than
            index (``customModels.anthropic.apiKey``). NEVER an array index —
            an index shifts on filter and would break idempotent revert.

    A field is "removed" when its value is ``None``; :meth:`set_value`
    translates that to the right structural deletion (drop the key, pop the
    array element, etc.).
    """

    key: str

    def get(self, doc: dict[str, Any] | None) -> tuple[bool, str | None]:
        """Return ``(present, value)`` for this field in ``doc``.

        ``present`` is False when the field (or any of its parents) is absent;
        ``value`` is the field's value coerced to ``str``-comparable form when
        present, else ``None``. ``present``/``value`` feed the ownership
        journal's prior-value capture and the revert-time current-value check.
        """
        ...

    def set_value(self, doc: dict[str, Any], value: str | None) -> dict[str, Any]:
        """Return a NEW doc with this field set to ``value`` (or removed).

        ``value is None`` means REMOVE the field (drop the key, filter the
        array element). Never mutates the input ``doc``. Foreign keys always
        round-trip untouched.
        """
        ...


@dataclass(frozen=True)
class StatusRow:
    """One tool's line(s) in the ``status`` report (read-only detect output).

    Attributes:
        tool: The tool name (matches :attr:`Tool.name`).
        configured: Whether the tool's config file exists / is detectable.
        zai_active: Whether Z.ai appears active for this tool (postcondition).
        region: Inferred region, or ``None`` if not determinable.
        detail: Human-readable extra info (e.g. masked key, provider name).
            MUST NOT carry secrets — values are masked upstream by the tool.
    """

    tool: str
    configured: bool
    zai_active: bool
    region: Region | None
    detail: str = ""


class Tool(ABC):
    """A config-patching tool integration (the CLI's dispatch unit).

    Concrete tools (``ClaudeCodeTool``, ``OpenCodeTool``, ...) implement this.
    The CLI holds a ``{name: Tool}`` registry (:data:`~zai_python_helper.tools.REGISTRY`)
    and dispatches ``use zai`` / ``use default`` generically: it reads state,
    plans, captures ownership, commits, and echoes — all via these methods, so
    no tool-specific branch ever lives in ``cli.py``.

    Class attributes:
        name: The tool identifier (matches the ``--tool`` value and the
            ownership-journal bucket key).
        file_tags: The closed set of :class:`FileTag` this tool's plan may
            emit deltas for. The CLI reads exactly these files and applies
            only deltas whose tag is in this set.
    """

    name: str
    file_tags: tuple[FileTag, ...]

    # ------------------------------------------------------------------
    # File IO (read state under the lock)
    # ------------------------------------------------------------------

    @abstractmethod
    def read_state(self, paths: Paths) -> dict[FileTag, Any]:
        """Read the parsed docs/texts this tool plans against.

        Returns ``{tag: doc_or_text}`` for every tag in :attr:`file_tags`.
        The CLI calls this inside the held process lock so the plan reflects a
        consistent snapshot. JSON tags yield a ``dict | None``; text tags
        (``ZSHRC``) yield a ``str``.
        """
        ...

    # ------------------------------------------------------------------
    # Pure planning (delegate to core/planner/<tool>.py)
    # ------------------------------------------------------------------

    @abstractmethod
    def plan_zai(
        self,
        spec: ProviderSpec,
        region: Region,
        *,
        state: dict[FileTag, Any],
        auth_token: str,
    ) -> PatchPlan:
        """Plan the ``use zai`` activation against the read ``state``."""
        ...

    @abstractmethod
    def plan_revert(
        self,
        *,
        state: dict[FileTag, Any],
        decisions: dict[str, RevertDecision],
    ) -> PatchPlan:
        """Plan the journal-aware ``use default`` reversion.

        ``decisions`` is ``{field.key: RevertDecision}`` for every key in
        :meth:`revert_key_set`; the tool applies them back through its
        :class:`ManagedField` descriptors.
        """
        ...

    # ------------------------------------------------------------------
    # Ownership descriptor (replaces CLI's env-specific helpers)
    # ------------------------------------------------------------------

    @abstractmethod
    def managed_fields(self, spec: ProviderSpec) -> list[ManagedField]:
        """The closed set of fields this tool owns for ``spec``'s model mode.

        The set may depend on the mode (e.g. Claude Code DEFAULT contributes
        extra ``ANTHROPIC_DEFAULT_*_MODEL`` fields). The CLI journals exactly
        these.
        """
        ...

    @abstractmethod
    def revert_key_set(self) -> tuple[str, ...]:
        """The UNION of journal keys ANY activation could have set.

        ``use default`` considers every key here regardless of the current
        mode, so a cross-mode revert is clean (no stale keys left behind).
        """
        ...

    @abstractmethod
    def extract_takeover(
        self,
        plan: PatchPlan,
        prior_state: dict[FileTag, Any],
        spec: ProviderSpec,
    ) -> list[tuple[str, str | None, bool, str | None]]:
        """Compute ``(key, prior_value, prior_present, set_hash)`` per owned field.

        For each field in :meth:`managed_fields`: prior comes from the
        PRE-patch ``prior_state``; ``set_hash`` is the hash of the value the
        plan writes (read back out of the planned delta via the field's
        ``get``), or ``None`` when the plan REMOVES the field (ownership taken
        as a removal). A field the plan neither sets nor removes is skipped,
        so the journal never records a key we did not touch.
        """
        ...

    @abstractmethod
    def revert_decisions(
        self,
        journal_records: dict[str, Any],
        state: dict[FileTag, Any],
    ) -> tuple[dict[str, RevertDecision], dict[str, Any]]:
        """Compute per-field :class:`~zai_python_helper.ownership.RevertDecision`.

        For each key in :meth:`revert_key_set`, look up the field's CURRENT
        value in ``state`` and consult the journal. RESTORE / REFUSE / CLEAR
        follow :func:`zai_python_helper.ownership.revert`.

        Returns ``(decisions, retired_records)``: ``decisions`` is the per-key
        decision the caller acts on; ``retired_records`` is the journal with
        every ``RESTORE`` decision's record retired to ``active=False``
        (issue #48 cycle-state). The caller persists ``retired_records`` so a
        later re-activation does not resurrect a stale restore point.
        """
        ...

    # ------------------------------------------------------------------
    # Status / postcondition
    # ------------------------------------------------------------------

    @abstractmethod
    def postconditions(self, region: Region, *, state: dict[FileTag, Any]) -> bool:
        """True iff ``state`` reflects an active ``use zai`` for ``region``."""
        ...

    @abstractmethod
    def status_row(self, paths: Paths) -> StatusRow:
        """Read-only detect of this tool's state for the ``status`` report."""
        ...

    @abstractmethod
    def echo_lines(self, plan: PatchPlan, region: Region) -> list[str]:
        """Human-readable lines to print after ``use zai`` (secrets masked)."""
        ...


# Tag → Paths attribute. Single place that maps a semantic file tag to its
# resolved path attribute name on :class:`Paths`. Kept here (not in cli.py) so
# both the CLI and the tools layer share one mapping. A tag not in this map is
# a programming error (every FileTag a tool emits must resolve).
TAG_TO_PATH_ATTR: dict[FileTag, str] = {
    FileTag.SETTINGS: "claude_settings",
    FileTag.CLAUDE_JSON: "claude_json",
    FileTag.ZSHRC: "zshrc",
    FileTag.OPENCODE: "opencode",
    FileTag.CRUSH: "crush",
    FileTag.FACTORY_DROID: "factory_droid",
}


def resolve_path(paths: Paths, tag: FileTag):
    """Map a semantic :class:`FileTag` to its resolved :class:`pathlib.Path`."""
    return getattr(paths, TAG_TO_PATH_ATTR[tag])
