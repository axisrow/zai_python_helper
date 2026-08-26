"""Tests for the tools layer (S6 foundation): Tool ABC, registry, and the
ClaudeCodeTool adapter that wraps the existing pure planner.

These tests prove the Tool adapter preserves the execution contract while the
pure planner remains available for explicit shell-block planning.
"""

from __future__ import annotations

import pytest

from zai_python_helper.core.domain import ModelMode, ProviderSpec
from zai_python_helper.core.planner import FileTag
from zai_python_helper.core.planner import plan_zai as cc_plan_zai
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.tools import REGISTRY, get_tool, tool_names
from zai_python_helper.tools.base import TAG_TO_PATH_ATTR, resolve_path
from zai_python_helper.tools.claude_code import ClaudeCodeTool, _EnvField

TOKEN = "sk-test-token-abc"
GLOBAL_URL = "https://api.z.ai/api/anthropic"


def _spec(mode: ModelMode = ModelMode.DEFAULT, **kw) -> ProviderSpec:
    base = {"base_url": GLOBAL_URL, "model_mode": mode}
    base.update(kw)
    return ProviderSpec(**base)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_claude_code_registered_as_default(self):
        assert "claude_code" in REGISTRY
        assert isinstance(REGISTRY["claude_code"], ClaudeCodeTool)

    def test_get_tool_returns_instance(self):
        tool = get_tool("claude_code")
        assert tool.name == "claude_code"

    def test_get_tool_unknown_raises(self):
        with pytest.raises(KeyError):
            get_tool("nope")

    def test_tool_names_sorted_and_contains_claude_code(self):
        names = tool_names()
        assert names == sorted(names)
        assert "claude_code" in names

    def test_registry_keys_match_tool_name(self):
        """A tool's ``name`` must equal its registry key (ownership bucket)."""
        for key, tool in REGISTRY.items():
            assert tool.name == key


# ---------------------------------------------------------------------------
# ClaudeCodeTool plan parity (Tool iface vs pure planner direct)
# ---------------------------------------------------------------------------


class TestClaudeCodePlanParity:
    def test_plan_zai_matches_pure_planner(self):
        """plan_zai through the Tool == plan_zai called directly (same deltas)."""
        tool = get_tool("claude_code")
        spec = _spec(ModelMode.DEFAULT)
        settings = {"env": {"FOREIGN": "keep"}, "top": 1}
        state = {
            FileTag.SETTINGS: settings,
            FileTag.CLAUDE_JSON: {"theme": "dark"},
            FileTag.ZSHRC: "",
        }

        via_tool = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
        direct = cc_plan_zai(
            spec,
            Region.GLOBAL,
            settings_doc=settings,
            claude_json_doc={"theme": "dark"},
            zshrc_text="",
            auth_token=TOKEN,
        )

        # Phase 1 deliberately excludes the optional .zshrc delta; config
        # deltas remain byte-for-byte identical to the pure planner.
        direct_deltas = [d for d in direct.deltas if d.tag != FileTag.ZSHRC]
        assert len(via_tool.deltas) == len(direct_deltas)
        for vt, dt in zip(via_tool.deltas, direct_deltas, strict=True):
            assert vt.tag == dt.tag
            assert vt.kind == dt.kind
            assert vt.content == dt.content

    def test_plan_zai_is_idempotent_through_tool(self):
        """A second plan on the first plan's post-state is all-NOOP."""
        tool = get_tool("claude_code")
        spec = _spec(ModelMode.ORIGINAL)
        state = {FileTag.SETTINGS: None, FileTag.CLAUDE_JSON: None, FileTag.ZSHRC: ""}

        first = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
        # Post-state: the desired docs/texts from the first plan.
        post_state = {
            FileTag.SETTINGS: first.delta_for(FileTag.SETTINGS).content,
            FileTag.CLAUDE_JSON: first.delta_for(FileTag.CLAUDE_JSON).content,
            FileTag.ZSHRC: "",
        }
        second = tool.plan_zai(spec, Region.GLOBAL, state=post_state, auth_token=TOKEN)
        assert second.is_empty

    def test_extract_takeover_captures_managed_and_removed(self):
        """extract_takeover journals the managed env keys + the removed API_KEY."""
        tool = get_tool("claude_code")
        spec = _spec(ModelMode.ORIGINAL)
        # Prior doc carries a foreign key + the API_KEY we will remove.
        prior = {
            FileTag.SETTINGS: {"env": {"ANTHROPIC_API_KEY": "old-key", "FOREIGN": "x"}},
            FileTag.CLAUDE_JSON: None,
            FileTag.ZSHRC: "",
        }
        plan = tool.plan_zai(spec, Region.GLOBAL, state=prior, auth_token=TOKEN)
        records = dict(
            (k, (pv, pp, sh))
            for k, pv, pp, sh in tool.extract_takeover(plan, prior_state=prior, spec=spec)
        )

        # AUTH_TOKEN is set (managed) → has a set_hash.
        assert "ANTHROPIC_AUTH_TOKEN" in records
        _pv, _pp, sh = records["ANTHROPIC_AUTH_TOKEN"]
        assert sh is not None
        # ANTHROPIC_API_KEY is removed → set_hash is None (ownership-by-removal).
        assert "ANTHROPIC_API_KEY" in records
        # records[key] == (prior_value, prior_present, set_hash).
        assert records["ANTHROPIC_API_KEY"][2] is None
        assert records["ANTHROPIC_API_KEY"][1] is True  # prior_present
        assert records["ANTHROPIC_API_KEY"][0] == "old-key"  # prior value


