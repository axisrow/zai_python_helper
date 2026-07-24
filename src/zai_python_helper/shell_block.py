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


def owns_owned_block(text: str) -> bool:
    """True iff ``text`` already contains our managed block (begin AND end).

    Pure substring check. We do not require the lines to be contiguous here —
    ``install_owned_block`` is the authority on shape; this predicate only
    answers "is the block present?". A second ``use zai`` short-circuits to a
    NOOP when this returns True.
    """
    return MANAGED_BLOCK_BEGIN in text and MANAGED_BLOCK_END in text


def install_owned_block(text: str) -> str:
    """Return ``text`` with our managed block appended (idempotent).

    Foreign lines are NEVER modified — the block is appended after the
    existing content. A single blank line separates the block from preceding
    content (only when the file is non-empty and not already blank-ended),
    and the result always ends with exactly one trailing newline so the file
    is well-formed whether it pre-existed or not.

    If the block is already present this is a no-op (returns ``text``
    unchanged) so the planner's equality check naturally yields a NOOP delta.
    """
    if owns_owned_block(text):
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

    Removes the fenced region INCLUSIVELY (begin fence, body, end fence) and
    collapses the surrounding blank lines so no dangling whitespace is left.
    Foreign lines are NEVER touched. If the block is absent this is a no-op.
    """
    if not owns_owned_block(text):
        return text

    lines = text.split("\n")
    out: list[str] = []
    skipping = False
    for line in lines:
        if line == MANAGED_BLOCK_BEGIN:
            skipping = True
            continue
        if skipping and line == MANAGED_BLOCK_END:
            skipping = False
            continue
        if skipping:
            continue
        out.append(line)

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
