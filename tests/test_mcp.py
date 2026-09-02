"""Unit and integration tests for preset MCP install/uninstall (S7, issue #8).

The literal assertions in this module are fast tests of our internal entry
builders. They do not claim upstream parity. Live byte-for-byte comparisons
against the pinned ``@z_ai/coding-helper`` run in ``tests/parity/test_parity.py``.

The pure transforms (``build_mcp_entry`` / ``install_into_doc`` /
``uninstall_from_doc``) are tested on plain dicts (no FS); the high-level
``install_mcp`` / ``uninstall_mcp`` cycle is tested with a tmp HOME (autouse
``_isolate_home``) and reads the written JSON back via ``JsonBackend``.
"""

from __future__ import annotations

import json

import pytest

from zai_python_helper.backends import JsonBackend
from zai_python_helper.mcp import (
    Tool,
    build_mcp_entry,
    install_into_doc,
    install_mcp,
    is_installed,
    list_installed,
    preset_by_id,
    preset_ids,
    tool_config_path,
    uninstall_from_doc,
    uninstall_mcp,
)
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region

#: A clearly-fake key used everywhere — never a real credential.
_KEY = "sk-fake-mcp-test-key"

#: The four preset ids, in declaration order.
_EXPECTED_IDS = ["zai-mcp-server", "web-search-prime", "web-reader", "zread"]


# --------------------------------------------------------------------------- #
# Preset table — fast unit coverage for the static definitions.
# --------------------------------------------------------------------------- #


def test_preset_ids_are_the_four_canonical_presets():
    """The preset table ships exactly the four upstream preset MCPs, in order."""
    assert preset_ids() == _EXPECTED_IDS


def test_preset_by_id_roundtrip():
    """Each declared id resolves to its preset; an unknown id resolves to None."""
    for mcp_id in _EXPECTED_IDS:
        assert preset_by_id(mcp_id)["id"] == mcp_id
    assert preset_by_id("no-such-mcp") is None


@pytest.mark.parametrize("mcp_id", _EXPECTED_IDS[1:])  # the three http presets
def test_http_presets_carry_region_aware_url_template(mcp_id):
    """Each http preset's urlTemplate has BOTH the global and china endpoints."""
    template = preset_by_id(mcp_id)["urlTemplate"]
    assert template["glm_coding_plan_global"].startswith("https://api.z.ai/api/mcp/")
    assert template["glm_coding_plan_china"].startswith(
        "https://open.bigmodel.cn/api/mcp/"
    )
    assert template["glm_coding_plan_global"].endswith("/mcp")
    assert template["glm_coding_plan_china"].endswith("/mcp")


def test_stdio_preset_carries_region_aware_env_template():
    """The stdio zai-mcp-server carries Z_AI_MODE=ZAI (global) / ZHIPU (china)."""
    env = preset_by_id("zai-mcp-server")["envTemplate"]
    assert env["glm_coding_plan_global"] == {"Z_AI_MODE": "ZAI"}
    assert env["glm_coding_plan_china"] == {"Z_AI_MODE": "ZHIPU"}


# --------------------------------------------------------------------------- #
# build_mcp_entry — exact internal per-tool entry shapes (unit tests).
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_claude_code_stdio_entry_exact_unit_shape():
    """Claude Code stdio: {type:stdio, command, args, env} with Z_AI_API_KEY."""
    entry = build_mcp_entry(Tool.CLAUDE_CODE, "zai-mcp-server", _KEY, Region.GLOBAL)
    assert entry == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@z_ai/mcp-server"],
        "env": {"Z_AI_MODE": "ZAI", "Z_AI_API_KEY": _KEY},
    }


@pytest.mark.unit
def test_claude_code_http_entry_exact_unit_shape():
    """Claude Code http: {type:http, url, headers:{Authorization: Bearer}}."""
    entry = build_mcp_entry(Tool.CLAUDE_CODE, "web-search-prime", _KEY, Region.GLOBAL)
    assert entry == {
        "type": "http",
        "url": "https://api.z.ai/api/mcp/web_search_prime/mcp",
        "headers": {"Authorization": f"Bearer {_KEY}"},
    }