# ---------------------------------------------------------------------------
# ManagedField (_EnvField)
# ---------------------------------------------------------------------------


class TestEnvField:
    def test_get_present(self):
        f = _EnvField("ANTHROPIC_AUTH_TOKEN")
        present, value = f.get({"env": {"ANTHROPIC_AUTH_TOKEN": "abc"}})
        assert present is True
        assert value == "abc"

    def test_get_absent(self):
        f = _EnvField("ANTHROPIC_AUTH_TOKEN")
        present, value = f.get({"env": {"OTHER": "x"}})
        assert present is False
        assert value is None

    def test_get_none_doc(self):
        f = _EnvField("X")
        assert f.get(None) == (False, None)

    def test_set_value_writes_and_preserves_foreign(self):
        f = _EnvField("ANTHROPIC_AUTH_TOKEN")
        out = f.set_value({"env": {"FOREIGN": "keep"}}, "tok")
        assert out["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok"
        assert out["env"]["FOREIGN"] == "keep"

    def test_set_value_none_removes_and_drops_empty_env(self):
        f = _EnvField("ANTHROPIC_AUTH_TOKEN")
        out = f.set_value({"env": {"ANTHROPIC_AUTH_TOKEN": "tok"}}, None)
        assert "env" not in out  # env empty → dropped

    def test_set_value_none_keeps_env_if_foreign_remains(self):
        f = _EnvField("ANTHROPIC_AUTH_TOKEN")
        out = f.set_value(
            {"env": {"ANTHROPIC_AUTH_TOKEN": "tok", "FOREIGN": "keep"}}, None
        )
        assert "ANTHROPIC_AUTH_TOKEN" not in out["env"]
        assert out["env"]["FOREIGN"] == "keep"


# ---------------------------------------------------------------------------
# resolve_path / TAG_TO_PATH_ATTR
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_known_tags_resolve(self, tmp_path):
        paths = Paths.from_home(tmp_path)
        assert resolve_path(paths, FileTag.SETTINGS) == paths.claude_settings
        assert resolve_path(paths, FileTag.ZSHRC) == paths.zshrc
        assert resolve_path(paths, FileTag.OPENCODE) == paths.opencode
        assert resolve_path(paths, FileTag.CRUSH) == paths.crush
        assert resolve_path(paths, FileTag.FACTORY_DROID) == paths.factory_droid

    def test_new_paths_under_home(self, tmp_path):
        """S6 tool paths resolve under the injected home (HOME isolation)."""
        paths = Paths.from_home(tmp_path)
        assert paths.opencode == tmp_path / ".config" / "opencode" / "opencode.json"
        assert paths.crush == tmp_path / ".config" / "crush" / "crush.json"
        assert paths.factory_droid == tmp_path / ".factory" / "settings.json"

    def test_every_filetag_has_a_path_mapping(self):
        """Every FileTag member resolves (no tag left unmapped)."""
        from zai_python_helper.core.planner import FileTag as FT

        for tag in FT:
            assert tag in TAG_TO_PATH_ATTR, f"FileTag.{tag.name} has no path mapping"


# ---------------------------------------------------------------------------
# echo_lines
# ---------------------------------------------------------------------------


class TestEcho:
    def test_echo_redacts_auth_token(self):
        tool = get_tool("claude_code")
        plan = tool.plan_zai(
            _spec(ModelMode.ORIGINAL),
            Region.GLOBAL,
            state={FileTag.SETTINGS: None, FileTag.CLAUDE_JSON: None, FileTag.ZSHRC: ""},
            auth_token=TOKEN,
        )
        lines = tool.echo_lines(plan, Region.GLOBAL)
        joined = "\n".join(lines)
        assert TOKEN not in joined  # secret never echoed in cleartext
        assert "base_url:" in joined
