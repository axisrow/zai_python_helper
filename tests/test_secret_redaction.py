"""Tests for the secret-redaction heuristic and diff renderer in cli.py.

Regression coverage for the camelCase credential-key leak: the generic
``--dry-run`` redactor must catch credential keys written in any case/separator
form (``apiKey`` / ``api_key`` / ``apikey`` / ``API-KEY``), not only the
UPPER_CASE ``API_KEY`` form. A secret value must never reach stdout/stderr via
a dry-run diff or the post-run echo.

Issue #44 (fail-closed) adds the inverse layer: a finite denylist is provably
incomplete, so the JSON redactor must HIDE any scalar whose key is not on an
explicit safe allowlist (``privateKey`` / ``Authorization`` / ``clientSecret``
…), and the shell dry-run must NOT print foreign ``.zshrc`` content at all
(zsh assignment-word quoting ``export $'KEY=VAL'`` defeats any regex). See
:class:`TestJsonFailClosedAllowlist` and :class:`TestShellForeignSuppression`.
"""

from __future__ import annotations

from zai_python_helper.cli import (
    _apply_plan,
    _is_secret_key,
    _redact_json_doc,
    _redact_text,
    _shell_managed_preview,
)
from zai_python_helper.core.planner import DeltaKind, FileDelta, FileTag, PatchPlan
from zai_python_helper.paths import Paths
from zai_python_helper.shell_block import install_owned_block


class TestIsSecretKey:
    def test_explicit_managed_keys(self):
        assert _is_secret_key("ANTHROPIC_AUTH_TOKEN")
        assert _is_secret_key("ANTHROPIC_API_KEY")

    def test_uppercase_suffix_variants(self):
        assert _is_secret_key("OPENAI_API_KEY")
        assert _is_secret_key("MY_SERVICE_TOKEN")

    def test_camel_case_apikey(self):
        """Regression: camelCase apiKey (OpenCode/Crush/Factory Droid) is secret."""
        assert _is_secret_key("apiKey")

    def test_snake_case_apikey(self):
        assert _is_secret_key("api_key")

    def test_lower_no_separator_apikey(self):
        assert _is_secret_key("apikey")

    def test_kebab_case_apikey(self):
        assert _is_secret_key("api-key")

    def test_camel_case_authtoken_accesstoken(self):
        assert _is_secret_key("authToken")
        assert _is_secret_key("accessToken")

    def test_non_secret_key_not_flagged(self):
        assert not _is_secret_key("model")
        assert not _is_secret_key("displayName")
        assert not _is_secret_key("provider")
        assert not _is_secret_key("base_url")


