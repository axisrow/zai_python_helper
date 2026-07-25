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


def _redact_json_doc(doc: Any) -> Any:
    """Return a copy of parsed JSON ``doc`` with every secret-key value redacted.

    STRUCTURAL: recurses through dicts/lists, replacing the value under any
    secret key (any nesting, any key characters incl. ``api-key``/``x-api-key``,
    any value type or escaping) with the string ``"<redacted>"``. Robust where
    a regex over the rendered string is not — regex key classes miss hyphens,
    and regex value captures split on escaped quotes, leaking secret suffixes.
    Applied to the parsed doc BEFORE it is rendered into a ``--dry-run`` diff,
    so the diff text itself never carries a secret.
    """
    if isinstance(doc, dict):
        return {
            k: ("<redacted>" if _is_secret_key(k) else _redact_json_doc(v))
            for k, v in doc.items()
        }
    if isinstance(doc, list):
        return [_redact_json_doc(item) for item in doc]
    return doc


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

    Heuristic dispatcher used when only the rendered text is available (not the
    parsed source): structural JSON redaction when the text parses as JSON,
    otherwise shell-assignment redaction. The ``--dry-run`` path redacts at the
    SOURCE (see :func:`_print_diff`) and only falls back to this for stray
    text. A key is "secret" by :func:`_is_secret_key`; a secret value never
    reaches stdout/stderr.
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
            # JSON: structural redaction on the parsed doc (robust to any key
            # chars / value escaping). Shell: regex redaction on the text.
            if delta.kind == DeltaKind.WRITE_JSON:
                cur_doc = JsonBackend.read(path)
                cur_text = (
                    JsonBackend.render(_redact_json_doc(cur_doc))
                    if cur_doc is not None
                    else ""
                )
                des_text = JsonBackend.render(_redact_json_doc(delta.content))
            else:  # WRITE_TEXT
                cur_text = _redact_shell_text(ShellBackend.read(path))
                des_text = _redact_shell_text(delta.content)
            _print_diff(path, cur_text, des_text, delta.tag)
            continue

        if delta.kind == DeltaKind.WRITE_JSON:
            JsonBackend.write(path, delta.content)
        else:
            from zai_python_helper.backends import atomic_write_bytes

            atomic_write_bytes(path, delta.content.encode("utf-8"))
        written.append(delta.tag)
    return written


def _print_diff(path: Path, current: str, desired: str, tag: FileTag) -> None:
    """Print a unified_diff for one file under ``--dry-run``.

    ``current`` and ``desired`` are ALREADY redacted at the source by the
    caller (structural JSON or shell regex), so the diff text is safe to print
    verbatim — no post-hoc redaction needed.
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

    print(
        f"Configuring Z.ai provider (tool: {tool.name}, "
        f"mode: {mode.value}, region: {region.value})"
    )

    if dry_run:
        # Dry-run is read-only: read state, plan, preview. No lock, no write.
        state = tool.read_state(paths)
        plan = tool.plan_zai(spec, region, state=state, auth_token=auth_token)
        print("--dry-run: no files written")
        if plan.is_empty:
            print("(no changes — already in desired state)")
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
        plan = tool.plan_zai(spec, region, state=state, auth_token=auth_token)

        # Ownership journal (ADR-004): for every field we are about to
        # set/remove, record its PRIOR value/presence + a hash of what we set.
        # take_over is idempotent w.r.t. the restore point (a repeat
        # activation of the same value preserves the ORIGINAL prior), so a
        # re-activation does not lose the user's first pre-activation value.
        records = tool.extract_takeover(plan, prior_state=state, spec=spec)

        journal = OwnershipJournal(paths.ownership_json)

        def _persist_journal() -> None:
            if records:
                current = journal.read()
                journal.write(_merge_takeover_records(tool, current, records))

        written = apply_plan_locked(paths, plan, on_locked=_persist_journal)
    if not written:
        print("(no changes — already in desired state)")
    else:
        for tag in written:
            print(f"  updated: {resolve_path(paths, tag)}")
        print(f"  {_RESTART_NOTICE}")

    # Echo the tool-owned summary (the Tool masks its own secrets).
    for line in tool.echo_lines(plan, region):
        print(line)
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
        decisions = tool.revert_decisions(journal_records, state)
        plan = tool.plan_revert(state=state, decisions=decisions)
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
        journal_records = OwnershipJournal(paths.ownership_json).read()
        decisions = tool.revert_decisions(journal_records, state)
        plan = tool.plan_revert(state=state, decisions=decisions)
        _print_refuse_warnings(decisions)
        written = apply_plan_locked(paths, plan)
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
    from zai_python_helper.doctor import run_doctor

    return run_doctor(Paths.default())


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
    from zai_python_helper.errors import ValidationError
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
    changed = install_mcp(tool, mcp_id, key, region)
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
