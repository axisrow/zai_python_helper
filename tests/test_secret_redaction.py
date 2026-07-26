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

    def test_non_secret_json_value_preserved(self):
        text = '{"model": "glm-4.6", "displayName": "Z.ai Plan"}'
        out = _redact_text(text)
        assert "glm-4.6" in out
        assert "Z.ai Plan" in out