class TestRedactText:
    def test_json_camelcase_apikey_redacted(self):
        """Regression: a JSON apiKey value is redacted, not leaked."""
        text = '{"options": {"apiKey": "sk-sentinel-secret-xyz"}}'
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_json_snake_api_key_redacted(self):
        text = '{"api_key": "sk-sentinel-secret-xyz"}'
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_shell_export_apikey_redacted(self):
        text = 'export apiKey="sk-sentinel-secret-xyz"\n'
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_json_kebab_api_key_redacted_by_renderer(self):
        """Regression: a kebab-case JSON key (api-key) is redacted by the
        RENDERER, not just the classifier. The old regex key class
        [A-Za-z0-9_] rejected the hyphen, so the classifier was never called
        and the value leaked. Structural JSON redaction closes this."""
        text = '{"api-key": "sk-sentinel-secret-xyz"}'
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_json_x_api_key_redacted(self):
        text = '{"x-api-key": "sk-sentinel-secret-xyz"}'
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_json_escaped_quote_in_value_redacted_fully(self):
        """Regression: a value containing an escaped quote must redact the
        WHOLE secret, not just a prefix. The old regex value capture split on
        the inner quote and printed the suffix."""
        text = '{"apiKey": "sk-\\"suffix-secret"}'
        out = _redact_text(text)
        assert "sk-" not in out
        assert "suffix-secret" not in out
        assert "<redacted>" in out

    def test_json_nested_secret_redacted(self):
        """Structural redaction reaches nested objects."""
        text = '{"provider": {"options": {"apiKey": "sk-deep-secret"}}}'
        out = _redact_text(text)
        assert "sk-deep-secret" not in out
        assert "<redacted>" in out

    def test_shell_single_quoted_secret_redacted(self):
        """Regression: single-quoted shell assignment (export KEY='...') must
        redact — the old shell pattern accepted only double-quoted/unquoted."""
        text = "export OPENAI_API_KEY='sk-sentinel-secret-xyz'\n"
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_shell_kebab_key_redacted(self):
        """A kebab-case shell key (api-key=...) is also secret (key class now
        accepts hyphens)."""
        text = "api-key=sk-sentinel-secret-xyz\n"
        out = _redact_text(text)
        assert "sk-sentinel-secret-xyz" not in out
        assert "<redacted>" in out

    def test_shell_escaped_double_quote_suffix_redacted(self):
        """Regression (Codex cycle-2): a value with an embedded escaped quote
        must redact the WHOLE RHS — the old regex consumed only the first
        quoted segment and printed the suffix."""
        text = 'export OPENAI_API_KEY="prefix-\\"SECRET-SUFFIX"\n'
        out = _redact_text(text)
        assert "SECRET-SUFFIX" not in out
        assert "prefix-" not in out
        assert "<redacted>" in out

    def test_shell_ansi_c_quoting_redacted(self):
        """Regression (Codex cycle-2): zsh ANSI-C quoting ($'...') must
        redact the whole token."""
        text = "export OPENAI_API_KEY=$'SECRET-WHOLE'\n"
        out = _redact_text(text)
        assert "SECRET-WHOLE" not in out
        assert "<redacted>" in out

    def test_shell_concatenated_quotes_redacted(self):
        """Regression (Codex cycle-2): concatenated quoting ('a'"b") must
        redact the whole RHS, not just the first segment."""
        text = 'export ANTHROPIC_API_KEY=\'"SECRET-A"\'"SECRET-B"\n'
        out = _redact_text(text)
        assert "SECRET-A" not in out
        assert "SECRET-B" not in out
        assert "<redacted>" in out

    def test_shell_unquoted_hash_suffix_redacted(self):
        """Regression (Codex cycle-2): an unquoted value with a trailing
        comment must redact the whole RHS (incl. the suffix)."""
        text = "export OPENAI_API_KEY=sk-secret # my key\n"
        out = _redact_text(text)
        assert "sk-secret" not in out
        assert "<redacted>" in out

    def test_shell_typeset_gx_prefix_redacted(self):
        """Regression (Codex cycle-2): zsh ``typeset -gx KEY=SECRET`` (the
        canonical oh-my-zsh/prezto global-export form) must redact the RHS.
        The old prefix alternation only accepted a plain ``export``."""
        text = 'typeset -gx ANTHROPIC_API_KEY="sk-typeset-secret"\n'
        out = _redact_text(text)
        assert "sk-typeset-secret" not in out
        assert "<redacted>" in out

    def test_shell_export_g_flag_redacted(self):
        """Regression (Codex cycle-2): ``export -g KEY=SECRET`` (the bash/zsh
        way to set a global from a function) must redact the RHS."""
        text = "export -g OPENAI_API_KEY=sk-export-g-secret\n"
        out = _redact_text(text)
        assert "sk-export-g-secret" not in out
        assert "<redacted>" in out

    def test_shell_multi_assignment_secret_after_nonsecret_redacted(self):
        """Regression (Codex cycle-3): a single ``export`` line with multiple
        assignments where a NON-secret key precedes a SECRET one must redact
        the whole line. The regex matches only the first ``KEY=VALUE``; the
        first key (ANTHROPIC_BASE_URL) is non-secret, so without inspecting
        later tokens the second assignment's credential leaked. ``ANTHROPIC_*``
        overrides in a user's ``.zshrc`` are exactly the project's credential
        surface, and one-line two-var exports are valid POSIX/zsh."""
        text = 'export ANTHROPIC_BASE_URL=https://api.z.ai ANTHROPIC_API_KEY=sk-secret-xyz\n'
        out = _redact_text(text)
        assert "sk-secret-xyz" not in out
        assert "<redacted>" in out

    def test_shell_multi_assignment_all_nonsecret_preserved(self):
        """A multi-assignment line where NEITHER key is secret is NOT redacted
        (the fail-closed multi-key check must not over-redact)."""
        text = "export PATH=/usr/local/bin:$PATH MODEL=glm\n"
        out = _redact_text(text)
        assert "/usr/local/bin" in out
        assert "glm" in out
        assert "<redacted>" not in out

    def test_non_secret_shell_value_preserved(self):
        """A non-secret assignment is NOT redacted (fail-closed only on secrets)."""
        text = 'export PATH="/usr/local/bin:$PATH"\n'
        out = _redact_text(text)
        assert "/usr/local/bin" in out
        assert "<redacted>" not in out

    def test_non_secret_json_value_preserved(self):
        text = '{"model": "glm-4.6", "displayName": "Z.ai Plan"}'
        out = _redact_text(text)
        assert "glm-4.6" in out
        assert "Z.ai Plan" in out


