"""Argparse parser builder + handlers for the ``zai-python-helper`` CLI.

The dispatch contract: each subcommand registers a handler via
``set_defaults(func=...)``, and :func:`zai_python_helper.__main__.main` calls
``args.func(args)``. Handlers are THIN SHELLS — resolve ``Paths.default()``,
look up the target :class:`~zai_python_helper.tools.base.Tool` in
:data:`~zai_python_helper.tools.REGISTRY`, and delegate the read → plan →
ownership-capture → commit cycle to it. They do NOT catch/print/exit: a
:class:`ZaiPythonHelperError` propagates to :func:`main`, which formats it as
one-line stderr + exit 1 (full traceback under ``--debug``).

The CLI is **tool-agnostic**: it never branches on the tool name. Every
tool-specific concern (which files, which ownership keys, how to echo) lives
on the Tool. Adding a tool is "implement ``Tool`` + register" — no CLI edit.

Root flags (``--debug`` / ``--dry-run``) attach via a single shared parent
parser so they parse BOTH before and after the subcommand (dual-parser
pattern).
"""

import argparse
import difflib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zai_python_helper.core.planner import DeltaKind, FileTag, PatchPlan
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.tools.base import resolve_path

if TYPE_CHECKING:
    from zai_python_helper.mcp import Tool

# Keys whose values are secrets and must be redacted in any diff/echo output
# (ADR: secrets never logged). Used by ``--dry-run`` diff rendering. This is a
# conservative allowlist-by-name: a key is secret if it is one of the explicit
# names below OR matches a credential-ish suffix/pattern. We redact defensively
# so a foreign key we don't know about (OPENAI_API_KEY, cloud tokens, etc.)
# never leaks through a dry-run diff. (Per-tool echo redaction lives on the
# Tool itself; this covers the generic diff path.)
_SECRET_ENV_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
_SECRET_NAME_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD")
_SECRET_NAME_SUBSTRINGS = ("SECRET", "PASSWORD", "CREDENTIAL", "TOKEN", "API_KEY")
# Canonical credential key names that tools write in non-UPPER_CASE forms the
# suffix/substring heuristics miss. Normalized (separator-stripped, upper) so
# ``apiKey``/``api_key``/``apikey``/``API-KEY`` all match ``APIKEY``. Without
# this, a ``--dry-run`` diff of an OpenCode/Crush/Factory Droid config leaked
# the credential verbatim (camelCase ``apiKey`` matched neither ``_KEY`` nor
# ``API_KEY``). See cycle-review finding (dry-run secret leak).
_SECRET_NAME_NORMALIZED = ("APIKEY", "AUTHTOKEN", "ACCESSTOKEN")

# FAIL-CLOSED JSON allowlist (issue #44). A regex/secret-denylist redactor is
# provably incomplete: a credential field whose name we did NOT enumerate
# (``privateKey``, ``Authorization``, ``clientSecret``, a future tool's token)
# leaks verbatim through ``--dry-run``. The inverse layer below closes that: a
# scalar value is shown ONLY if its key is on this explicit SAFE allowlist
# (known non-secret config we write); every OTHER scalar is replaced with
# ``<redacted>``. Structure (dict keys, list shape) is always preserved, so the
# diff stays useful (you see WHICH field changed) while an unknown value can
# never reach stdout.
#
# The allowlist is keyed by the field NAMES the tools write (case-insensitive),
# regardless of nesting depth — the same structural recurse walks every level.
# A key is added here only when we are CERTAIN its value is never a credential.
# When in doubt, leave it OUT: over-redaction degrades the diff, an unlisted
# secret leaks it — we accept the former. The denylist (:func:`_is_secret_key`)
# stays as a redundant fast path so an allowlist gap never re-exposes a key we
# already KNOW is secret.
_SAFE_JSON_KEY_NAMES: frozenset[str] = frozenset(
    {
        # settings.json env (Claude Code): non-secret managed vars. AUTH_TOKEN
        # / API_KEY are secret (denylist) and intentionally absent here.
        "ANTHROPIC_BASE_URL",
        "API_TIMEOUT_MS",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES",
        # .claude.json: the only key we manage.
        "hasCompletedOnboarding",
        # crush.json provider entry shape (api_key is secret — denylist).
        "id",
        "name",
        "base_url",
        # MCP entries: the field names the per-tool managers write. ``env`` /
        # ``headers`` / ``environment`` are CONTAINERS — recursed, and their
        # scalar children fall under the same allowlist (so Z_AI_API_KEY /
        # Authorization, NOT listed, are redacted).
        "type",
        "command",
        "args",
        "urlTemplate",
        "url",
        "protocol",
        "requiresAuth",
        "envTemplate",
        "local",
        # Container keys — present so the STRUCTURE is walked/preserved; their
        # scalar children are still gated by the allowlist (fail-closed).
        "env",
        "headers",
        "environment",
        "providers",
        "mcpServers",
        "mcp",
        "options",
        # Known non-secret MCP env vars (the region selector; the API key is
        # secret and NOT listed here, so it is redacted by its own name).
        "Z_AI_MODE",
        # Generic structural markers tools nest under.
        "model",
        "displayName",
        "provider",
    }
)

_RESTART_NOTICE = "restart recommended for deterministic switching"


def _normalize_secret_key(key: str) -> str:
    """Normalize a key for credential-name matching: upper, separators removed.

    ``apiKey`` → ``APIKEY``, ``api-key`` → ``APIKEY``, ``api_key`` → ``APIKEY``.
    So the camelCase/kebab/snake variants a tool config uses collapse to one
    canonical form the substring/suffix checks run against.
    """
    return key.upper().replace("_", "").replace("-", "")


def _is_secret_key(key: str) -> bool:
    """Heuristic: is ``key`` likely a credential that must be redacted?

    Conservative — errs on the side of redacting. Matches the explicit
    managed-secret names plus credential-ish suffixes/substrings so foreign
    secrets (OPENAI_API_KEY, cloud tokens) are caught even though we don't
    enumerate them. Also matches canonical credential names in any case/separator
    form (``apiKey``/``api_key``/``apikey`` → ``APIKEY``) so the camelCase keys
    tools write are redacted, not leaked. Used by the generic ``--dry-run`` diff
    renderer.
    """
    upper = key.upper()
    if key in _SECRET_ENV_KEYS:
        return True
    if any(upper.endswith(suf) for suf in _SECRET_NAME_SUFFIXES):
        return True
    if any(sub in upper for sub in _SECRET_NAME_SUBSTRINGS):
        return True
    normalized = _normalize_secret_key(key)
    return any(name in normalized for name in _SECRET_NAME_NORMALIZED)


