"""Owned marker-fenced block for shell rc files (PURE, ADR-003).

This module is PURE: it contains only string constants and pure text
transforms — no IO, no env, no file access. Both the planner
(:mod:`zai_python_helper.core.planner.claude_code`) and the IO layer
(:mod:`zai_python_helper.backends.ShellBackend`) import from here, so the
marker strings and the add/remove logic live in exactly one place and cannot
drift between core and io.

The block is a presence marker. Per ADR-003 we do NOT export ``ANTHROPIC_*``
inside it (``settings.json`` owns the env); the block exists so
``status``/``doctor`` can detect that we have managed this file and warn if
the user re-exports conflicting vars elsewhere. Installing it has no shell
side effect, which makes it fully reversible and incapable of clobbering a
user-written export.

The fence strings are intentionally distinctive (``>>> ... <<<``) so they do
not collide with other tools' markers and are grep-able.
"""

from __future__ import annotations

# The fence comment lines. These EXACT strings are the contract: presence of
# both, in order, delimits our managed region. Never change them without a
# migration (older installs would orphan their block).
MANAGED_BLOCK_BEGIN = "# >>> zai-python-helper managed >>>"
MANAGED_BLOCK_END = "# <<< zai-python-helper managed <<<"

# The block body — comments only (no exports). See module docstring.
MANAGED_BLOCK_BODY_LINES: tuple[str, ...] = (
    "# This block is managed by zai-python-helper — do not edit or move it.",
    "# Claude Code settings.json owns ANTHROPIC_*; remove any conflicting",
    "# `export ANTHROPIC_*` lines elsewhere or they will override settings.",
)


def managed_block_lines() -> list[str]:
    """Return the full owned block as a list of lines (no trailing newline).

    ``[BEGIN, body..., END]``. Pure list assembly.
    """
    return [MANAGED_BLOCK_BEGIN, *MANAGED_BLOCK_BODY_LINES, MANAGED_BLOCK_END]


def _find_block_range(text: str) -> tuple[int, int] | None:
    """Locate the managed block as a ``(begin_line_idx, end_line_idx)`` pair.

    Validates the block is **well-formed**: exactly one BEGIN line followed
    later by exactly one END line, in order, with nothing in between that
    looks like another fence. Returns ``None`` when:

    - no fences are present (the block was never installed);
    - the fences are out of order (END before BEGIN — a manual edit or merge
      conflict left the file malformed);
    - either fence is duplicated (ambiguous — refuse to guess which region
      is ours).

    Fail-closed: any ambiguity returns ``None`` so the caller treats the
    block as absent and never truncates foreign content. A malformed file is
    left untouched; ``status``/``doctor`` can surface the anomaly separately.
    """
    lines = text.split("\n")
    begin_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if line == MANAGED_BLOCK_BEGIN:
            if begin_idx is not None:
                return None  # duplicate BEGIN — ambiguous
            begin_idx = i
        elif line == MANAGED_BLOCK_END:
            if end_idx is not None:
                return None  # duplicate END — ambiguous
            end_idx = i
    if begin_idx is None or end_idx is None:
        return None  # missing one or both fences
    if end_idx <= begin_idx:
        return None  # END before BEGIN — malformed ordering
    return begin_idx, end_idx


def owns_owned_block(text: str) -> bool:
    """True iff ``text`` contains a **well-formed** managed block.

    Requires exactly one BEGIN followed by exactly one END, in order. A
    malformed fence (reordered, duplicated, or lone) returns False — we do
    not claim ownership of a file we cannot safely edit. A second ``use zai``
    short-circuits to a NOOP only when this returns True.
    """
    return _find_block_range(text) is not None


def install_owned_block(text: str) -> str:
    """Return ``text`` with our managed block appended (idempotent).

    Foreign lines are NEVER modified — the block is appended after the
    existing content. A single blank line separates the block from preceding
    content (only when the file is non-empty and not already blank-ended),
    and the result always ends with exactly one trailing newline so the file
    is well-formed whether it pre-existed or not.

    If a well-formed block is already present this is a no-op (returns
    ``text`` unchanged) so the planner's equality check naturally yields a
    NOOP delta. If ANY fence marker is present but the pair is MALFORMED
    (reordered/duplicated/lone), we refuse to install rather than add a
    second block on top of the corruption — the file is left untouched.
    """
    if MANAGED_BLOCK_BEGIN in text or MANAGED_BLOCK_END in text:
        # A marker is present: well-formed pair → no-op; malformed → refuse.
        return text

    block = "\n".join(managed_block_lines())
    if not text:
        # Empty / absent file → block is the whole file.
        return block + "\n"

    # Ensure exactly one blank line between existing content and the block.
    stripped = text.rstrip("\n")
    return stripped + "\n\n" + block + "\n"


def remove_owned_block(text: str) -> str:
    """Return ``text`` with our managed block removed (idempotent).

    Removes the fenced region INCLUSIVELY (begin fence, body, end fence) —
    ONLY the exact, well-formed range — and collapses the surrounding blank
    lines so no dangling whitespace is left. Foreign lines are NEVER touched.

    Fail-closed: if the fences are absent or malformed (reordered/duplicated),
    this is a no-op (returns ``text`` unchanged). It never deletes content
    outside the validated begin→end range, so a manually-corrupted fence
    cannot truncate the user's file.
    """
    rng = _find_block_range(text)
    if rng is None:
        return text
    begin_idx, end_idx = rng

    lines = text.split("\n")
    # Drop the inclusive [begin_idx, end_idx] range only.
    out = lines[:begin_idx] + lines[end_idx + 1 :]

    # Collapse runs of 2+ consecutive blank lines (the removal can leave a
    # dangling blank pair at the splice point) down to a single blank line.
    collapsed: list[str] = []
    prev_blank = False
    for line in out:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = is_blank
    # Trim leading blank lines (file should not start with a blank line).
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    # Trim trailing blank lines so a round-trip restores the original file's
    # single trailing newline (install added a blank separator before the
    # block; after removal we must not leave it dangling).
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    # Ensure exactly one trailing newline, or empty if nothing remains.
    if not collapsed:
        return ""
    return "\n".join(collapsed) + "\n"