@pytest.mark.unit
def test_opencode_entry_shapes_unit_contract():
    """OpenCode stdio uses 'local' + command array + 'environment'; http 'remote'."""
    stdio = build_mcp_entry(Tool.OPENCODE, "zai-mcp-server", _KEY, Region.GLOBAL)
    assert stdio == {
        "type": "local",
        "command": ["npx", "-y", "@z_ai/mcp-server"],
        "environment": {"Z_AI_MODE": "ZAI", "Z_AI_API_KEY": _KEY},
    }
    http = build_mcp_entry(Tool.OPENCODE, "zread", _KEY, Region.GLOBAL)
    assert http["type"] == "remote"
    assert http["headers"] == {"Authorization": f"Bearer {_KEY}"}


@pytest.mark.unit
def test_factory_droid_entry_shapes_unit_contract():
    """Factory Droid adds 'disabled: false' to both stdio and http entries."""
    stdio = build_mcp_entry(Tool.FACTORY_DROID, "zai-mcp-server", _KEY, Region.GLOBAL)
    assert stdio["disabled"] is False
    http = build_mcp_entry(Tool.FACTORY_DROID, "web-reader", _KEY, Region.GLOBAL)
    assert http["disabled"] is False


@pytest.mark.unit
def test_crush_http_entry_shape_unit_contract():
    """Crush uses the same http entry shape as Claude Code (type: http)."""
    crush = build_mcp_entry(Tool.CRUSH, "web-search-prime", _KEY, Region.GLOBAL)
    claude = build_mcp_entry(Tool.CLAUDE_CODE, "web-search-prime", _KEY, Region.GLOBAL)
    assert crush == claude


# --------------------------------------------------------------------------- #
# Region awareness + auth handling.
# --------------------------------------------------------------------------- #


def test_region_china_swaps_url_host_and_mode():
    """CHINA region: open.bigmodel.cn host + Z_AI_MODE=ZHIPU."""
    http = build_mcp_entry(Tool.CLAUDE_CODE, "web-search-prime", _KEY, Region.CHINA)
    assert http["url"] == "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
    stdio = build_mcp_entry(Tool.CLAUDE_CODE, "zai-mcp-server", _KEY, Region.CHINA)
    assert stdio["env"]["Z_AI_MODE"] == "ZHIPU"


def test_region_global_uses_api_z_ai_and_zai_mode():
    """GLOBAL region: api.z.ai host + Z_AI_MODE=ZAI."""
    http = build_mcp_entry(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL)
    assert http["url"] == "https://api.z.ai/api/mcp/zread/mcp"
    stdio = build_mcp_entry(Tool.CLAUDE_CODE, "zai-mcp-server", _KEY, Region.GLOBAL)
    assert stdio["env"]["Z_AI_MODE"] == "ZAI"


def test_install_without_key_omits_auth():
    """A None key installs the entry WITHOUT the auth env/header."""
    stdio = build_mcp_entry(Tool.CLAUDE_CODE, "zai-mcp-server", None, Region.GLOBAL)
    assert stdio["env"] == {"Z_AI_MODE": "ZAI"}
    http = build_mcp_entry(Tool.CLAUDE_CODE, "web-reader", None, Region.GLOBAL)
    assert http["headers"] == {}


def test_unknown_preset_raises():
    """An unknown preset id surfaces as a ValueError (caller wraps it)."""
    with pytest.raises(ValueError, match="Unknown MCP preset"):
        build_mcp_entry(Tool.CLAUDE_CODE, "nope", _KEY, Region.GLOBAL)


# --------------------------------------------------------------------------- #
# Pure document transforms — install_into_doc / uninstall_from_doc.
# --------------------------------------------------------------------------- #


def test_install_into_doc_creates_section_and_preserves_foreign():
    """Installing creates the section, keeps foreign keys + foreign entries, pure."""
    prior = {
        "hasCompletedOnboarding": True,  # foreign top-level key
        "mcpServers": {"user-custom": {"type": "stdio"}},  # foreign entry
    }
    doc = install_into_doc(prior, Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL)
    assert doc["hasCompletedOnboarding"] is True
    assert doc["mcpServers"]["user-custom"] == {"type": "stdio"}
    assert "zread" in doc["mcpServers"]
    # Input not mutated (pure).
    assert "zread" not in prior["mcpServers"]


def test_install_into_doc_uses_correct_section_per_tool():
    """OpenCode writes to 'mcp'; Claude Code to 'mcpServers'."""
    opencode = install_into_doc(None, Tool.OPENCODE, "zread", _KEY, Region.GLOBAL)
    assert "mcp" in opencode and "mcpServers" not in opencode
    claude = install_into_doc(None, Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL)
    assert "mcpServers" in claude and "mcp" not in claude