def _is_safe_json_key(key: str) -> bool:
    """True iff a JSON scalar directly under ``key`` is known non-secret (issue #44).

    FAIL-CLOSED: defaults to False for any key we did not explicitly allowlist.
    Membership is matched case-insensitively against
    :data:`_SAFE_JSON_KEY_NAMES` so a tool's casing variant (``Url`` vs ``url``)
    still resolves. Used by :func:`_redact_json_doc` to gate a DIRECT scalar
    child's visibility.

    Note: this does NOT vouch for bare-scalar LIST elements under the key —
    see :func:`_is_safe_scalar_list_parent`. Container keys (``env``,
    ``headers``, …) are safe to RECURSE but a bare scalar under them is still
    redacted.
    """
    return key.lower() in {k.lower() for k in _SAFE_JSON_KEY_NAMES}


# Keys whose value is a CONTAINER we recurse into, but whose bare-scalar list
# children must NOT inherit "safe" (issue #44 review). ``env``/``headers`` hold
# named-keyed children (a dict) — a bare-scalar list under them is unclassified
# input and is redacted fail-closed. Contrast ``args`` (a list of command
# tokens) which IS safe to show element-for-element. Listed case-insensitively.
_CONTAINER_JSON_KEYS: frozenset[str] = frozenset(
    {"env", "headers", "environment", "providers", "mcpServers", "mcp", "options"}
)


def _is_safe_scalar_list_parent(key: str | None) -> bool:
    """True iff a bare-scalar LIST element under ``key`` is safe to show.

    ``args`` (a list of command tokens) is safe element-for-element. A
    CONTAINER key (``env``, ``headers``, …) is NOT — its children are meant to
    be named-keyed dicts, so a bare-scalar list under it is unclassified input
    and is redacted fail-closed. This closes the gap where
    ``_redact_json_doc({'env': ['sk-SENTINEL']})`` leaked because ``env`` is in
    the safe-allowlist: container keys are safe to recurse but not to vouch for
    a bare-scalar child.
    """
    if key is None:
        return False
    if key.lower() in {k.lower() for k in _CONTAINER_JSON_KEYS}:
        return False
    return _is_safe_json_key(key)


def _redact_json_doc(doc: Any, *, parent_key: str | None = None) -> Any:
    """Return a copy of parsed JSON ``doc`` safe for a ``--dry-run`` diff.

    FAIL-CLOSED (issue #44). Two layers, applied structurally (dict/list
    recurse so structure is always preserved):

    1. Denylist fast path: a value under a key :func:`_is_secret_key` flags is
       replaced with ``"<redacted>"`` outright — redundant defense so an
       allowlist gap never re-exposes a key we already KNOW is a credential
       (``ANTHROPIC_AUTH_TOKEN``, ``apiKey``, …).
    2. Allowlist gate (the inverse layer): any OTHER scalar value is shown only
       if its key is :func:`_is_safe_json_key`; otherwise it is also
       ``"<redacted>"``. This is what closes the leak a finite denylist cannot:
       a credential field whose name we never enumerated (``privateKey``,
       ``Authorization``, ``clientSecret``) is unclassified → redacted.

    Containers are always recursed so their KEYS and shape are shown (the diff
    stays useful: you see WHICH field changed). The classification rules:

    - A nested DICT classifies each child by ITS OWN key (``headers`` →
      ``Authorization`` is secret by its own name, not by ``headers``). So a
      safe container can still hide a secret child.
    - A LIST of DICTS recurses each element (its children classify by key).
    - A LIST of SCALARS classifies each element by the parent key, but ONLY via
      :func:`_is_safe_scalar_list_parent`: ``args`` (command tokens) shows its
      elements; a CONTAINER parent (``env``, ``headers``) does NOT vouch for a
      bare-scalar child, so ``{"env": ["sk-…"]}`` redacts each element
      fail-closed (a bare list under a container is unclassified input).

    ``parent_key`` is the key this value (or its enclosing list) sits under; it
    is None only at the document root (the initial call). Applied to the parsed
    doc BEFORE it is rendered into a ``--dry-run`` diff, so the diff text
    itself never carries a secret.
    """
    if isinstance(doc, dict):
        out: dict[str, Any] = {}
        for k, v in doc.items():
            out[k] = _redact_json_doc(v, parent_key=k)
        return out
    if isinstance(doc, list):
        return [_redact_json_doc(item, parent_key=parent_key) for item in doc]
    # A scalar value. Decide visibility from the enclosing key:
    # - secret by name             → always redacted (denylist fast path).
    # - safe by name (direct child) → shown.
    # - bare scalar list element    → shown only under a safe SCALAR-LIST parent
    #   (args); a container parent (env/headers) does not vouch for it.
    # - anything else              → redacted (fail-closed).
    if parent_key is not None:
        if _is_secret_key(parent_key):
            return "<redacted>"
        # A scalar reached here either as a direct dict child (parent_key is
        # its own name) or as a list element (parent_key is the list's name).
        # _is_safe_json_key covers the direct-child case; the list-element
        # case additionally excludes container parents via the combined check.
        if _is_safe_scalar_list_parent(parent_key):
            return doc
        return "<redacted>"
    # No enclosing key (document-root scalar): a value with no key carries no
    # hint it is non-secret, so fail-closed. (No managed config is a bare
    # scalar at root.)
    return "<redacted>"


