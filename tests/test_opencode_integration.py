"""Integration tests for the OpenCode tool: HOME-isolated apply → revert cycle
through the Tool interface and the ownership journal (ADR-004 / ADR-005).

These exercise the OpenCodeTool adapter end-to-end against a tmp HOME: read
state → plan → capture ownership → commit (via the same apply_plan_locked the
CLI uses) → then journal-aware revert → assert restored. No real $HOME, no
network.
"""

from __future__ import annotations

import pytest

from zai_python_helper.backends import JsonBackend
from zai_python_helper.core.planner import FileTag
from zai_python_helper.core.planner import opencode as oc
from zai_python_helper.ownership import OwnershipJournal
from zai_python_helper.patchplan import ProcessLock, apply_plan_locked
from zai_python_helper.paths import Paths
from zai_python_helper.regions import Region
from zai_python_helper.tools import get_tool
from zai_python_helper.tools.opencode import OpenCodeTool

TOKEN = "sk-integration-token"
GLOBAL_NAME = "zai-coding-plan"
CHINA_NAME = "zhipuai-coding-plan"


@pytest.fixture
def tool() -> OpenCodeTool:
    return get_tool("opencode")  # type: ignore[return-value]


def _spec():
    # OpenCode ignores model-mode; pass a minimal valid spec.
    from zai_python_helper.core.domain import ModelMode, ProviderSpec

    return ProviderSpec(base_url="https://api.z.ai/api/anthropic", model_mode=ModelMode.ORIGINAL)


def _read_doc(paths):
    return JsonBackend.read(paths.opencode)