def test_install_into_doc_rejects_malformed_section_without_overwriting_it():
    """A non-object MCP section must fail closed rather than lose user data."""
    with pytest.raises(ValueError, match="mcpServers.*JSON object"):
        install_into_doc(
            {"mcpServers": ["user-owned-value"]},
            Tool.CLAUDE_CODE,
            "zread",
            _KEY,
            Region.GLOBAL,
        )


def test_uninstall_from_doc_removes_only_its_id_and_preserves_empty_section():
    """uninstall removes ONLY the named id; siblings survive; empty section stays."""
    doc = {
        "hasCompletedOnboarding": True,
        "mcpServers": {
            "zread": {"type": "http"},
            "user-custom": {"type": "stdio"},
        },
    }
    out = uninstall_from_doc(doc, Tool.CLAUDE_CODE, "zread")
    assert "zread" not in out["mcpServers"]
    assert out["mcpServers"]["user-custom"] == {"type": "stdio"}
    assert out["hasCompletedOnboarding"] is True
    # Input not mutated.
    assert "zread" in doc["mcpServers"]
    # Removing the last entry preserves the section as an empty object.
    last = uninstall_from_doc(out, Tool.CLAUDE_CODE, "user-custom")
    assert last["mcpServers"] == {}


@pytest.mark.parametrize(
    "tool,section",
    [
        (Tool.CLAUDE_CODE, "mcpServers"),
        (Tool.OPENCODE, "mcp"),
        (Tool.CRUSH, "mcp"),
        (Tool.FACTORY_DROID, "mcpServers"),
    ],
)
def test_uninstall_from_doc_preserves_empty_section_for_every_tool(tool, section):
    """All adapters retain their configured section after the last uninstall."""
    doc = {section: {"zread": {"type": "http"}}}

    out = uninstall_from_doc(doc, tool, "zread")

    assert out == {section: {}}
    assert doc == {section: {"zread": {"type": "http"}}}


def test_uninstall_idempotent_on_absent_id():
    """Removing an absent id is a no-op; None doc -> empty dict."""
    doc = {"mcpServers": {"zread": {"type": "http"}}}
    assert uninstall_from_doc(doc, Tool.CLAUDE_CODE, "web-reader") == doc
    assert uninstall_from_doc(None, Tool.CLAUDE_CODE, "zread") == {}


def test_list_installed_and_is_installed():
    """list_installed returns ids in order; is_installed is membership."""
    doc = {"mcpServers": {"zread": {}, "web-reader": {}}}
    assert list_installed(doc, Tool.CLAUDE_CODE) == ["zread", "web-reader"]
    assert is_installed(doc, Tool.CLAUDE_CODE, "zread") is True
    assert is_installed(doc, Tool.CLAUDE_CODE, "nope") is False
    assert list_installed(None, Tool.CLAUDE_CODE) == []


@pytest.mark.parametrize(
    "tool,rel",
    [
        (Tool.CLAUDE_CODE, ".claude.json"),
        (Tool.OPENCODE, ".config/opencode/opencode.json"),
        (Tool.CRUSH, ".config/crush/crush.json"),
        (Tool.FACTORY_DROID, ".factory/mcp.json"),
    ],
)
def test_tool_config_path_resolves_per_tool(_isolate_home, tool, rel):
    """Each tool's config path resolves off home at its fixed location."""
    assert tool_config_path(tool, _isolate_home) == _isolate_home / rel


# --------------------------------------------------------------------------- #
# High-level install_mcp / uninstall_mcp — the IO cycle on a tmp HOME.
# --------------------------------------------------------------------------- #


def test_install_mcp_writes_section_and_entry_to_disk(_isolate_home):
    """install_mcp writes the tool config with the exact entry to disk."""
    changed = install_mcp(
        Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home
    )
    assert changed is True
    on_disk = JsonBackend.read(tool_config_path(Tool.CLAUDE_CODE, _isolate_home))
    assert on_disk == {
        "mcpServers": {
            "zread": build_mcp_entry(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL)
        }
    }


def test_opencode_mcp_config_uses_upstream_four_space_indent(_isolate_home):
    """OpenCode MCP install/uninstall writes match upstream JSON formatting."""
    path = tool_config_path(Tool.OPENCODE, _isolate_home)
    install_mcp(Tool.OPENCODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home)
    text = path.read_text(encoding="utf-8")
    assert '    "mcp": {' in text
    assert '        "zread": {' in text
    assert '            "type": "remote"' in text

    uninstall_mcp(Tool.OPENCODE, "zread", home=_isolate_home)
    assert path.read_text(encoding="utf-8") == '{\n    \"mcp\": {}\n}'