def _redact_shell_text(text: str) -> str:
    """Redact secret values in shell text (``.zshrc``) for safe diffing.

    Handles ``[export ]KEY="value"`` (double-quoted), ``'value'`` (single-
    quoted), and bare ``value`` assignments; the key class accepts hyphens
    (``api-key=...``). A key is "secret" by :func:`_is_secret_key`. Applied to
    the shell source BEFORE it is rendered into a ``--dry-run`` diff.

    FAIL-CLOSED: any line that assigns a secret key has its ENTIRE RHS
    (everything after ``=`` to end-of-line) replaced with ``<redacted>``,
    regardless of quoting. Trying to match only the quoted/bare value segment
    with a regex cannot cover every zsh quoting form (escaped double quotes
    ``"a-\\"b"``, ANSI-C ``$'...'``, concatenation, trailing ``#`` comments)
    and leaked suffixes. Replacing the whole RHS is conservative (it may also
    redact a trailing comment on a secret line) but cannot leak a credential.

    The leading declaration builtin is matched broadly: plain ``export``;
    ``export -g`` (set a global from a function); and ``typeset``/``declare``
    with a ``g`` (global) flag, e.g. ``typeset -gx`` (the canonical oh-my-zsh /
    prezto global-export form). These are exactly the forms an ``ANTHROPIC_*``
    override in a user's ``.zshrc`` realistically takes.
    """
    # Match an assignment line: a declaration prefix (export / export -g /
    # typeset|declare with a `g` flag), a key, `=`, then the rest of the line
    # (the RHS we will redact wholesale for secret keys).
    _shell_pat = re.compile(
        r'(?m)^(\s*(?:export(?:\s+-\w*)?|(?:typeset|declare)\s+-\w*g\w*)\s+)?'
        r'([A-Za-z_][A-Za-z0-9_-]*)=(.*)$'
    )

    def _replace_shell(match: re.Match[str]) -> str:
        prefix = match.group(1) or ""
        key = match.group(2)
        if _is_secret_key(key):
            return f"{prefix}{key}=<redacted>"
        # FAIL-CLOSED for multiple assignments on one line: the regex matched
        # only the FIRST ``KEY=VALUE``. If the first key is non-secret but a
        # LATER ``IDENTIFIER=`` token in the RHS names a secret key (e.g.
        # ``export ANTHROPIC_BASE_URL=… ANTHROPIC_API_KEY=sk-…``), redact the
        # whole line — otherwise the second assignment's credential leaks via
        # the unchanged line. Whitespace-separated multi-assignment is valid
        # POSIX/zsh and a realistic form for an ``ANTHROPIC_*`` override.
        _later_key = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z_][A-Za-z0-9_-]*)=")
        if any(_is_secret_key(k) for k in _later_key.findall(match.group(3))):
            return f"{prefix}{key}=<redacted>"
        return match.group(0)

    return _shell_pat.sub(_replace_shell, text)


def _redact_text(text: str) -> str:
    """Redact secret values in rendered text for safe diffing.

    Dispatcher used when only the rendered text is available (not the parsed
    source): structural JSON redaction (fail-closed allowlist — see
    :func:`_redact_json_doc`) when the text parses as JSON, otherwise
    shell-assignment redaction. The ``--dry-run`` path redacts at the SOURCE
    inside :func:`_apply_plan` (JSON via :func:`_redact_json_doc`, shell via
    :func:`_shell_managed_preview`) BEFORE the diff is rendered; this function
    is only the fallback for stray text (e.g. the MCP ``--dry-run`` echo).
    :func:`_print_diff` itself does no redaction — it prints already-redacted
    strings verbatim. A secret value never reaches stdout/stderr.
    """
    import json

    stripped = text.lstrip()
    if stripped and stripped[0] in "{[":
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        else:
            return json.dumps(_redact_json_doc(doc), indent=2, ensure_ascii=False) + "\n"
    return _redact_shell_text(text)


def _shell_managed_preview(text: str) -> str:
    """Return a LEAK-SAFE preview of shell rc text for ``--dry-run`` (issue #44).

    FAIL-CLOSED for shell: print ONLY the managed marker-fenced block we own,
    never the user's foreign ``.zshrc`` content. Foreign lines are already
    never modified (the block transforms only add/remove the fenced region), so
    printing them serves no diff purpose — and printing them is the leak
    surface. zsh lets you quote the assignment WORD itself
    (``export $'OPENAI_API_KEY=SENTINEL'``, ``export "KEY=SENTINEL'``), which a
    line-anchored regex cannot reliably detect; the only provably-safe choice is
    to not echo foreign content at all.

    Foreign content is summarized (a ``<N foreign line(s) redacted>`` note) so
    the user knows their content exists without any of it reaching stdout.

    The managed block itself is ALSO run through :func:`_redact_shell_text`
    before printing (defense-in-depth). The block body is comment-only by
    construction, but :func:`~zai_python_helper.shell_block._find_block_range`
    validates only fence ordering/uniqueness — NOT body content — so a line
    injected BETWEEN the fences (manual edit, merge, another tool) still yields
    a valid range. Redacting the slice guarantees an injected ``export
    KEY=secret`` can never leak via the preview, preserving the defense #43
    established for the whole-file path.

    An absent/malformed block (no well-formed fence pair) yields an empty
    managed section; the foreign summary still reflects the surrounding lines.
    """
    from zai_python_helper.shell_block import _find_block_range

    lines = text.split("\n")
    rng = _find_block_range(text)
    if rng is None:
        # No well-formed block (absent, or malformed fence). The whole file is
        # "foreign" from our perspective — summarize, print nothing of it.
        foreign_count = sum(1 for ln in lines if ln != "")
        return _foreign_summary(foreign_count, managed="")

    begin_idx, end_idx = rng
    managed_lines = lines[begin_idx : end_idx + 1]
    # Foreign = every non-blank line OUTSIDE the managed region. Computed as
    # (total non-blank) - (non-blank inside the block) so there is no O(n*m)
    # membership test inside the loop.
    total_nonblank = sum(1 for ln in lines if ln != "")
    managed_nonblank = sum(1 for ln in managed_lines if ln != "")
    foreign_count = total_nonblank - managed_nonblank
    # DEFENSE-IN-DEPTH: redact the managed slice too. The block body is
    # comment-only by construction, but _find_block_range does not validate
    # body content — an export injected between the fences still parses as a
    # valid block. Running the shell redactor over the slice closes that hole
    # (the whole-file redaction #43 added is preserved here for the slice).
    managed = _redact_shell_text("\n".join(managed_lines))
    if managed and not managed.endswith("\n"):
        managed += "\n"
    return _foreign_summary(foreign_count, managed=managed)


def _foreign_summary(count: int, *, managed: str) -> str:
    """Build the redacted shell preview: foreign summary line(s) + managed block.

    The foreign count note is emitted on its own line (comment-styled so it is
    obviously not shell) ABOVE any managed block, mirroring where the user's
    content usually sits. A zero foreign count emits no summary (nothing
    hidden). The managed block (when present) follows verbatim — it is
    comment-only and safe.
    """
    parts: list[str] = []
    if count > 0:
        # Deliberately NOT the user's content — just a count, comment-styled.
        parts.append(
            f"# ({count} foreign line{'s' if count != 1 else ''} hidden — "
            "not managed, not shown to avoid leaking secrets)\n"
        )
    if managed:
        parts.append(managed)
    return "".join(parts)


