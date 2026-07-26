#!/usr/bin/env python3
"""Issue #20 — human-docs i18n structure-sync gate.

The mkdocs site builds one tree per language (``docs/{en,ru,zh}/``). English is
the source of truth; ru/zh are derivatives. This gate enforces that the
**set of ``.md`` files** is identical across the three language folders, so the
language selector never leads to a missing page.

What is checked
---------------
* Every ``.md`` present under ``docs/en/`` must also exist at the same
  relative path under ``docs/ru/`` and ``docs/zh/``.
* No extra ``.md`` may exist in ``ru/`` or ``zh/`` that is absent from ``en/``.

What is NOT checked
-------------------
This is intentionally a *structural* gate, not a content gate. It does not
compare H2 headings or translation completeness — translations are allowed to
diverge in wording (and ``ru``/``zh`` are at different review stages). Keeping
the file set in lockstep is the load-bearing invariant; anything finer would
block legitimate translation work. Per-issue design choice.

Usage::

    python tools/check_human_docs_structure.py      # exit 0 = in sync
    python tools/check_human_docs_structure.py --root /path/to/repo

Exits non-zero with a diff on the first desynchronized language.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# English is the source of truth; these are the derivative languages that must
# mirror its file set.
DEFAULT_LANGUAGES = ("en", "ru", "zh")
DOCS_DIR = Path("docs")


def md_relpaths(lang_dir: Path) -> set[str]:
    """Return the set of ``.md`` paths under ``lang_dir`` relative to it."""
    return {
        str(p.relative_to(lang_dir)).replace("\\", "/")
        for p in sorted(lang_dir.rglob("*.md"))
    }


def check_one(root: Path, source: str, target: str) -> list[str]:
    """Diff the ``.md`` file set of ``target`` against ``source``.

    :return: a list of human-readable problem lines (empty = in sync).
    """
    src_dir = root / DOCS_DIR / source
    tgt_dir = root / DOCS_DIR / target
    problems: list[str] = []

    if not src_dir.is_dir():
        problems.append(f"source language folder missing: {src_dir}")
        return problems
    if not tgt_dir.is_dir():
        problems.append(f"language folder missing: {tgt_dir}")
        return problems

    src = md_relpaths(src_dir)
    tgt = md_relpaths(tgt_dir)

    missing = sorted(src - tgt)
    extra = sorted(tgt - src)
    for rel in missing:
        problems.append(
            f"{target}/ is missing {rel} (exists in {source}/)"
        )
    for rel in extra:
        problems.append(
            f"{target}/ has extra {rel} (absent from {source}/)"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: parent of this script)",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_LANGUAGES[0],
        help=f"source-of-truth language (default: {DEFAULT_LANGUAGES[0]})",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=list(DEFAULT_LANGUAGES[1:]),
        help=(
            f"languages that must mirror the source "
            f"(default: {' '.join(DEFAULT_LANGUAGES[1:])})"
        ),
    )
    args = parser.parse_args(argv)

    all_problems: list[str] = []
    for target in args.targets:
        problems = check_one(args.root, args.source, target)
        if problems:
            all_problems.append(f"== {target}/ vs {args.source}/ ==")
            all_problems.extend(f"  - {p}" for p in problems)

    if all_problems:
        print(
            "::error::Human-docs i18n structure is out of sync. "
            "ru/ and zh/ must contain the same set of .md files as en/.\n"
            + "\n".join(all_problems),
            file=sys.stderr,
        )
        return 1

    print(
        "Human-docs i18n structure in sync: "
        + ", ".join(f"{t}/ == {args.source}/" for t in args.targets)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