def test_install_mcp_preserves_foreign_and_is_idempotent(_isolate_home):
    """install deep-merges (foreign kept); re-install with same value is a no-op."""
    path = tool_config_path(Tool.CLAUDE_CODE, _isolate_home)
    JsonBackend.write(path, {"hasCompletedOnboarding": True, "mcpServers": {"x": {}}})
    install_mcp(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home)
    on_disk = JsonBackend.read(path)
    assert on_disk["hasCompletedOnboarding"] is True
    assert on_disk["mcpServers"]["x"] == {}
    assert "zread" in on_disk["mcpServers"]
    # Idempotent re-install writes nothing.
    assert (
        install_mcp(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home)
        is False
    )


def test_install_mcp_accepts_string_tool(_isolate_home):
    """A string tool value ('claude-code') coerces to the Tool enum."""
    assert install_mcp("claude-code", "zread", _KEY, Region.GLOBAL, home=_isolate_home)
    on_disk = JsonBackend.read(tool_config_path(Tool.CLAUDE_CODE, _isolate_home))
    assert is_installed(on_disk, Tool.CLAUDE_CODE, "zread")


def test_uninstall_mcp_removes_entry_leaving_siblings(_isolate_home):
    """uninstall_mcp removes the id and leaves siblings on disk; idempotent."""
    install_mcp(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home)
    install_mcp(Tool.CLAUDE_CODE, "web-reader", _KEY, Region.GLOBAL, home=_isolate_home)
    assert uninstall_mcp(Tool.CLAUDE_CODE, "zread", home=_isolate_home) is True
    on_disk = JsonBackend.read(tool_config_path(Tool.CLAUDE_CODE, _isolate_home))
    assert "zread" not in on_disk["mcpServers"]
    assert "web-reader" in on_disk["mcpServers"]
    # Idempotent on absent id.
    assert uninstall_mcp(Tool.CLAUDE_CODE, "zread", home=_isolate_home) is False


@pytest.mark.parametrize(
    "tool,section",
    [
        (Tool.CLAUDE_CODE, "mcpServers"),
        (Tool.OPENCODE, "mcp"),
        (Tool.CRUSH, "mcp"),
        (Tool.FACTORY_DROID, "mcpServers"),
    ],
)
def test_uninstall_mcp_writes_empty_section_for_every_tool(_isolate_home, tool, section):
    """The high-level uninstall persists an empty section for every adapter."""
    install_mcp(tool, "zread", _KEY, Region.GLOBAL, home=_isolate_home)

    assert uninstall_mcp(tool, "zread", home=_isolate_home) is True
    assert JsonBackend.read(tool_config_path(tool, _isolate_home)) == {section: {}}


def test_install_mcp_uses_injected_io_seams(_isolate_home):
    """The reader/writer seams are injectable (tests fake the FS)."""
    store: dict = {}

    def fake_reader(path):
        return store.get(str(path))

    def fake_writer(path, doc):
        store[str(path)] = doc

    install_mcp(
        Tool.OPENCODE,
        "zread",
        _KEY,
        Region.GLOBAL,
        home=_isolate_home,
        reader=fake_reader,
        writer=fake_writer,
    )
    path = str(tool_config_path(Tool.OPENCODE, _isolate_home))
    assert store[path]["mcp"]["zread"]["type"] == "remote"


def test_written_config_uses_compact_json_bytes(_isolate_home):
    """Tool config JSON is parseable, mode 0644, and has no final newline."""
    install_mcp(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home)
    path = tool_config_path(Tool.CLAUDE_CODE, _isolate_home)
    text = path.read_text(encoding="utf-8")
    assert not text.endswith("\n")
    json.loads(text)
    assert path.stat().st_mode & 0o777 == 0o644


# --------------------------------------------------------------------------- #
# doctor — the MCP probe (READ-ONLY, never fails).
# --------------------------------------------------------------------------- #


def _write_minimal_zai_settings(home) -> None:
    """Write a minimal valid Z.ai settings.json so doctor's chain is healthy."""
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": _KEY,
                }
            }
        ),
        encoding="utf-8",
    )


