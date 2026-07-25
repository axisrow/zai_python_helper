#!/usr/bin/env python3
"""Auto-generate machine-readable docs from the source code (issue #19).

FastAPI principle: the code (docstrings + type hints) is the single source of
truth; documentation is a *derivative*. This script has zero hand-written API
content — every signature, docstring line, and enum member is read live from the
package via ``griffe`` and rendered to Markdown.

Two artifacts are produced in one pass:

* ``docs/api/zai_python_helper.md`` — the structured API reference. One file
  (not one-per-module) so an LLM can load the whole public surface in a single
  context window.
* ``llms.txt`` — the LLM entry file (https://llmstxt.org). Compact: what this
  is, how to install, a 30-second HEADLESS example, the public API as
  ``name -> signature -> one-line``, and links to the detailed reference.

Design constraints (load-bearing for CI):

* **Deterministic.** Output is sorted and uses ``\n`` line endings + a trailing
  newline, so ``make docs`` is idempotent and ``git diff --exit-code`` is stable.
* **No runtime import of the package.** griffe parses source, so generation does
  not execute ``zai_python_helper`` (no side effects, no HOME access).
* **Public surface only.** Everything documented is reached from
  ``zai_python_helper.__all__`` — the versioned contract from issue #18. Internal
  names are invisible, mirroring the importable API exactly.

Usage::

    python tools/gen_docs.py            # write docs/api/ + llms.txt
    python tools/gen_docs.py --check    # exit 1 if artifacts would change
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

import griffe

# --- Paths -------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
PACKAGE_NAME = "zai_python_helper"
DOCS_API_DIR = REPO_ROOT / "docs" / "api"
LLMS_TXT = REPO_ROOT / "llms.txt"
API_MD = DOCS_API_DIR / "zai_python_helper.md"

# Repo URL (keeps links absolute + stable regardless of where docs are served).
REPO_URL = "https://github.com/axisrow/zai_python_helper"
# Source links point under ``src/<package>/...``; ``SRC_DIR`` is the ``src/``
# root, so the link prefix stops there (the relative path adds ``<package>/``).
SRC_TREE = f"{REPO_URL}/blob/main/src"


# --- griffe loading ----------------------------------------------------------


def load_package() -> griffe.Module:
    """Parse the package source tree (no execution).

    griffe logs at WARNING about parameters that lack type annotations (e.g.
    ``stream=None`` in ``status.render_status``); those are not actionable here
    — the generator renders what it can — so logging is silenced to keep CI
    output clean. Real parse failures still raise.
    """
    import logging

    logging.getLogger("griffe").setLevel(logging.ERROR)
    return griffe.load(PACKAGE_NAME, search_paths=[str(SRC_DIR)])


def public_names(pkg: griffe.Module) -> list[str]:
    """The versioned public surface, read from ``__all__`` (issue #18)."""
    attr = pkg.attributes.get("__all__")
    if attr is None or attr.value is None:
        raise SystemExit("FATAL: zai_python_helper.__all__ not found / empty.")
    raw = attr.value
    # griffe may expose the list literal as an ExprList (elements are str-literal
    # nodes) or as a plain python list once resolved. Handle both.
    elements = getattr(raw, "elements", None)
    if elements is not None:
        names: list[str] = []
        for el in elements:
            s = str(el)
            if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
                s = s[1:-1]
            names.append(s)
        return names
    return [str(n) for n in raw]


def resolve_member(pkg: griffe.Module, name: str) -> griffe.Object | None:
    """Find a top-level public member by its ``__all__`` name.

    Public names are re-exported into the top-level module, so they live in
    ``pkg.members`` (classes / functions) or ``pkg.attributes`` (constants).

    Re-exports arrive as :class:`griffe.Alias`; we resolve to the final target
    (the real class/function/attribute) so signatures, docstrings, and source
    links point at the definition, not the re-export site.
    """
    member = pkg.members.get(name)
    if member is not None:
        # Alias -> dereference to the concrete object it re-exports.
        if isinstance(member, griffe.Alias):
            target = member.final_target
            return target if target is not None else member
        return member
    attr = pkg.attributes.get(name)
    return attr


# --- rendering helpers -------------------------------------------------------

# griffe kind strings -> human labels used in headings.
_KIND_LABEL = {
    "function": "function",
    "class": "class",
    "attribute": "constant",
}


def kind_label(obj: griffe.Object) -> str:
    return _KIND_LABEL.get(getattr(obj, "kind", "").value, "symbol")


def annot(annotation: object) -> str:
    """Render a type annotation to a clean string (``str``, ``Region``, …)."""
    if annotation is None:
        return ""
    return str(annotation).strip()


def _strip_quotes(s: str) -> str:
    """Strip a matching surrounding quote pair from a griffe literal string."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _rst_to_markdown(text: str) -> str:
    """Convert the common RST inline roles used in our docstrings to Markdown.

    Docstrings are written in Google/RST style (````literal````, ``:class:`X```).
    The rendered Markdown reads cleaner as plain inline code / bare names. This
    is deliberately minimal — only the constructs that appear in this codebase.
    """
    import re

    # ``literal``  ->  `literal`
    text = re.sub(r"``([^`]+)``", r"`\1`", text)
    # :class:`X` / :func:`x` / :data:`X` / :mod:`m` / :attr:`a`  ->  `X`
    text = re.sub(r":(class|func|data|mod|attr|meth|obj):`([^`]+)`", r"`\2`", text)
    return text


def first_doc_line(obj: griffe.Object | None) -> str:
    """The one-line summary used in ``llms.txt`` (first non-empty docstring line).

    Falls back to ``"(undocumented)"`` so the entry file never has a blank cell.
    """
    if obj is None or obj.docstring is None:
        return "(undocumented)"
    text = obj.docstring.value.strip()
    if not text:
        return "(undocumented)"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            # Strip RST inline markup for plain-text readability.
            return _rst_to_markdown(stripped).replace("``", "")
    return "(undocumented)"


def docstring_body(obj: griffe.Object | None) -> str:
    """Prose description for the detailed reference.

    Only the *description* portion of the parsed docstring is rendered. The
    structured sections (parameters, returns, attributes, raises, examples) are
    intentionally dropped because the generator rebuilds signatures and
    attributes from the type hints — the code, not the prose, is the source of
    truth (FastAPI-style). Rendering both would duplicate the ``Attributes:``
    block and drift from the actual annotations.
    """
    if obj is None or obj.docstring is None:
        return "*(no docstring)*"
    try:
        parsed = obj.docstring.parse("google")
    except Exception:  # noqa: BLE001 — fall back to raw text if parsing breaks.
        parsed = []
    text_parts: list[str] = []
    for section in parsed:
        # ``text`` carries the free-form description (summary + body). Any other
        # section kind is structured and rebuilt from the code instead.
        if section.kind.value == "text":
            value = _rst_to_markdown(str(section.value)).strip()
            if value:
                text_parts.append(value)
    if not text_parts:
        # No parsed text section (or no docstring at all): show the raw first
        # paragraph if present, else an explicit marker.
        raw = obj.docstring.value.strip()
        return raw or "*(no docstring)*"
    return "\n\n".join(text_parts)


def render_param(p: griffe.Parameter) -> str:
    """``name: annotation`` (``= default`` when a keyword default exists)."""
    ann = annot(p.annotation)
    sig = f"{p.name}: {ann}" if ann else p.name
    # Only show defaults for keyword/positional-or-keyword params that declare
    # one (skip ``*`` / ``**`` collectors and params without a default sentinel).
    default = getattr(p, "default", None)
    if default is not None and p.kind.value in {
        "keyword_only",
        "positional_or_keyword",
    }:
        sig += f" = {default}"
    return sig


def signature_str(func: griffe.Function) -> str:
    """Reconstruct a readable ``def name(params) -> return`` signature."""
    params: list[str] = []
    saw_kw_only = False
    saw_pos_only = False
    for p in func.parameters:
        kind = p.kind.value
        if kind == "positional_only" and not saw_pos_only:
            saw_pos_only = True
        if kind == "keyword_only" and not saw_kw_only:
            saw_kw_only = True
            params.append("*")
        params.append(render_param(p))
    ret = annot(func.returns)
    arrow = f" -> {ret}" if ret else ""
    return f"{func.name}({', '.join(params)}){arrow}"


def source_link(obj: griffe.Object) -> str:
    """GitHub link to the object's source file:line (best-effort)."""
    path = getattr(obj, "filepath", None)
    if path is None:
        return ""
    try:
        rel = Path(path).relative_to(SRC_DIR).as_posix()
    except ValueError:
        return ""
    line = getattr(obj, "lineno", 1) or 1
    return f"{SRC_TREE}/{rel}#L{line}"


def is_enum_class(cls: griffe.Class) -> bool:
    """Detect a class subclassing ``enum.Enum``.

    griffe exposes ``is_enum`` as a tri-state (``None`` until computed); the
    reliable signal here is a base named ``Enum``. Used so enums render their
    members instead of being labelled ``dataclass``.
    """
    flag = getattr(cls, "is_enum", None)
    if flag is True:
        return True
    for base in cls.bases or []:
        if str(base) in {"Enum", "enum.Enum", "IntEnum", "StrEnum"}:
            return True
    return False


# --- per-kind Markdown blocks (docs/api/*.md) --------------------------------


def render_function(func: griffe.Function) -> Iterable[str]:
    yield f"### `{func.name}()`"
    yield ""
    label = kind_label(func)
    link = source_link(func)
    src = f" — [source]({link})" if link else ""
    yield f"*{label}{src}*"
    yield ""
    yield f"```python\n{signature_str(func)}\n```"
    yield ""
    yield docstring_body(func)
    yield ""


def render_enum(cls: griffe.Class) -> Iterable[str]:
    yield f"### `{cls.name}`"
    yield ""
    link = source_link(cls)
    src = f" — [source]({link})" if link else ""
    yield f"*enum{src}*"
    yield ""
    yield docstring_body(cls)
    yield ""
    members = [m for m in cls.members.values() if not m.name.startswith("_")]
    if members:
        yield "Members:"
        yield ""
        for m in members:
            yield f"- `{m.name}` = `{m.value}`"
        yield ""


def render_dataclass_or_class(cls: griffe.Class) -> Iterable[str]:
    yield f"### `{cls.name}`"
    yield ""
    # Distinguish dataclasses (have annotated attributes with no callable value)
    # from plain classes for the reader.
    is_dc = bool(cls.attributes)
    kind = "dataclass" if is_dc else "class"
    link = source_link(cls)
    src = f" — [source]({link})" if link else ""
    yield f"*{kind}{src}*"
    yield ""
    yield docstring_body(cls)
    yield ""
    fields = [a for a in cls.attributes.values() if not a.name.startswith("_")]
    if fields:
        yield "Attributes:"
        yield ""
        for a in fields:
            ann = annot(a.annotation)
            ann_str = f": `{ann}`" if ann else ""
            yield f"- `{a.name}`{ann_str}"
        yield ""
    methods = [
        f
        for f in cls.functions.values()
        if not f.name.startswith("_") and f.is_function
    ]
    for m in methods:
        yield from render_method(m)
    yield ""


def render_method(func: griffe.Function) -> Iterable[str]:
    yield f"#### `{func.name}()`"
    yield ""
    yield f"```python\n{signature_str(func)}\n```"
    yield ""
    yield docstring_body(func)
    yield ""


def render_constant(attr: griffe.Attribute) -> Iterable[str]:
    yield f"### `{attr.name}`"
    yield ""
    link = source_link(attr)
    src = f" — [source]({link})" if link else ""
    ann = annot(attr.annotation)
    ann_str = f" (`{ann}`)" if ann else ""
    yield f"*constant{src}{ann_str}*"
    yield ""
    value = getattr(attr, "value", None)
    if value is not None:
        yield f"Value: `{_strip_quotes(str(value))}`"
        yield ""
    yield docstring_body(attr)
    yield ""


def render_member(name: str, obj: griffe.Object | None) -> Iterable[str]:
    if obj is None:
        yield f"### `{name}`"
        yield ""
        yield "*(could not resolve from source — definition may be missing)*"
        yield ""
        return
    # Defensive: a stray un-resolved Alias (target unreachable) — render its name.
    if isinstance(obj, griffe.Alias):
        target = obj.final_target
        obj = target if target is not None else obj
    if isinstance(obj, griffe.Function):
        yield from render_function(obj)
    elif isinstance(obj, griffe.Class):
        # Enums render their members; dataclasses/classes render attributes.
        if is_enum_class(obj):
            yield from render_enum(obj)
        else:
            yield from render_dataclass_or_class(obj)
    elif isinstance(obj, griffe.Attribute):
        yield from render_constant(obj)
    else:
        yield f"### `{name}`"
        yield ""
        yield docstring_body(obj)
        yield ""


# --- llms.txt ----------------------------------------------------------------

LLMS_HEADER = """\
# zai_python_helper

> MIT-licensed, clean-room Python helper that connects **Claude Code** to the
> **Z.ai GLM Coding Plan** by patching `~/.claude/settings.json`,
> `~/.claude.json`, and `~/.zshrc` — no background service, no binary. Designed
> **importable** (use the planning core as a library) and **headless** (every
> action is a CLI flag, no interactive menu).

## Install

```bash
pip install zai-python-helper
```

Or from source (editable, with docs tooling):

```bash
pip install -e ".[docs]"
```

## 30-second headless use

Importable pure-planner API — no side effects until you apply the plan via the
IO backends:

```python
from zai_python_helper import (
    ProviderSpec, ModelMode, Region, plan_zai,
    JsonBackend, ShellBackend, Paths, base_url_for_region,
)

spec = ProviderSpec(
    base_url=base_url_for_region(Region.GLOBAL),
    model_mode=ModelMode.ORIGINAL,
)
paths = Paths.default()
plan = plan_zai(
    spec,
    Region.GLOBAL,
    settings_doc=JsonBackend.read(paths.claude_settings),
    claude_json_doc=JsonBackend.read(paths.claude_json),
    zshrc_text=ShellBackend.read(paths.zshrc),
    auth_token="<your Z.ai auth token>",
)
# Apply plan.deltas via JsonBackend / ShellBackend (omitted for brevity).
```

Headless CLI (every option is a flag — scriptable, no prompts unless a token is
missing):

```bash
# Switch Claude Code to Z.ai (model selection mode, region, token, dry-run)
zai-python-helper use zai --mode original --region global --api-key "$ZAI_API_KEY"
zai-python-helper use zai --mode select --model glm-4-plus
zai-python-helper use zai --mode custom --model "my-model" --name "My Model"
zai-python-helper use zai --dry-run          # preview, write nothing

# Revert to default Anthropic config
zai-python-helper use default

# Read-only observability + diagnostics
zai-python-helper status
zai-python-helper doctor
zai-python-helper list --format json
```

## Public API

Auto-generated from `zai_python_helper.__all__` (the versioned contract, issue
#18). `name → signature → one-line`. Detailed reference:
[docs/api/zai_python_helper.md](docs/api/zai_python_helper.md).

"""

LLMS_FOOTER = f"""\
## Docs

- [docs/api/zai_python_helper.md](docs/api/zai_python_helper.md): full auto-generated API reference (signatures + docstrings).
- [ARCHITECTURE.md](docs/ARCHITECTURE.md): design, ADRs, and module layout.
- [README.md](README.md): human-oriented overview and CLI mode reference.
- [Source]({REPO_URL}/src/zai_python_helper): browse the implementation.

## Optional

- [llmstxt.org](https://llmstxt.org/): the `llms.txt` format this file follows.
- [issue #19]({REPO_URL}/issues/19): how these docs are generated and kept in sync.
"""


def compact_api_line(name: str, obj: griffe.Object | None) -> str:
    """``- `name`: signature — one-line doc``  (or class/enum summary)."""
    summary = first_doc_line(obj)
    if obj is None:
        return f"- `{name}`: {summary}"
    if isinstance(obj, griffe.Function):
        return f"- `{signature_str(obj)}`: {summary}"
    if isinstance(obj, griffe.Class):
        if is_enum_class(obj):
            label = "enum"
        elif obj.attributes:
            label = "dataclass"
        else:
            label = "class"
        return f"- `{name}` ({label}): {summary}"
    # Attribute / constant — show its literal value when available.
    value = getattr(obj, "value", None)
    if name == "__version__" and value is not None:
        return f"- `{name}` = `{_strip_quotes(str(value))}`"
    return f"- `{name}`: {summary}"


def render_llms_txt(pkg: griffe.Module, names: list[str]) -> str:
    lines: list[str] = [LLMS_HEADER, "```text"]
    lines.append(f"# name → signature → one-line   (from __all__, n={len(names)})")
    for name in names:
        obj = resolve_member(pkg, name)
        lines.append(compact_api_line(name, obj))
    lines.append("```")
    lines.append("")
    lines.append(LLMS_FOOTER)
    return "\n".join(lines)


# --- docs/api/zai_python_helper.md ------------------------------------------


def render_api_md(pkg: griffe.Module, names: list[str]) -> str:
    out: list[str] = []
    out.append("# `zai_python_helper` — API reference")
    out.append("")
    out.append(
        "<!-- AUTO-GENERATED by tools/gen_docs.py (issue #19). Do not edit by "
        "hand — regenerate with `make docs`. Source of truth = docstrings + "
        "type hints in src/zai_python_helper/. -->"
    )
    out.append("")
    out.append(
        "Machine-readable reference for the **importable public API** "
        "(`__all__`, issue #18). Every signature below is read live from the "
        "source — this file is never out of sync with the code."
    )
    out.append("")
    # Table of contents.
    out.append("## Contents")
    out.append("")
    for name in names:
        out.append(f"- [`{name}`](#{name.lower()})")
    out.append("")
    # One section per public name, in __all__ order.
    for name in names:
        obj = resolve_member(pkg, name)
        out.append("---")
        out.append("")
        out.append(f'<a id="{name.lower()}"></a>')
        out.append("")
        out.extend(render_member(name, obj))
    return "\n".join(out).rstrip() + "\n"


# --- write / check -----------------------------------------------------------


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def generate() -> None:
    pkg = load_package()
    names = public_names(pkg)
    api_md = render_api_md(pkg, names)
    llms = render_llms_txt(pkg, names)
    write_file(API_MD, api_md)
    write_file(LLMS_TXT, llms)


def check() -> int:
    """Exit 1 if any generated artifact differs from what's on disk."""
    pkg = load_package()
    names = public_names(pkg)
    expected = {
        API_MD: render_api_md(pkg, names),
        LLMS_TXT: render_llms_txt(pkg, names),
    }
    stale: list[str] = []
    for path, content in expected.items():
        actual = path.read_text(encoding="utf-8") if path.exists() else None
        if actual != content:
            stale.append(str(path.relative_to(REPO_ROOT)))
    if stale:
        print("::error::Docs are stale. Rebuild with `make docs` and commit:")
        for s in stale:
            print(f"  - {s}")
        return 1
    print("OK: docs/ and llms.txt are in sync with the code.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if generated docs would change (CI gate)",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check()
    generate()
    print(f"wrote {API_MD.relative_to(REPO_ROOT)}")
    print(f"wrote {LLMS_TXT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