class TestApplyAndRevert:
    def test_use_zai_writes_exact_config(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(_spec(), Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        doc = _read_doc(paths)
        assert doc["provider"][GLOBAL_NAME] == {"options": {"apiKey": TOKEN}}
        assert doc["model"] == "zai-coding-plan/glm-4.6"
        assert doc["small_model"] == "zai-coding-plan/glm-4.5-air"

    def test_use_zai_is_idempotent(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan1 = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan1)
            state2 = tool.read_state(paths)
            plan2 = tool.plan_zai(spec, Region.GLOBAL, state=state2, auth_token=TOKEN)
        assert plan2.is_empty  # second activation: nothing to do

    def test_use_default_restores_prior_via_journal(self, tool, tmp_path):
        """use zai then use default restores the pre-activation state."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        # Seed a foreign provider + a foreign top-level key.
        seed = {
            "$schema": "keep",
            "provider": {"openai": {"options": {"apiKey": "foreign"}}},
            "theme": "dark",
        }
        JsonBackend.write(paths.opencode, seed)

        journal = OwnershipJournal(paths.ownership_json)

        # 1) use zai
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            current = journal.read()
            journal.write(_merge(tool, current, records))
            apply_plan_locked(paths, plan)

        # 2) use default (journal-aware)
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions, _retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read_doc(paths)
        # Coding-plan provider gone; foreign provider + schema + theme restored.
        assert "zai-coding-plan" not in doc.get("provider", {})
        assert doc["provider"] == {"openai": {"options": {"apiKey": "foreign"}}}
        assert doc["$schema"] == "keep"
        assert doc["theme"] == "dark"
        # Our model strings removed (they referenced a coding-plan provider
        # prefix only if it contained "coding-plan"; "zai/glm-4.6" does NOT,
        # so the blind planner keeps them — but journal revert RESTOREs the
        # prior which was absent → they are removed).
        assert "model" not in doc
        assert "small_model" not in doc

    def test_use_default_refuses_when_key_changed_externally(self, tool, tmp_path):
        """If the apiKey changed externally since activation, revert leaves it."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            journal.write(_merge(tool, journal.read(), records))
            apply_plan_locked(paths, plan)

        # External edit: rotate the apiKey out from under us.
        doc = _read_doc(paths)
        doc["provider"][GLOBAL_NAME]["options"]["apiKey"] = "user-rotated"
        JsonBackend.write(paths.opencode, doc)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            decisions, _retired = tool.revert_decisions(journal.read(), state)
            plan = tool.plan_revert(state=state, decisions=decisions)
            apply_plan_locked(paths, plan)

        doc = _read_doc(paths)
        # The externally-set key is NOT clobbered.
        assert doc["provider"][GLOBAL_NAME]["options"]["apiKey"] == "user-rotated"

    def test_independent_of_claude_code(self, tool, tmp_path):
        """Disabling OpenCode does not affect Claude Code files (and vice versa)."""
        paths = Paths.from_home(tmp_path)
        # Claude Code settings exist and are untouched.
        JsonBackend.write(paths.claude_settings, {"env": {"ANTHROPIC_AUTH_TOKEN": "cc-tok"}})

        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        # Claude Code settings round-trip unchanged.
        cc = JsonBackend.read(paths.claude_settings)
        assert cc == {"env": {"ANTHROPIC_AUTH_TOKEN": "cc-tok"}}

    def test_duplicate_state_activation_refused(self, tool, tmp_path):
        """Issue #50 / Bug 4 edge (integration): a duplicate-state seed (BOTH
        regional providers with distinct credentials) must NOT proceed through
        ``use zai``. A region switch would silently destroy one entry because
        the ownership journal keys the apiKey under a single fixed logical name
        and cannot round-trip two regional names. ``plan_zai`` refuses the
        activation (ValidationError) instead of guessing; the on-disk doc is
        left untouched (non-destructive). Both insertion orders refused.

        Here the journal is empty, so NEITHER entry is attributable — the
        ambiguous case the guard is for (issue #61)."""
        from zai_python_helper.errors import ValidationError

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        seed = {
            "$schema": "keep",
            "provider": {
                GLOBAL_NAME: {
                    "options": {"apiKey": "user-global-key"},
                    "baseURL": "https://user.global",
                },
                CHINA_NAME: {
                    "options": {"apiKey": "user-china-key"},
                    "baseURL": "https://user.china",
                    "models": {"glm-4.6": {}},
                },
            },
        }
        JsonBackend.write(paths.opencode, seed)

        # Activating EITHER region is refused from a dual-provider seed.
        for region in (Region.GLOBAL, Region.CHINA):
            with ProcessLock(paths.lock_file):
                state = tool.read_state(paths)
                journal_records = OwnershipJournal(paths.ownership_json).read()
                with pytest.raises(ValidationError):
                    tool.plan_zai(
                        spec,
                        region,
                        state=state,
                        auth_token=TOKEN,
                        journal_records=journal_records,
                    )

        # The seed is left exactly as-is — no silent data loss.
        assert _read_doc(paths) == seed

    def _use_default(self, tool, paths):
        """Run the CLI's ``use default`` path (journal-aware plan_revert)."""
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            journal_records = OwnershipJournal(paths.ownership_json).read()
            decisions, _ = tool.revert_decisions(journal_records, state)
            plan = tool.plan_revert(
                state=state, decisions=decisions, journal_records=journal_records
            )
            apply_plan_locked(paths, plan)
            return decisions

    def test_use_default_clears_duplicate_when_our_value_intact(
        self, tool, tmp_path
    ):
        """Recovery contract, branch 1: when one entry still holds exactly
        what we wrote (value matches the journal ``set_hash``), ``use default``
        RESTOREs it away and the duplicate is cleared — the user is NOT stuck
        and must not be told to hand-edit JSON.

        Sequence: clean GLOBAL activation (journal owns provider.apiKey), then
        the user hand-adds a china provider. Our global entry is provably ours,
        so revert removes it and leaves the user's china entry untouched."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            apply_plan_locked(paths, plan)
        journal = OwnershipJournal(paths.ownership_json)
        journal.write(_merge(tool, journal.read(), records))

        # User hand-adds the china provider -> duplicate state.
        doc = _read_doc(paths)
        doc["provider"][CHINA_NAME] = {"options": {"apiKey": "user-china-key"}}
        JsonBackend.write(paths.opencode, doc)

        decisions = self._use_default(tool, paths)
        assert decisions["provider.apiKey"].action.name == "RESTORE"

        after = _read_doc(paths)
        assert not oc.has_duplicate_regional_providers(after)
        # Our entry is gone; the user's china key survives untouched.
        assert GLOBAL_NAME not in after["provider"]
        assert after["provider"][CHINA_NAME] == {"options": {"apiKey": "user-china-key"}}

        # ...and `use zai` now succeeds — recovery is complete.
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)

    def test_use_default_refuses_when_both_regional_entries_match_our_value(
        self, tool, tmp_path
    ):
        """Two matching regional entries are ambiguous and must both survive."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            apply_plan_locked(paths, plan)
        journal = OwnershipJournal(paths.ownership_json)
        journal.write(_merge(tool, journal.read(), records))

        doc = _read_doc(paths)
        doc["provider"][CHINA_NAME] = {"options": {"apiKey": TOKEN}}
        JsonBackend.write(paths.opencode, doc)

        decisions = self._use_default(tool, paths)
        assert decisions["provider.apiKey"].action.name == "REFUSE"
        after = _read_doc(paths)
        assert after["provider"][GLOBAL_NAME]["options"]["apiKey"] == TOKEN
        assert after["provider"][CHINA_NAME]["options"]["apiKey"] == TOKEN

    def test_use_default_cannot_clear_duplicate_when_unowned(self, tool, tmp_path):
        """Recovery contract, branch 2: when NO entry's value matches the
        journal (both user-authored here), every decision is REFUSE, the doc
        round-trips byte-identical, and ``use zai`` stays refused — a hand edit
        is the only exit.

        This is the seed the guard exists for; pinning both branches keeps the
        docstring/error message from generalizing either one to the other."""
        from zai_python_helper.errors import ValidationError

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        seed = {
            "provider": {
                GLOBAL_NAME: {"options": {"apiKey": "user-global-key"}},
                CHINA_NAME: {"options": {"apiKey": "user-china-key"}},
            },
        }
        JsonBackend.write(paths.opencode, seed)

        self._use_default(tool, paths)

        # `use default` changed nothing — the duplicate state survives.
        assert _read_doc(paths) == seed

        # ...and `use zai` is therefore still refused.
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            journal_records = OwnershipJournal(paths.ownership_json).read()
            with pytest.raises(ValidationError):
                tool.plan_zai(
                    spec,
                    Region.GLOBAL,
                    state=state,
                    auth_token=TOKEN,
                    journal_records=journal_records,
                )

    def test_duplicate_state_self_heals_when_our_entry_is_attributable(
        self, tool, tmp_path
    ):
        """Issue #61, the behavior fix: a duplicate-state doc where the journal
        PROVES one entry is ours is NOT ambiguous, so ``use zai`` proceeds and
        self-heals it in ONE shot (this is what #41 shipped and #57's
        unconditional guard removed).

        Sequence mirrors the reproducer in the issue: clean GLOBAL activation
        (journal owns provider.apiKey = hash of our token), then the user
        hand-adds a china provider. Activating again must drop OUR entry, keep
        going, and leave a single regional provider — no dead-end."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        # 1) Clean activation — the journal now attributes our entry by value.
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            apply_plan_locked(paths, plan)
        journal.write(_merge(tool, journal.read(), records))

        # 2) User hand-adds a china provider with their OWN credential.
        doc = _read_doc(paths)
        doc["provider"][CHINA_NAME] = {"options": {"apiKey": "user-china-key"}}
        JsonBackend.write(paths.opencode, doc)
        assert oc.has_duplicate_regional_providers(_read_doc(paths))

        # 3) `use zai` again — no refusal, and the duplicate is healed.
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            journal_records = journal.read()
            plan = tool.plan_zai(
                spec,
                Region.GLOBAL,
                state=state,
                auth_token=TOKEN,
                journal_records=journal_records,
            )
            records = tool.extract_takeover(
                plan, prior_state=state, spec=spec, journal_records=journal_records
            )
            apply_plan_locked(paths, plan)
        journal.write(_merge(tool, journal.read(), records))

        after = _read_doc(paths)
        assert not oc.has_duplicate_regional_providers(after)
        assert list(after["provider"].keys()) == [GLOBAL_NAME]
        assert after["provider"][GLOBAL_NAME]["options"]["apiKey"] == TOKEN

        # Verify the unattributed-entry detection logic used by the CLI warning.
        # On this exact prior state, the warning must name CHINA_NAME as the
        # entry that was removed (it is the one the journal does NOT own).
        owned = oc.owned_regional_provider_name(doc, journal_records)
        unattributed = [n for n in doc.get("provider", {}) if n != owned]
        assert owned == GLOBAL_NAME
        assert unattributed == [CHINA_NAME]

    def test_self_heal_takeover_prior_is_our_value_not_the_users(
        self, tool, tmp_path
    ):
        """The ownership capture on a self-heal must read the PRIOR apiKey off
        OUR entry, not off whichever came first in dict order. If it recorded
        the user's key as the prior, a later ``use default`` would RESTORE the
        user's credential into our provider slot — writing their secret to a
        place they never put it."""
        from zai_python_helper.ownership import hash_value

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            apply_plan_locked(paths, plan)
        journal.write(_merge(tool, journal.read(), records))

        # The user's china entry is inserted FIRST in dict order, so a
        # first-match resolution would read THEIR key as our prior.
        doc = _read_doc(paths)
        JsonBackend.write(
            paths.opencode,
            {
                "provider": {
                    CHINA_NAME: {"options": {"apiKey": "user-china-key"}},
                    GLOBAL_NAME: doc["provider"][GLOBAL_NAME],
                },
                "model": doc["model"],
                "small_model": doc["small_model"],
            },
        )

        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            journal_records = journal.read()
            plan = tool.plan_zai(
                spec,
                Region.GLOBAL,
                state=state,
                auth_token="sk-rotated-token",
                journal_records=journal_records,
            )
            records = tool.extract_takeover(
                plan, prior_state=state, spec=spec, journal_records=journal_records
            )

        prior_by_key = {key: prior for key, prior, _present, _h in records}
        # The captured prior is OUR previous token, never the user's key.
        assert prior_by_key["provider.apiKey"] == TOKEN
        assert prior_by_key["provider.apiKey"] != "user-china-key"
        # And the recorded set_hash is the new value we wrote.
        set_hash_by_key = {key: h for key, _p, _pp, h in records}
        assert set_hash_by_key["provider.apiKey"] == hash_value("sk-rotated-token")

    def test_revert_acts_on_our_entry_on_a_duplicate_doc(self, tool, tmp_path):
        """``use default`` on a duplicate doc must revert OUR entry, even when
        the user's regional entry comes first in dict order (issue #61:
        ``plan_revert`` inferred the region by first-match). The user's entry
        must survive byte-identical."""
        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        # Activate CHINA so our entry is the china name...
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.CHINA, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            apply_plan_locked(paths, plan)
        journal.write(_merge(tool, journal.read(), records))

        # ...then hand-add a GLOBAL entry FIRST in dict order, so first-match
        # resolution would infer GLOBAL and revert the wrong entry.
        doc = _read_doc(paths)
        JsonBackend.write(
            paths.opencode,
            {
                "provider": {
                    GLOBAL_NAME: {"options": {"apiKey": "user-global-key"}},
                    CHINA_NAME: doc["provider"][CHINA_NAME],
                },
                "model": doc["model"],
                "small_model": doc["small_model"],
            },
        )

        decisions = self._use_default(tool, paths)
        assert decisions["provider.apiKey"].action.name == "RESTORE"

        after = _read_doc(paths)
        # OUR china entry is gone; the user's global entry is untouched.
        assert CHINA_NAME not in after.get("provider", {})
        assert after["provider"][GLOBAL_NAME] == {"options": {"apiKey": "user-global-key"}}
        assert not oc.has_duplicate_regional_providers(after)


# ---------------------------------------------------------------------------
# helper: merge takeover records into the journal (mirrors cli._merge_takeover)
# ---------------------------------------------------------------------------


def _merge(tool, current, records):
    from zai_python_helper.ownership import take_over

    merged = current
    for key, prior_value, prior_present, set_hash in records:
        merged = take_over(merged, tool.name, key, prior_value, prior_present, set_hash)
    return merged


class TestStatusRowOnDuplicateState:
    """``status_row`` reads the journal so it reports OUR entry and surfaces
    the duplicate state itself (issue #61). Previously it resolved the provider
    by dict order and rendered a normal-looking row, so a user in a dead-end
    state learned about it only when ``use zai`` errored."""

    @staticmethod
    def _seed(paths, *, first: str, second: str, keys: dict):
        JsonBackend.write(
            paths.opencode,
            {
                "provider": {
                    first: {"options": {"apiKey": keys[first]}},
                    second: {"options": {"apiKey": keys[second]}},
                },
                "model": f"{first}/glm-4.6",
            },
        )

    def test_reports_our_provider_not_the_first_in_dict_order(self, tool, tmp_path):
        from zai_python_helper.ownership import hash_value

        paths = Paths.from_home(tmp_path)
        # The USER's global entry comes first; OURS is the china one.
        self._seed(
            paths,
            first=GLOBAL_NAME,
            second=CHINA_NAME,
            keys={GLOBAL_NAME: "user-global-key", CHINA_NAME: "helper-wrote-this"},
        )
        OwnershipJournal(paths.ownership_json).write(
            {
                "opencode": {
                    "provider.apiKey": {
                        "prior_value": None,
                        "prior_present": False,
                        "set_hash": hash_value("helper-wrote-this"),
                        "active": True,
                    }
                }
            }
        )

        row = tool.status_row(paths)
        assert f"provider={CHINA_NAME}" in row.detail
        assert row.region is Region.CHINA

    def test_detail_flags_the_duplicate_state(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        self._seed(
            paths,
            first=GLOBAL_NAME,
            second=CHINA_NAME,
            keys={GLOBAL_NAME: "user-global-key", CHINA_NAME: "user-china-key"},
        )

        row = tool.status_row(paths)
        assert "DUPLICATE-REGIONAL-PROVIDERS" in row.detail
        # Unattributable → the row says a hand edit is required.
        assert "hand edit required" in row.detail

    def test_detail_omits_hand_edit_when_ours_is_attributable(self, tool, tmp_path):
        from zai_python_helper.ownership import hash_value

        paths = Paths.from_home(tmp_path)
        self._seed(
            paths,
            first=GLOBAL_NAME,
            second=CHINA_NAME,
            keys={GLOBAL_NAME: "helper-wrote-this", CHINA_NAME: "user-china-key"},
        )
        OwnershipJournal(paths.ownership_json).write(
            {
                "opencode": {
                    "provider.apiKey": {
                        "prior_value": None,
                        "prior_present": False,
                        "set_hash": hash_value("helper-wrote-this"),
                        "active": True,
                    }
                }
            }
        )

        row = tool.status_row(paths)
        assert "DUPLICATE-REGIONAL-PROVIDERS" in row.detail
        # `use zai` self-heals this one — no hand edit to advertise.
        assert "hand edit required" not in row.detail

    def test_no_duplicate_marker_on_a_clean_doc(self, tool, tmp_path):
        paths = Paths.from_home(tmp_path)
        JsonBackend.write(
            paths.opencode,
            {
                "provider": {GLOBAL_NAME: {"options": {"apiKey": "k"}}},
                "model": f"{GLOBAL_NAME}/glm-4.6",
            },
        )
        row = tool.status_row(paths)
        assert "DUPLICATE" not in row.detail


class TestSelfHealDestructionWarning:
    """The CLI must warn — in BOTH ``--dry-run`` preview and real activation —
    when a self-heal is about to irreversibly drop a non-attributed regional
    entry (Codex round 3 finding: the warning was previously wired only into
    the real-activation path, so ``--dry-run`` silently under-reported what
    the real run would do)."""

    def test_warns_on_both_dry_run_and_real_activation(self, tool, tmp_path, capsys):
        from zai_python_helper.cli import _warn_self_heal_destruction

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        journal = OwnershipJournal(paths.ownership_json)

        # Clean activation, then hand-add a china provider (mirrors the
        # self-heal fixture above).
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            records = tool.extract_takeover(plan, prior_state=state, spec=spec)
            apply_plan_locked(paths, plan)
        journal.write(_merge(tool, journal.read(), records))

        doc = _read_doc(paths)
        doc["provider"][CHINA_NAME] = {"options": {"apiKey": "user-china-key"}}
        JsonBackend.write(paths.opencode, doc)

        state = tool.read_state(paths)
        journal_records = journal.read()
        plan = tool.plan_zai(
            spec,
            Region.GLOBAL,
            state=state,
            auth_token=TOKEN,
            journal_records=journal_records,
        )

        # Same call the CLI makes on BOTH the dry-run preview and the real
        # activation path — assert it warns identically either way.
        for _ in range(2):
            capsys.readouterr()
            _warn_self_heal_destruction(
                state.get(FileTag.OPENCODE), journal_records, plan
            )
            out = capsys.readouterr().out
            assert "warning" in out
            assert CHINA_NAME in out
            assert "irreversible" in out
            # Foreign (non-managed) providers must never be named as removed.
            assert GLOBAL_NAME not in out.split("warning")[1].split("were removed")[0]

    def test_no_warning_when_state_is_not_duplicate(self, tool, tmp_path, capsys):
        from zai_python_helper.cli import _warn_self_heal_destruction

        paths = Paths.from_home(tmp_path)
        spec = _spec()
        with ProcessLock(paths.lock_file):
            state = tool.read_state(paths)
            plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
            apply_plan_locked(paths, plan)

        state = tool.read_state(paths)
        plan = tool.plan_zai(spec, Region.GLOBAL, state=state, auth_token=TOKEN)
        _warn_self_heal_destruction(state.get(FileTag.OPENCODE), {}, plan)
        assert capsys.readouterr().out == ""