# ---------------------------------------------------------------------------
# Issue #44 — JSON fail-closed allowlist (denylist is provably incomplete)
# ---------------------------------------------------------------------------


class TestJsonFailClosedAllowlist:
    """A scalar under an UNCLASSIFIED key is redacted, never shown.

    The denylist (:func:`_is_secret_key`) only matches keys we enumerated. A
    credential field whose name we did NOT list (``privateKey``,
    ``Authorization``, ``clientSecret``) is unclassified → the fail-closed
    layer hides it. These are exactly the gaps the #43 cycle-review flagged
    (Codex cycle-3, confidence 0.98).
    """

    def test_private_key_field_redacted(self):
        """``privateKey`` is not in the denylist — fail-closed hides it."""
        out = _redact_text('{"privateKey": "-----BEGIN SENTINEL PEM-----"}')
        assert "SENTINEL PEM" not in out
        assert "<redacted>" in out

    def test_authorization_header_redacted(self):
        """``Authorization`` (Bearer token) is unclassified → redacted."""
        out = _redact_text('{"headers": {"Authorization": "Bearer SENTINEL"}}')
        assert "SENTINEL" not in out
        assert "Bearer SENTINEL" not in out

    def test_client_secret_field_redacted(self):
        out = _redact_text('{"clientSecret": "SENTINEL-SECRET"}')
        assert "SENTINEL-SECRET" not in out
        assert "<redacted>" in out

    def test_unknown_nested_scalar_redacted(self):
        """Any arbitrary foreign scalar is hidden, at any depth."""
        out = _redact_text(
            '{"providers": {"foreign": {"someToken": "SENTINEL-NESTED"}}}'
        )
        assert "SENTINEL-NESTED" not in out

    def test_value_with_escaped_quotes_redacted_wholly(self):
        """Escaped quotes / newlines in an unclassified value do not leak a
        suffix — structural redaction replaces the whole scalar."""
        out = _redact_text('{"privateKey": "SENTINEL-\\"suffix\\nmore"}')
        assert "SENTINEL" not in out
        assert "suffix" not in out
        assert "<redacted>" in out

    def test_safe_values_still_visible(self):
        """Fail-closed must not make the diff useless: known non-secret values
        (base URL, model id, timeout) are shown so the user sees what changes."""
        text = (
            '{"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai", '
            '"ANTHROPIC_DEFAULT_SONNET_MODEL": "zai/glm-4.7", '
            '"ANTHROPIC_AUTH_TOKEN": "sk-SENTINEL"}}'
        )
        out = _redact_text(text)
        assert "sk-SENTINEL" not in out  # secret hidden
        assert "https://api.z.ai" in out  # safe shown
        assert "zai/glm-4.7" in out  # safe shown

    def test_safe_container_hides_secret_child(self):
        """A safe container (``env``) still hides a secret CHILD by its own
        key — fail-closed is per-scalar, not per-container."""
        text = (
            '{"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai", '
            '"OPENAI_API_KEY": "sk-SENTINEL"}}'
        )
        out = _redact_text(text)
        assert "sk-SENTINEL" not in out
        assert "https://api.z.ai" in out

    def test_safe_list_of_scalars_visible(self):
        """A list of scalars under a SAFE key (``args``) is shown; the command
        tokens are not credentials. Over-redacting here would hide the diff."""
        text = '{"command": "npx", "args": ["-y", "@z_ai/mcp-server"]}'
        out = _redact_text(text)
        assert '"-y"' in out
        assert "@z_ai/mcp-server" in out
        assert "<redacted>" not in out

    def test_unclassified_list_of_scalars_redacted(self):
        """A list of scalars under an UNCLASSIFIED key has each element
        redacted (a bare list element under a secret-ish field carries no key
        to vouch for it)."""
        text = '{"tokens": ["SENTINEL-A", "SENTINEL-B"]}'
        out = _redact_text(text)
        assert "SENTINEL-A" not in out
        assert "SENTINEL-B" not in out

    def test_regression_camelcase_still_redacted(self):
        """#43 forms stay closed after the fail-closed refactor."""
        out = _redact_text('{"apiKey": "sk-SENTINEL"}')
        assert "sk-SENTINEL" not in out
        out = _redact_text('{"api-key": "sk-SENTINEL"}')
        assert "sk-SENTINEL" not in out

    def test_structural_doc_helper_directly(self):
        """The structural helper (used at the SOURCE by ``_apply_plan``) hides
        an unclassified scalar; the denylist fast path hides a secret one."""
        redacted = _redact_json_doc(
            {
                "privateKey": "SENTINEL-UNCLASSIFIED",
                "apiKey": "SENTINEL-DENYLIST",
                "ANTHROPIC_BASE_URL": "https://api.z.ai",
            }
        )
        assert redacted["privateKey"] == "<redacted>"
        assert redacted["apiKey"] == "<redacted>"
        assert redacted["ANTHROPIC_BASE_URL"] == "https://api.z.ai"

    def test_container_key_list_of_scalars_redacted(self):
        """Regression (PR #51 review): a bare-scalar LIST under a CONTAINER
        key (env/headers/…) is redacted element-for-element. Container keys are
        structural walkers — their children classify by OWN key (a dict); a
        bare-scalar list under them is unclassified input and must NOT inherit
        the container's "safe" status. Without this, ``{"env": ["sk-…"]}``
        leaked because ``env`` is in the safe-allowlist."""
        for container in ("env", "headers", "environment", "options"):
            redacted = _redact_json_doc({container: ["SENTINEL-A", "SENTINEL-B"]})
            assert redacted == {container: ["<redacted>", "<redacted>"]}, (
                f"{container}: bare-scalar list leaked"
            )

    def test_scalar_list_parent_args_still_visible(self):
        """The container-key fix must NOT over-redact a genuine scalar-list
        parent: ``args`` (command tokens) shows its elements. Guards the
        inverse of test_container_key_list_of_scalars_redacted."""
        redacted = _redact_json_doc({"args": ["-y", "@z_ai/mcp-server"]})
        assert redacted == {"args": ["-y", "@z_ai/mcp-server"]}