def _apply_plan(paths: Paths, plan: PatchPlan, *, dry_run: bool) -> list[FileTag]:
    """Apply (or preview) a plan's deltas. Returns the list of written tags.

    For each non-NOOP delta, in dry-run mode it computes a ``unified_diff``
    between current and desired content (secrets redacted) and prints it; in
    real mode it writes the file via the matching backend. NOOP deltas are
    skipped silently in both modes.

    Returns the tags that were actually written (empty under dry-run, since
    dry-run writes nothing — callers use the return only in real mode).
    """
    from zai_python_helper.backends import JsonBackend, ShellBackend

    written: list[FileTag] = []
    for delta in plan.deltas:
        if delta.kind == DeltaKind.NOOP:
            continue
        path = resolve_path(paths, delta.tag)

        if dry_run:
            # Redact at the SOURCE so the rendered diff never carries a secret.
            # JSON: fail-closed structural redaction on the parsed doc (an
            # unclassified scalar is never shown — see :func:`_redact_json_doc`).
            # Shell: print ONLY the managed block, summarize foreign lines
            # (issue #44 — zsh assignment-word quoting can defeat any regex).
            if delta.kind == DeltaKind.WRITE_JSON:
                cur_doc = JsonBackend.read(path)
                cur_text = (
                    JsonBackend.render(
                        _redact_json_doc(cur_doc),
                        indent=JsonBackend._indent_for_tag(delta.tag),
                    )
                    if cur_doc is not None
                    else ""
                )
                des_text = JsonBackend.render(
                    _redact_json_doc(delta.content),
                    indent=JsonBackend._indent_for_tag(delta.tag),
                )
            else:  # WRITE_TEXT
                cur_text = _shell_managed_preview(ShellBackend.read(path))
                des_text = _shell_managed_preview(delta.content)
            _print_diff(path, cur_text, des_text, delta.tag)
            continue

        if delta.kind == DeltaKind.WRITE_JSON:
            JsonBackend.write(
                path,
                delta.content,
                indent=JsonBackend._indent_for_tag(delta.tag),
            )
        else:
            from zai_python_helper.backends import atomic_write_bytes

            atomic_write_bytes(path, delta.content.encode("utf-8"))
        written.append(delta.tag)
    return written


def _print_diff(path: Path, current: str, desired: str, tag: FileTag) -> None:
    """Print a unified_diff for one file under ``--dry-run``.

    ``current`` and ``desired`` are ALREADY redacted at the source by the
    caller — JSON via fail-closed structural redaction
    (:func:`_redact_json_doc`), shell via managed-block-only preview
    (:func:`_shell_managed_preview`) — so the diff text is safe to print
    verbatim. No post-hoc redaction is applied (or needed).
    """
    label = f"{path} ({tag.value})"
    cur_lines = current.splitlines(keepends=True)
    des_lines = desired.splitlines(keepends=True)
    diff = difflib.unified_diff(
        cur_lines,
        des_lines,
        fromfile=f"{label} (current)",
        tofile=f"{label} (desired)",
    )
    diff_text = "".join(diff)
    if not diff_text:
        # No textual diff — nothing would change for this file.
        return
    print(diff_text, end="")
    # JSON rendering intentionally has no final newline to match upstream
    # bytes. Keep consecutive dry-run diff records readable regardless of
    # whether the rendered input ends with a newline.
    if not diff_text.endswith("\n"):
        print()


def _run_recovery(paths: Paths, recover_fn) -> None:
    """Roll forward any interrupted prior activation before a new run.

    If a recovery manifest survives (a prior ``use`` was hard-killed mid-way),
    replay it to completion and report what was recovered. Best-effort and
    silent when there is nothing to recover.
    """
    from zai_python_helper.patchplan import has_pending_recovery

    if not has_pending_recovery(paths):
        return
    applied = recover_fn(paths)
    if applied:
        print(
            "warning: recovered from an interrupted prior run "
            f"(re-applied: {', '.join(applied)})"
        )


def _merge_takeover_records(tool, current: dict, records: list) -> dict:
    """Fold per-field takeover records into the journal dict (pure take_over).

    Applies :func:`~zai_python_helper.ownership.take_over` for each record over
    the current journal, keyed by ``tool.name``, returning the merged journal
    to persist.
    """
    from zai_python_helper.ownership import take_over

    merged = current
    for key, prior_value, prior_present, set_hash in records:
        merged = take_over(
            merged,
            tool.name,
            key,
            prior_value,
            prior_present,
            set_hash,
        )
    return merged


def _print_refuse_warnings(decisions: dict) -> None:
    """Surface REFUSE decisions as warnings (the user must see what we skipped)."""
    for d in decisions.values():
        if getattr(d, "action", None) and d.action.name == "REFUSE":
            print(f"  warning: {d.reason}")


def _resolve_tool(args: argparse.Namespace):
    """Look up the target Tool from ``--tool`` (default: claude_code)."""
    from zai_python_helper.tools import get_tool

    return get_tool(getattr(args, "tool", "claude_code"))


def _build_provider_spec(args: argparse.Namespace, mode, region: Region):
    """Build a ProviderSpec from CLI args. Carries the model mode + selection.

    Only meaningful for tools that consume model-mode (Claude Code). Other
    tools ignore the spec fields they don't use; the model-mode flags are
    accepted on the parser for a uniform CLI surface but are inert for them.
    The ``base_url`` stored on the spec is the region's Z.ai URL.
    """
    from zai_python_helper.core.domain import ProviderSpec
    from zai_python_helper.core.planner.claude_code import base_url_for_region

    return ProviderSpec(
        base_url=base_url_for_region(region),
        model_mode=mode,
        selected_model=getattr(args, "model", None) if mode.value == "select" else None,
        custom_model_id=getattr(args, "model", None) if mode.value == "custom" else None,
        custom_model_name=getattr(args, "name", None),
        custom_model_description=getattr(args, "description", None),
        custom_capabilities=getattr(args, "capabilities", None),
    )


def _resolve_mode_or_raise(args: argparse.Namespace):
    """Parse ``--mode`` and reject custom-only flags outside custom mode.

    Returns the resolved :class:`ModelMode`. The model-mode flags are a
    Claude-Code concern, but validating them here keeps the CLI surface
    consistent across tools (a typo is caught regardless of tool).
    """
    from zai_python_helper.core.domain import ModelMode
    from zai_python_helper.errors import ValidationError

    mode = ModelMode(getattr(args, "mode", ModelMode.ORIGINAL.value))
    custom_only = {
        "--name": getattr(args, "name", None),
        "--description": getattr(args, "description", None),
        "--capabilities": getattr(args, "capabilities", None),
    }
    if mode != ModelMode.CUSTOM:
        used = [flag for flag, value in custom_only.items() if value]
        if used:
            raise ValidationError(f"{', '.join(used)} only apply to --mode custom")
    return mode


