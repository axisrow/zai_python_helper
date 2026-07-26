"""Tests for the secret-redaction heuristic and diff renderer in cli.py.

Regression coverage for the camelCase credential-key leak: the generic
``--dry-run`` redactor must catch credential keys written in any case/separator
form (``apiKey`` / ``api_key`` / ``apikey`` / ``API-KEY``), not only the
UPPER_CASE ``API_KEY`` form. A secret value must never reach stdout/stderr via
a dry-run diff or the post-run echo.
"""

from __future__ import annotations

from zai_python_helper.cli import _is_secret_key, _redact_text


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