# ---------------------------------------------------------------------------
# Issue #44 — shell foreign .zshrc suppression in dry-run
# ---------------------------------------------------------------------------


class TestShellForeignSuppression:
    """Foreign ``.zshrc`` content is NEVER printed in ``--dry-run``.

    zsh lets you quote the assignment WORD itself
    (``export $'OPENAI_API_KEY=SENTINEL'``, ``export "OPENAI_API_KEY=SENTINEL'``),
    which sets+exports the credential but is not matched by any line-anchored
    regex (Codex cycle-3, confidence 0.99). The only provably-safe choice is to
    not echo foreign content: the diff shows ONLY the managed block plus a
    count of hidden foreign lines.
    """

    def _foreign_with_zsh_gap(self) -> str:
        """A foreign .zshrc whose tail carries the zsh assignment-word GAP."""
        return (
            "# my shell config\n"
            "export PATH=/usr/local/bin:$PATH\n"
            "alias ll='ls -la'\n"
            # The two known-gap forms #43's regex cannot catch:
            "export $'OPENAI_API_KEY=SENTINEL-ZSH-DOLLAR'\n"
            'export "OPENAI_API_KEY=SENTINEL-ZSH-QUOTE"\n'
        )

    def test_foreign_content_never_in_preview(self):
        """No foreign line (incl. the zsh-quoting gap) reaches the preview."""
        preview = _shell_managed_preview(self._foreign_with_zsh_gap())
        assert "SENTINEL-ZSH-DOLLAR" not in preview
        assert "SENTINEL-ZSH-QUOTE" not in preview
        assert "alias ll" not in preview
        assert "export PATH" not in preview

    def test_foreign_summary_reports_count(self):
        """The summary line reports how many foreign lines were hidden (so the
        user knows their content exists without seeing it)."""
        preview = _shell_managed_preview(self._foreign_with_zsh_gap())
        assert "foreign line" in preview
        # 5 non-blank foreign lines in the fixture.
        assert "5 foreign lines hidden" in preview

    def test_managed_block_shown_after_install(self):
        """After installing the block, the preview shows the managed fences —
        that is the whole point of the diff (block added)."""
        desired = install_owned_block(self._foreign_with_zsh_gap())
        preview = _shell_managed_preview(desired)
        assert ">>> zai-python-helper managed >>>" in preview
        assert "<<< zai-python-helper managed <<<" in preview
        # And still no secret:
        assert "SENTINEL" not in preview

    def test_no_managed_block_summarizes_all_foreign(self):
        """A .zshrc with NO managed block: everything is foreign, nothing
        structural is shown beyond the summary."""
        preview = _shell_managed_preview("# plain comment\nexport FOO=bar\n")
        assert "export FOO=bar" not in preview
        assert "foreign line" in preview

    def test_apply_plan_dry_run_no_zsh_leak(self, tmp_path, monkeypatch, capsys):
        """END-TO-END: ``_apply_plan(dry_run=True)`` on a .zshrc carrying the
        zsh-quoting gap prints a diff with NO sentinel, only the managed block.

        Issue #44 acceptance: a sentinel at the file tail must not reach stdout.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        paths = Paths.from_home(tmp_path, state_home=tmp_path)
        foreign = self._foreign_with_zsh_gap()
        paths.zshrc.write_text(foreign)

        desired = install_owned_block(foreign)
        plan = PatchPlan(
            deltas=(FileDelta(FileTag.ZSHRC, DeltaKind.WRITE_TEXT, desired),)
        )
        _apply_plan(paths, plan, dry_run=True)

        out = capsys.readouterr().out
        assert "SENTINEL-ZSH-DOLLAR" not in out
        assert "SENTINEL-ZSH-QUOTE" not in out
        # The managed block IS shown (that is the change being previewed):
        assert "zai-python-helper managed" in out
        # And the foreign summary is present:
        assert "foreign line" in out

    def test_managed_block_injected_export_redacted(self):
        """Regression (PR #51 review): an export line INJECTED between the
        fences (manual edit / merge / another tool) is redacted in the preview.

        ``_find_block_range`` validates only fence ordering/uniqueness, NOT
        body content — so an injected ``export ANTHROPIC_API_KEY=sk-…`` still
        parses as a valid managed block. Pre-fix, ``_shell_managed_preview``
        printed the slice verbatim and the secret leaked; the managed slice is
        now run through ``_redact_shell_text`` (defense-in-depth, preserving
        what #43 established for the whole-file path)."""
        from zai_python_helper.shell_block import (
            MANAGED_BLOCK_BEGIN,
            MANAGED_BLOCK_END,
        )

        injected = (
            f"{MANAGED_BLOCK_BEGIN}\n"
            "# This block is managed by zai-python-helper — do not edit or move it.\n"
            "export ANTHROPIC_API_KEY=sk-INJECTED-BETWEEN-FENCES\n"
            f"{MANAGED_BLOCK_END}\n"
        )
        preview = _shell_managed_preview(injected)
        assert "sk-INJECTED-BETWEEN-FENCES" not in preview
        assert "export ANTHROPIC_API_KEY=<redacted>" in preview
        # The fence structure is still shown (the block is present):
        assert "zai-python-helper managed" in preview