def test_doctor_mcp_probe_none_then_installed(_isolate_home, capsys):
    """The MCP probe reports 'none installed' by default and surfaces installed ids.

    Both verdicts are PASS (opt-in), so an installed OR absent MCP never fails
    doctor. The HTTP probe is stubbed offline so the run is deterministic.
    """
    from zai_python_helper.doctor import ProbeResult, run_doctor

    _write_minimal_zai_settings(_isolate_home)
    offline = lambda url, headers, body: ProbeResult(  # noqa: E731
        status=None, error="offline"
    )

    rc = run_doctor(
        Paths.from_home(_isolate_home, state_home=_isolate_home), color=False, environ={}, http_get=offline
    )
    assert "preset MCP servers" in capsys.readouterr().out
    assert rc == 0  # MCP must never fail doctor.

    install_mcp(Tool.CLAUDE_CODE, "zread", _KEY, Region.GLOBAL, home=_isolate_home)
    rc = run_doctor(
        Paths.from_home(_isolate_home, state_home=_isolate_home), color=False, environ={}, http_get=offline
    )
    out = capsys.readouterr().out
    assert "preset MCP servers" in out
    assert "zread@claude-code" in out
    assert rc == 0


# --------------------------------------------------------------------------- #
# CLI — headless install/uninstall/list + dry-run secret redaction.
# --------------------------------------------------------------------------- #


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke the CLI parser+handler in-process; return (rc, stdout, stderr).

    Mirrors ``__main__.main``: a handler raises :class:`ZaiPythonHelperError`
    on bad input (it does NOT print/exit itself — ``main`` formats it as
    stderr + exit 1). We emulate that here so error-path assertions see rc=1
    and the message on stderr.
    """
    import contextlib
    import io

    from zai_python_helper.cli import build_parser
    from zai_python_helper.errors import ZaiPythonHelperError

    parser = build_parser()
    out, err = io.StringIO(), io.StringIO()
    try:
        args = parser.parse_args(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = args.func(args)
    except ZaiPythonHelperError as e:
        err.write(f"{e}\n")
        rc = 1
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def test_cli_mcp_install_uninstall_list_roundtrip(_isolate_home):
    """The headless CLI installs, lists (installed), then uninstalls.

    Real install/uninstall runs are silent on success (issue #125 byte-parity
    contract; the status lines return as opt-in ``--verbose`` in #128) — the
    on-disk config + exit code are the contract, asserted via ``mcp list``.
    """
    rc, out, _ = _run_cli(
        ["mcp", "install", "zread", "--tool", "claude-code", "--api-key", _KEY]
    )
    assert rc == 0
    assert out == ""

    rc, out, _ = _run_cli(["mcp", "list", "--tool", "claude-code"])
    assert rc == 0
    assert "zread [installed]" in out

    rc, out, _ = _run_cli(["mcp", "uninstall", "zread", "--tool", "claude-code"])
    assert rc == 0
    assert out == ""


def test_cli_mcp_verbose_restores_status_lines(_isolate_home):
    """Opt-in ``--verbose`` (issue #128) restores the status lines silenced in
    #125 — and changes NOTHING else: exit code 0 and the on-disk config are
    identical with and without the flag.

    ``use zai`` does not take the flag (its two pinned lines are the parity
    contract); these three commands print nothing by default and the former
    lines only with the flag.
    """
    argv = ["mcp", "install", "zread", "--tool", "claude-code", "--api-key", _KEY]

    rc, out, _ = _run_cli([*argv, "--verbose"])
    assert rc == 0
    assert "  zread: installed for claude-code" in out

    rc, out, _ = _run_cli([*argv, "--verbose"])
    assert rc == 0
    assert "  zread: already installed (no change) for claude-code" in out

    rc, out, _ = _run_cli(
        ["mcp", "uninstall", "zread", "--tool", "claude-code", "--verbose"]
    )
    assert rc == 0
    assert "  zread: removed from claude-code" in out

    rc, out, _ = _run_cli(
        ["mcp", "uninstall", "zread", "--tool", "claude-code", "--verbose"]
    )
    assert rc == 0
    assert "  zread: not installed (no change) from claude-code" in out


def test_cli_mcp_verbose_matches_silent_run_exactly(_isolate_home):
    """Paired #128 invariant for MCP: two fresh identical installs — one
    silent, one ``--verbose`` — must produce byte-identical config files with
    identical modes, the same exit code, and the same (empty) stderr; only
    stdout differs.
    """
    import os

    from zai_python_helper.mcp import Tool, tool_config_path

    config_path = tool_config_path(Tool.CLAUDE_CODE, _isolate_home)
    install_argv = ["mcp", "install", "zread", "--tool", "claude-code", "--api-key", _KEY]

    results = {}
    for label, extra in (("silent", []), ("verbose", ["--verbose"])):
        rc, out, err = _run_cli([*install_argv, *extra])
        snapshot = {
            p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in sorted(config_path.parent.iterdir())
            if p.is_file()
        }
        results[label] = (rc, snapshot, out, err, os.stat(config_path.parent).st_mode)
        # Reset for the paired run.
        rc2, _, _ = _run_cli(["mcp", "uninstall", "zread", "--tool", "claude-code"])
        assert rc2 == 0

    silent_rc, silent_files, silent_out, silent_err, _ = results["silent"]
    verbose_rc, verbose_files, verbose_out, verbose_err, _ = results["verbose"]

    assert silent_rc == verbose_rc == 0
    assert verbose_files == silent_files
    assert silent_err == verbose_err == ""
    assert silent_out == ""
    assert "  zread: installed for claude-code" in verbose_out


def test_cli_mcp_install_dry_run_redacts_api_key(_isolate_home):
    """dry-run must NEVER print the real --api-key (cycle-review regression).

    The entry shape is shown with a <redacted> placeholder where the key lands
    (env.Z_AI_API_KEY for stdio, headers.Authorization for http). A passed
    ``--api-key`` must not reach stdout in any form.
    """
    secret = "sk-DO-NOT-LEAK-THIS-KEY"
    rc, out, _ = _run_cli(
        ["mcp", "install", "zai-mcp-server", "--tool", "claude-code", "--api-key", secret, "--dry-run"]
    )
    assert rc == 0
    assert secret not in out
    assert "<redacted>" in out
    # The auth field is shown so the user sees WHERE the key lands.
    assert "Z_AI_API_KEY" in out

    # http preset: the Bearer header value must be redacted too.
    rc, out, _ = _run_cli(
        ["mcp", "install", "zread", "--tool", "claude-code", "--api-key", secret, "--dry-run"]
    )
    assert rc == 0
    assert secret not in out
    assert "Authorization" in out


def test_cli_mcp_install_unknown_preset_errors(_isolate_home):
    """An unknown preset id surfaces as a clean error (not a traceback)."""
    rc, _out, err = _run_cli(
        ["mcp", "install", "no-such", "--tool", "claude-code", "--api-key", _KEY]
    )
    assert rc != 0
    assert "Unknown MCP preset" in err


def test_cli_mcp_install_malformed_section_errors_cleanly(_isolate_home):
    """A malformed MCP section surfaces as a clean error, not a traceback.

    install_into_doc fails closed with a ValueError to avoid overwriting the
    user's malformed-but-owned section; the CLI must translate that into the
    project's ZaiPythonHelperError contract (`error: <msg>` + exit 1), not let
    a bare ValueError escape __main__'s error boundary as an uncaught
    traceback. Reproduces the malformed section directly on disk (the pure
    install_into_doc test above does not exercise this CLI path).
    """
    path = tool_config_path(Tool.CLAUDE_CODE, _isolate_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": ["not-an-object"]}), encoding="utf-8")

    rc, _out, err = _run_cli(
        ["mcp", "install", "zread", "--tool", "claude-code", "--api-key", _KEY]
    )
    assert rc != 0
    assert "must be a JSON object" in err
    # Fail-closed: the malformed section must be left untouched, not replaced.
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcpServers": ["not-an-object"]
    }


def test_cli_mcp_uninstall_dry_run_does_not_mutate(_isolate_home):
    """`mcp uninstall --dry-run` must be read-only (cycle-review regression).

    Dry-run is a preview contract across the whole CLI (`use zai`/`use default`
    dry-run never write). The uninstall handler used to ignore ``--dry-run`` and
    delete the entry for real — this guards that it shows a preview and leaves
    the config byte-for-byte unchanged.
    """
    # Install first so there is something to preview removing.
    _run_cli(["mcp", "install", "zread", "--tool", "claude-code", "--api-key", _KEY])
    path = tool_config_path(Tool.CLAUDE_CODE, _isolate_home)
    before = path.read_text(encoding="utf-8")
    assert "zread" in before

    rc, out, _ = _run_cli(
        ["mcp", "uninstall", "zread", "--tool", "claude-code", "--dry-run"]
    )
    assert rc == 0
    # The config must be byte-for-byte unchanged after a dry-run.
    assert path.read_text(encoding="utf-8") == before
    # And the preview must NOT claim it was removed.
    assert "removed" not in out
    assert "would remove" in out or "dry-run" in out.lower()