def _handle_list(args: argparse.Namespace) -> int:
    """List available Z.ai model presets."""
    from zai_python_helper.constants import (
        ZAI_MODEL_PRESETS,
        get_preset_model,
        list_available_presets,
    )

    fmt = getattr(args, "format", "table")
    if fmt == "json":
        import json

        print(json.dumps(ZAI_MODEL_PRESETS, indent=2))
        return 0

    print("Available Z.ai model presets:")
    print()
    for preset in list_available_presets():
        config = get_preset_model(preset)
        if config is None:
            print(f"  {preset}: (error: preset not found)")
            continue
        print(f"  {preset}:")
        print(f"    ID: {config['model_id']}")
        print(f"    Name: {config['name']}")
        print(f"    Description: {config['description']}")
        print(f"    Maps to: {config['anthropic_alias']}")
        print()
    return 0


def _warn_self_heal_destruction(
    prior_doc: dict | None,
    journal_records: dict,
    plan: PatchPlan,
) -> None:
    """Warn when a self-heal is about to irreversibly drop a non-attributed
    regional provider entry (issue #61 follow-up).

    If the prior doc carried both regional providers and ``plan_zai`` did
    NOT refuse, a self-heal just replaced the user's non-attributed regional
    entry.  The deletion is irreversible — the journal records only OUR
    apiKey, not the user's entry — so this must be surfaced in BOTH the
    dry-run preview and the real activation, not only the latter.
    """
    from zai_python_helper.core.planner.opencode import (
        ALL_PROVIDER_NAMES,
        has_duplicate_regional_providers,
        owned_regional_provider_name,
    )

    if not (
        prior_doc
        and has_duplicate_regional_providers(prior_doc)
        and not plan.is_empty
    ):
        return

    owned = owned_regional_provider_name(prior_doc, journal_records)
    # Only managed regional providers are removed by the self-heal;
    # foreign providers (e.g. "openai") are preserved untouched.
    unattributed = [
        n
        for n in prior_doc.get("provider", {})
        if n in ALL_PROVIDER_NAMES and n != owned
    ]
    print(
        "  warning: opencode.json carried multiple regional "
        "providers; the non-attributed entries"
        + (f" ({', '.join(unattributed)})" if unattributed else "")
        + " were removed to activate the selected region.  "
        "This is irreversible — the removed entries are not "
        "recoverable via `use default`."
    )


def _handle_use_zai(args: argparse.Namespace) -> int:
    """Make Z.ai the default provider for the selected tool.

    Generic over the tool: looks up ``REGISTRY[args.tool]``, reads its state,
    plans via ``tool.plan_zai``, captures ownership via
    ``tool.extract_takeover``, and commits under one held :class:`ProcessLock`
    (ADR-005). ``--dry-run`` previews redacted unified_diffs and writes
    nothing. Prints ``restart recommended`` whenever it changes files.
    Idempotent: a second run with no drift is a no-op.
    """
    from zai_python_helper.constants import get_preset_model
    from zai_python_helper.core.domain import ModelMode
    from zai_python_helper.errors import ValidationError
    from zai_python_helper.io.secrets import resolve_key

    tool = _resolve_tool(args)
    region = Region(getattr(args, "region", Region.GLOBAL.value))
    paths = Paths.default()
    dry_run = getattr(args, "dry_run", False)

    mode = _resolve_mode_or_raise(args)
    spec = _build_provider_spec(args, mode, region)
    if not spec.validate():
        if mode == ModelMode.SELECT:
            raise ValidationError("--model is required for --mode select")
        if mode == ModelMode.CUSTOM:
            raise ValidationError("--model is required for --mode custom")

    # Validate the preset before planning (plan_model_config raises a bare
    # ValueError on an unknown preset; wrap it for the error contract).
    if mode == ModelMode.SELECT:
        selected = spec.selected_model
        if selected is None or get_preset_model(selected) is None:
            raise ValidationError(f"Unknown preset: {selected}")

    # Resolve the auth token in the IO layer (env/flag/prompt) — never in core.
    # In dry-run we still need a token to plan (the planner writes it into the
    # delta); a placeholder keeps the preview meaningful without prompting.
    if dry_run:
        auth_token = getattr(args, "api_key", None) or "<redacted>"
    else:
        auth_token = resolve_key(getattr(args, "api_key", None))

    if dry_run:
        # Dry-run is read-only: read state, plan, preview. No lock, no write.
        # The journal is read here too (read-only) so the preview reflects the
        # SAME ownership-aware decision the real run would take — otherwise a
        # doc that activates cleanly would preview as a refusal (issue #61).
        from zai_python_helper.ownership import OwnershipJournal as _Journal

        state = tool.read_state(paths)
        journal_records = _Journal(paths.ownership_json).read()
        plan = tool.plan_zai(
            spec,
            region,
            state=state,
            auth_token=auth_token,
            journal_records=journal_records,
        )
        print("--dry-run: no files written")
        if plan.is_empty:
            print("(no changes — already in desired state)")
        _warn_self_heal_destruction(
            state.get(FileTag.OPENCODE), journal_records, plan
        )
        _apply_plan(paths, plan, dry_run=True)
        return 0

    # Real activation: the ENTIRE read → plan → ownership-capture → commit
    # happens inside ONE held ProcessLock (ADR-005 / S3 finding #6). The lock
    # must serialize the state used to PLAN, not only the writes — otherwise a
    # concurrent ``use default`` could restore the prior between our read and
    # our commit, and we would journal the (now-stale) value we read as the
    # prior. Recovery also runs under this lock (it takes the lock itself, so
    # it serializes with us) before we read any state.
    from zai_python_helper.ownership import OwnershipJournal
    from zai_python_helper.patchplan import ProcessLock, apply_plan_locked, recover

    _run_recovery(paths, recover)

    with ProcessLock(paths.lock_file):
        # Read state, plan, and capture ownership — all inside the lock so a
        # concurrent revert cannot mutate the config between our read and our
        # commit (and so the takeover prior reflects exactly the pre-commit
        # state we are about to overwrite).
        state = tool.read_state(paths)

        # Read the journal BEFORE planning, still under the lock: a tool may
        # need prior ownership to plan at all — on a config state that is
        # ambiguous by document alone, the journal is what distinguishes an
        # entry we wrote from one the user wrote (issue #61). Reading it here
        # keeps the snapshot consistent with the state we plan against.
        journal = OwnershipJournal(paths.ownership_json)
        journal_records = journal.read()

        plan = tool.plan_zai(
            spec,
            region,
            state=state,
            auth_token=auth_token,
            journal_records=journal_records,
        )

        # If the prior doc carried both regional providers and plan_zai did
        # NOT refuse, a self-heal just replaced the user's non-attributed
        # regional entry.  Warn prominently — the deletion is irreversible
        # (the journal records only OUR apiKey, not the user's entry).
        _warn_self_heal_destruction(
            state.get(FileTag.OPENCODE), journal_records, plan
        )

        # Ownership journal (ADR-004): for every field we are about to
        # set/remove, record its PRIOR value/presence + a hash of what we set.
        # take_over is idempotent w.r.t. the restore point (a repeat
        # activation of the same value preserves the ORIGINAL prior), so a
        # re-activation does not lose the user's first pre-activation value.
        records = tool.extract_takeover(
            plan, prior_state=state, spec=spec, journal_records=journal_records
        )

        # Hand the journal's FINAL TEXT to the transaction instead of writing
        # it here: the commit layer folds it into the recovery manifest, so
        # journal + config land together or not at all (issue #60).
        def _journal_text() -> str | None:
            if not records:
                return None
            current = journal.read()
            return journal.render(_merge_takeover_records(tool, current, records))

        apply_plan_locked(paths, plan, journal_content=_journal_text)
    # Match the pinned upstream `chelper auth reload <tool>` CLI.  File
    # changes remain observable through the filesystem contract; stdout is a
    # stable process contract and must not expose paths or configuration.
    display_name = {
        "claude_code": "Claude Code",
        "opencode": "OpenCode",
        "crush": "Crush",
        "factory_droid": "Factory Droid",
    }[tool.name]
    print(f"Reloading GLM configuration to {display_name}...")
    print(f"GLM configuration reloaded to {display_name} successfully")
    return 0


def _handle_use_default(args: argparse.Namespace) -> int:
    """Revert to the default provider for the selected tool.

    Generic over the tool: non-destructive inverse of ``use zai`` (ADR-004).
    For each managed field, consult the ownership journal and restore the
    prior value (RESTORE), drop our managed value (CLEAR), or leave it
    untouched with a warning (REFUSE — the field changed externally since
    activation). Idempotent.
    """
    tool = _resolve_tool(args)
    _resolve_mode_or_raise(args)  # validate custom-only flags for CLI symmetry
    region = Region(getattr(args, "region", Region.GLOBAL.value))
    paths = Paths.default()
    dry_run = getattr(args, "dry_run", False)

    from zai_python_helper.ownership import OwnershipJournal
    from zai_python_helper.patchplan import ProcessLock, apply_plan_locked, recover

    # Roll forward any interrupted prior run first (recover takes the lock
    # itself, so it serializes with the commit below).
    _run_recovery(paths, recover)

    print(f"Reverting to default provider (tool: {tool.name}, region: {region.value})")

    if dry_run:
        # Read-only preview: no lock, no write.
        state = tool.read_state(paths)
        journal_records = OwnershipJournal(paths.ownership_json).read()
        decisions = tool.revert_decisions(journal_records, state)[0]
        plan = tool.plan_revert(
            state=state, decisions=decisions, journal_records=journal_records
        )
        _print_refuse_warnings(decisions)
        print("--dry-run: no files written")
        if plan.is_empty:
            print("(no changes — already at default)")
        _apply_plan(paths, plan, dry_run=True)
        return 0

    # Read journal + LIVE state, compute revert decisions, and commit — all
    # inside ONE held ProcessLock (ADR-005 / S3 finding #6): a concurrent
    # ``use zai`` must not be able to change the config between our decision
    # read and our commit (which would make the decisions stale).
    with ProcessLock(paths.lock_file):
        state = tool.read_state(paths)
        journal = OwnershipJournal(paths.ownership_json)
        journal_records = journal.read()
        decisions, retired_records = tool.revert_decisions(journal_records, state)
        plan = tool.plan_revert(
            state=state, decisions=decisions, journal_records=journal_records
        )
        _print_refuse_warnings(decisions)

        # Persist the retired journal ATOMICALLY WITH the revert (issue #48
        # cycle-state + issue #60 Bug 7): every RESTORE retires its record to
        # ``active=False`` so a later re-activation starts a fresh restore
        # point instead of resurrecting a stale credential. The retirement is
        # handed to the commit layer as TEXT, not written here, so it enters
        # the recovery manifest together with the config writes it describes:
        # a crash mid-commit can no longer leave ``active=False`` on disk while
        # the config still holds our value (which would strand the user's prior
        # behind a permanent REFUSE). REFUSE-only reverts leave the journal
        # byte-identical (a fresh copy with no retirement); when there is
        # nothing at all to journal we skip it so no empty file is created.
        def _journal_text() -> str | None:
            if not retired_records and not journal.path.exists():
                return None
            return journal.render(retired_records)

        written = apply_plan_locked(paths, plan, journal_content=_journal_text)
    if not written:
        print("(no changes — already at default)")
    else:
        for tag in written:
            print(f"  updated: {resolve_path(paths, tag)}")
        print(f"  {_RESTART_NOTICE}")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Print the current Claude Code ⇄ Z.ai integration state.

    Delegates to :mod:`zai_python_helper.status` (read-only detect + render).
    Detection is read-only: no writes, no network. The ``--region`` flag
    (added by S2 for the postconditions-based stub) is accepted for CLI
    compatibility but not used — ``status`` auto-detects the region from
    the configured endpoint.
    """
    from zai_python_helper.status import detect_status, render_status

    paths = Paths.default()
    report = detect_status(paths)
    print(render_status(report))
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    """Run the diagnostic pipeline — verify the configured chain works.

    Thin shell: resolve ``Paths.default()`` and delegate to
    :func:`zai_python_helper.doctor.run_doctor`. doctor owns its exit code
    (``0`` unless a check FAILs; WARNs alone → ``0``) and its own rendering,
    so this handler neither prints nor catches — it just returns the int.
    """
    import sys

    from zai_python_helper.doctor import run_cli_doctor

    return run_cli_doctor(Paths.default(), progress_stream=sys.stderr)


# --------------------------------------------------------------------------- #
# `mcp` — preset MCP server install/uninstall/list (S7, issue #8).
#
# Headless: every action is a flag (``--tool`` / ``--region`` / ``--api-key``),
# no interactive menu — our extension over the upstream. ``use zai`` / ``use
# default`` do NOT touch MCP; installing a preset is an explicit opt-in here.
# --------------------------------------------------------------------------- #


def _resolve_mcp_tool(value: str) -> "Tool":
    """Coerce a CLI ``--tool`` string into a :class:`~zai_python_helper.mcp.Tool`.

    This is the MCP-side tool resolver (``mcp.Tool`` enum), distinct from the
    generic :func:`_resolve_tool` used by ``use zai`` / ``use default`` (which
    resolves the S6 Tool ABC via ``tools.get_tool``). Wrapped so an unknown
    tool surfaces as a clean :class:`ValidationError` (one-line stderr + exit 1)
    rather than a bare ``ValueError`` traceback.
    """
    from zai_python_helper.errors import ValidationError
    from zai_python_helper.mcp import Tool

    try:
        return Tool(value)
    except ValueError as e:
        raise ValidationError(
            f"Unknown tool {value!r}. Choose one of: "
            f"{', '.join(t.value for t in Tool)}"
        ) from e


def _handle_mcp_install(args: argparse.Namespace) -> int:
    """Install a preset MCP server into a tool's MCP config (headless).

    Resolves the tool + region + key from flags, delegates the one-shot IO
    cycle to :func:`zai_python_helper.mcp.install_mcp`, and prints the outcome.
    The key is resolved via :func:`io.secrets.resolve_key` (env/flag/prompt) in
    real mode and NEVER printed in ``--dry-run`` — the preview builds the entry
    with a ``<redacted>`` placeholder so the rendered shape shows WHERE the key
    lands (``env.Z_AI_API_KEY`` / ``headers.Authorization``) without leaking it.
    ``--dry-run`` is read-only: it shows the entry that WOULD be written,
    writes nothing.
    """
    from zai_python_helper.errors import ConfigurationError, ValidationError
    from zai_python_helper.mcp import (
        PRESET_MCP_SERVICES,
        build_mcp_entry,
        install_mcp,
        preset_by_id,
    )

    tool = _resolve_mcp_tool(args.tool)
    mcp_id = args.mcp_id
    region = Region(getattr(args, "region", Region.GLOBAL.value))
    dry_run = getattr(args, "dry_run", False)

    preset = preset_by_id(mcp_id)
    if preset is None:
        known = ", ".join(p["id"] for p in PRESET_MCP_SERVICES)
        raise ValidationError(f"Unknown MCP preset: {mcp_id}. Choose one of: {known}")

    if dry_run:
        # Read-only preview: no key prompt, no write. ALWAYS build the entry
        # with the placeholder (never the real ``--api-key``) so the secret
        # can never reach stdout. The rendered shape still shows the auth
        # FIELD (env.Z_AI_API_KEY / headers.Authorization) where the key would
        # land. Redacting unconditionally — not just when --api-key is absent —
        # is the fix: the prior ``or "<redacted>"`` leaked a passed key.
        entry = build_mcp_entry(tool, mcp_id, "<redacted>", region)
        print(f"--dry-run: would install {mcp_id} into {tool.value}")
        import json

        rendered = json.dumps({mcp_id: entry}, indent=2)
        # Defense-in-depth: also run the redactor over the rendered JSON so a
        # future field that carries a secret is caught even if the builder
        # changes. Matches how `use zai` treats its dry-run diff output.
        print(_redact_text(rendered), end="")
        return 0

    from zai_python_helper.io.secrets import resolve_key

    key = resolve_key(getattr(args, "api_key", None))
    try:
        changed = install_mcp(tool, mcp_id, key, region)
    except ValueError as e:
        # install_mcp/install_into_doc fail closed with a bare ValueError on a
        # malformed (non-object) MCP section to avoid overwriting user-owned
        # data. Translate it into the project's error contract here so the
        # CLI reports a one-line message instead of an uncaught traceback.
        raise ConfigurationError(str(e)) from e
    label = "installed" if changed else "already installed (no change)"
    print(f"  {mcp_id}: {label} for {tool.value}")
    return 0


def _handle_mcp_uninstall(args: argparse.Namespace) -> int:
    """Remove a preset MCP server from a tool's MCP config (headless, opt-in).

    Delegates to :func:`zai_python_helper.mcp.uninstall_mcp`. Idempotent: an
    absent id writes nothing and reports "not installed". ``--dry-run`` is
    read-only (the same contract as ``use zai``/``use default`` dry-run): it
    reports whether the id WOULD be removed, without touching the config.
    """
    from zai_python_helper.mcp import (
        is_installed,
        read_config,
        tool_config_path,
        uninstall_mcp,
    )

    tool = _resolve_mcp_tool(args.tool)
    mcp_id = args.mcp_id
    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        # Read-only preview: never call the mutator. Resolve the live config
        # (Path.home(), the same place the real uninstall targets) and report
        # whether the id is present — without writing anything.
        doc = read_config(tool_config_path(tool, Path.home()))
        present = is_installed(doc, tool, mcp_id)
        label = "would remove" if present else "not installed (no change)"
        print(f"--dry-run: {mcp_id}: {label} from {tool.value}")
        return 0

    changed = uninstall_mcp(tool, mcp_id)
    label = "removed" if changed else "not installed (no change)"
    print(f"  {mcp_id}: {label} from {tool.value}")
    return 0


def _handle_mcp_list(args: argparse.Namespace) -> int:
    """List preset MCPs and their installed status per tool (headless, read-only).

    With ``--tool``: shows the preset table + which are installed for that one
    tool. Without ``--tool``: shows the preset table only (install status is
    per-tool, so a tool must be named to read it). Pure read — no writes.
    """
    from pathlib import Path

    from zai_python_helper.mcp import (
        PRESET_MCP_SERVICES,
        list_installed,
        read_config,
        tool_config_path,
    )

    print("Preset MCP servers:")
    print()
    installed: set[str] = set()
    tool_filter = getattr(args, "tool", None)
    if tool_filter is not None:
        tool = _resolve_mcp_tool(tool_filter)
        doc = read_config(tool_config_path(tool, Path.home()))
        installed = set(list_installed(doc, tool))

    for preset in PRESET_MCP_SERVICES:
        mark = ""
        if tool_filter is not None:
            mark = " [installed]" if preset["id"] in installed else " [not installed]"
        proto = preset["protocol"]
        endpoint = ""
        if "urlTemplate" in preset:
            endpoint = preset["urlTemplate"].get("glm_coding_plan_global", "")
        elif "command" in preset:
            endpoint = f"{preset['command']} {' '.join(preset.get('args', []))}".strip()
        print(f"  {preset['id']}{mark}")
        print(f"    name: {preset['name']} ({preset['description']})")
        print(f"    protocol: {proto}  endpoint: {endpoint}")
    print()
    print("Install with: zai-python-helper mcp install <id> --tool <tool>")
    return 0


def _add_use_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the tool + model-selection + region flags to a ``use`` subparser.

    ``--tool`` selects the integration (default: claude_code); the model-mode
    flags are a Claude-Code concern but are accepted uniformly so the CLI
    surface is consistent (inert for tools that ignore them).
    """
    from zai_python_helper.core.domain import ModelMode
    from zai_python_helper.tools import tool_names

    parser.add_argument(
        "--tool",
        choices=tool_names(),
        default="claude_code",
        help="coding tool to configure (default: claude-code)",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in ModelMode],
        default=ModelMode.ORIGINAL.value,
        help=(
            "model selection mode (default: original). "
            "original: only ANTHROPIC_BASE_URL; "
            "default: preset ANTHROPIC_DEFAULT_*_MODEL vars; "
            "select: choose a preset; "
            "custom: provide a custom model ID"
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "model ID (for --mode select or custom). "
            "select: preset name (e.g. glm-4-plus); "
            "custom: full model ID"
        ),
    )
    parser.add_argument(
        "--region",
        choices=[r.value for r in Region],
        default=Region.GLOBAL.value,
        help="Z.ai region (default: global)",
    )
    parser.add_argument(
        "--api-key",
        help="Z.ai auth token (else resolved from ZAI_API_KEY env / prompt)",
    )
    parser.add_argument(
        "--name",
        help="display name for the custom model (custom mode only)",
    )
    parser.add_argument(
        "--description",
        help="description for the custom model (custom mode only)",
    )
    parser.add_argument(
        "--capabilities",
        help="supported capabilities for the custom model (e.g. 'effort,thinking')",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the root ``zai-python-helper`` argparse parser.

    The root parser owns the global flags (``--debug`` / ``--dry-run``) and
    the subcommand dispatch table. Root flags work BOTH before AND after the
    subcommand via a single shared parent parser attached to the root parser
    AND every subparser via ``parents=[sub_flags]``.
    """
    sub_flags = argparse.ArgumentParser(add_help=False)
    sub_flags.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show full traceback on error",
    )
    sub_flags.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="preview without writing",
    )

    parser = argparse.ArgumentParser(
        prog="zai-python-helper",
        description="Manage Claude Code ⇄ Z.ai integration without hand-editing config.",
        parents=[sub_flags],
    )
    # ``-v`` / ``--version`` prints the bare version string, mirroring the
    # upstream ``@z_ai/coding-helper`` (Commander ``.version(version)``), which
    # prints just the package version with no program-name prefix. This is a
    # Phase-1 parity surface (see issue #17): the FORMAT must match the
    # original even though the version NUMBER differs.
    from zai_python_helper import __version__

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    parser.set_defaults(func=lambda args: (parser.print_help(), 0)[1])

    subparsers = parser.add_subparsers(
        dest="cmd",
        required=False,
        metavar="<command>",
    )

    # `list` — show available Z.ai model presets
    p_list = subparsers.add_parser(
        "list",
        help="list available Z.ai model presets",
        parents=[sub_flags],
    )
    p_list.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="output format (default: table)",
    )
    p_list.set_defaults(func=_handle_list)

    # `use` — nested provider sub-subs
    p_use = subparsers.add_parser(
        "use",
        help="switch the default provider",
        parents=[sub_flags],
    )
    use_sub = p_use.add_subparsers(
        dest="provider",
        required=True,
        metavar="<provider>",
    )

    p_use_zai = use_sub.add_parser(
        "zai",
        help="make Z.ai the default",
        parents=[sub_flags],
    )
    _add_use_flags(p_use_zai)
    p_use_zai.set_defaults(func=_handle_use_zai)

    p_use_default = use_sub.add_parser(
        "default",
        help="revert to default provider",
        parents=[sub_flags],
    )
    _add_use_flags(p_use_default)
    p_use_default.set_defaults(func=_handle_use_default)

    # `status` — read-only observability
    p_status = subparsers.add_parser(
        "status",
        help="show current status and paths",
        parents=[sub_flags],
    )
    p_status.add_argument(
        "--region",
        choices=[r.value for r in Region],
        default=Region.GLOBAL.value,
        help="Z.ai region to check against (default: global)",
    )
    p_status.set_defaults(func=_handle_status)

    # `doctor` — diagnostic
    p_doctor = subparsers.add_parser(
        "doctor",
        help="diagnose the integration",
        parents=[sub_flags],
    )
    p_doctor.set_defaults(func=_handle_doctor)

    # `mcp` — preset MCP server install/uninstall/list (S7, issue #8). Headless
    # (flags, no menu). ``use zai``/``use default`` do NOT touch MCP; this is
    # the explicit opt-in surface for installing the four Z.ai preset MCPs into
    # a tool's MCP config.
    p_mcp = subparsers.add_parser(
        "mcp",
        help="manage preset MCP servers (install/uninstall/list)",
        parents=[sub_flags],
    )
    mcp_sub = p_mcp.add_subparsers(
        dest="mcp_cmd",
        required=True,
        metavar="<mcp command>",
    )
    from zai_python_helper.mcp import Tool as _McpTool

    _tool_choices = [t.value for t in _McpTool]

    p_mcp_install = mcp_sub.add_parser(
        "install",
        help="install a preset MCP server into a tool",
        parents=[sub_flags],
    )
    p_mcp_install.add_argument("mcp_id", help="preset MCP id (e.g. web-search-prime)")
    p_mcp_install.add_argument(
        "--tool",
        required=True,
        choices=_tool_choices,
        help="target tool",
    )
    p_mcp_install.add_argument(
        "--region",
        choices=[r.value for r in Region],
        default=Region.GLOBAL.value,
        help="Z.ai region (default: global)",
    )
    p_mcp_install.add_argument(
        "--api-key",
        help="Z.ai auth token (else resolved from ZAI_API_KEY env / prompt)",
    )
    p_mcp_install.set_defaults(func=_handle_mcp_install)

    p_mcp_uninstall = mcp_sub.add_parser(
        "uninstall",
        help="remove a preset MCP server from a tool",
        parents=[sub_flags],
    )
    p_mcp_uninstall.add_argument("mcp_id", help="preset MCP id to remove")
    p_mcp_uninstall.add_argument(
        "--tool",
        required=True,
        choices=_tool_choices,
        help="target tool",
    )
    p_mcp_uninstall.set_defaults(func=_handle_mcp_uninstall)

    p_mcp_list = mcp_sub.add_parser(
        "list",
        help="list preset MCP servers (+ installed status with --tool)",
        parents=[sub_flags],
    )
    p_mcp_list.add_argument(
        "--tool",
        choices=_tool_choices,
        default=None,
        help="show installed status for this tool",
    )
    p_mcp_list.set_defaults(func=_handle_mcp_list)

    return parser
