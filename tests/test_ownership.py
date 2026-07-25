"""Unit tests for the ownership journal (ADR-004).

Covers the pure operations (:func:`take_over` / :func:`revert` /
:func:`hash_value`) and the IO seam (:class:`OwnershipJournal`):

- take_over → revert RESTORE when the value is still ours;
- revert REFUSE when the value changed externally;
- revert CLEAR when we have no journal entry;
- ownership-by-removal (set_hash None) → RESTORE prior unconditionally;
- the journal file is written atomically at mode 0600 (credentials may live
  in it) and round-trips through read.
"""

from __future__ import annotations

import json

import pytest

from zai_python_helper.ownership import (
    OwnershipJournal,
    RevertAction,
    hash_value,
    revert,
    take_over,
)

TOOL = "claude_code"


# ---------------------------------------------------------------------------
# Pure: hash_value
# ---------------------------------------------------------------------------


def test_hash_value_is_stable_and_sha256():
    """hash_value is deterministic SHA-256 (16^64 hex)."""
    h = hash_value("sk-secret")
    assert h == hash_value("sk-secret")
    assert h != hash_value("sk-other")
    # SHA-256 hex digest length.
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Pure: take_over
# ---------------------------------------------------------------------------


def test_take_over_records_prior_presence_and_set_hash():
    """take_over stores the prior value/presence + hash of the set value."""
    records: dict = {}
    out = take_over(
        records,
        TOOL,
        "ANTHROPIC_AUTH_TOKEN",
        prior_value=None,
        prior_present=False,
        set_hash=hash_value("sk-new"),
    )
    # Input is NOT mutated (pure).
    assert records == {}
    entry = out[TOOL]["ANTHROPIC_AUTH_TOKEN"]
    assert entry["prior_value"] is None
    assert entry["prior_present"] is False
    assert entry["set_hash"] == hash_value("sk-new")


def test_take_over_preserves_other_keys_and_tools():
    """take_over never clobbers existing journal entries."""
    records = {TOOL: {"OTHER_KEY": {"prior_value": "x", "prior_present": True, "set_hash": "h"}}}
    out = take_over(
        records,
        TOOL,
        "ANTHROPIC_AUTH_TOKEN",
        prior_value="sk-old",
        prior_present=True,
        set_hash=hash_value("sk-new"),
    )
    # The OTHER_KEY entry survives untouched.
    assert out[TOOL]["OTHER_KEY"]["prior_value"] == "x"
    assert out[TOOL]["ANTHROPIC_AUTH_TOKEN"]["prior_value"] == "sk-old"
    # And other tools' buckets survive too.
    out2 = take_over(out, "opencode", "X", None, False, hash_value("v"))
    assert out2[TOOL]["ANTHROPIC_AUTH_TOKEN"]["prior_value"] == "sk-old"
    assert out2["opencode"]["X"]["set_hash"] == hash_value("v")


def test_take_over_refreshes_on_genuine_value_change():
    """A take_over with a DIFFERENT set_hash records the new prior (rotation).

    Distinct from the repeat-activation idempotency case: this is a real value
    change (e.g. a rotated token), so the new prior is the value present now.
    """
    records = take_over({}, TOOL, "K", "old", True, hash_value("first"))
    records = take_over(records, TOOL, "K", "second", True, hash_value("renewed"))
    assert records[TOOL]["K"]["prior_value"] == "second"
    assert records[TOOL]["K"]["set_hash"] == hash_value("renewed")


# ---------------------------------------------------------------------------
# Pure: revert — the three ADR-004 cases
# ---------------------------------------------------------------------------


def test_revert_restore_when_value_still_ours():
    """current_value matches set_hash → RESTORE the journaled prior."""
    records = take_over({}, TOOL, "K", prior_value="sk-old", prior_present=True,
                        set_hash=hash_value("sk-new"))
    decision = revert(records, TOOL, "K", current_value="sk-new")
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_value == "sk-old"
    assert decision.prior_present is True


def test_revert_restore_re_absents_when_prior_was_absent():
    """RESTORE with prior_present=False re-removes the key (not empty string)."""
    records = take_over({}, TOOL, "K", prior_value=None, prior_present=False,
                        set_hash=hash_value("sk-new"))
    decision = revert(records, TOOL, "K", current_value="sk-new")
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_present is False
    assert decision.prior_value is None


def test_revert_refuse_on_external_change():
    """current_value differs from set_hash → REFUSE, do not overwrite."""
    records = take_over({}, TOOL, "K", prior_value="sk-old", prior_present=True,
                        set_hash=hash_value("sk-new"))
    decision = revert(records, TOOL, "K", current_value="sk-edited-by-user")
    assert decision.action == RevertAction.REFUSE
    assert "changed externally" in decision.reason


def test_revert_refuse_when_key_now_absent_but_we_set_it():
    """We set a value; the key is now absent (user cleared it) → REFUSE.

    The value is not the one we set (it's gone), so we decline to act — the
    user removed it intentionally. We must not silently re-create it.
    """
    records = take_over({}, TOOL, "K", prior_value="sk-old", prior_present=True,
                        set_hash=hash_value("sk-new"))
    decision = revert(records, TOOL, "K", current_value=None)
    assert decision.action == RevertAction.REFUSE


def test_revert_refuses_when_no_entry():
    """No journal entry → REFUSE (we cannot prove ownership; do not delete).

    S3 regression fix (Codex finding #3): missing provenance must NOT fall
    back to blind deletion. ``use default`` with no prior ``use zai`` must
    not wipe a key the user configured by hand. The honest inverse when we
    have no provenance is to leave the value untouched + warn.
    """
    decision = revert({}, TOOL, "K", current_value="whatever")
    assert decision.action == RevertAction.REFUSE
    assert decision.prior_present is False
    assert "cannot prove ownership" in decision.reason


def test_revert_refuses_for_unrelated_tool():
    """An entry under another tool does not count for this tool → REFUSE."""
    records = {"opencode": {"K": {"prior_value": "x", "prior_present": True, "set_hash": "h"}}}
    decision = revert(records, TOOL, "K", current_value="x")
    assert decision.action == RevertAction.REFUSE


def test_revert_ownership_by_removal_restores_while_absent():
    """set_hash None (we took ownership by REMOVING the key) → RESTORE prior,
    but ONLY while the key is still absent.

    Used for ANTHROPIC_API_KEY: ``use zai`` removes it, so revert restores the
    user's original key — but only if the key is still absent. If a value has
    since appeared (the user added a new key), that is an external change and
    we REFUSE rather than clobber it (S3 regression fix, Codex finding #2).
    """
    records = take_over({}, TOOL, "ANTHROPIC_API_KEY", prior_value="sk-original",
                        prior_present=True, set_hash=None)
    # Key is currently absent (we removed it). Restore the original.
    decision = revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_value == "sk-original"
    assert decision.prior_present is True


def test_revert_ownership_by_removal_refuses_when_value_reappeared():
    """If a value reappeared after our removal, REFUSE — do not clobber it.

    S3 regression fix (Codex finding #2): ``use zai`` removes an old API key;
    the user later adds a NEW one. ``use default`` must NOT silently replace
    the new key with the stale prior — the appearance of a value is an
    external change.
    """
    records = take_over({}, TOOL, "ANTHROPIC_API_KEY", prior_value="sk-old",
                        prior_present=True, set_hash=None)
    decision = revert(records, TOOL, "ANTHROPIC_API_KEY", current_value="sk-user-added-new")
    assert decision.action == RevertAction.REFUSE
    assert "reappeared" in decision.reason


def test_take_over_preserves_restore_point_on_repeat_activation():
    """Re-activating the SAME value keeps the ORIGINAL prior (idempotent).

    S3 regression fix (Codex finding #1): P→Z→Z must not overwrite the
    journal's prior=P on the second activation.
    """
    zai_hash = hash_value("sk-zai")
    # First activation: prior was the user's original P.
    records = take_over({}, TOOL, "K", prior_value="P", prior_present=True,
                        set_hash=zai_hash)
    # Second activation of the SAME value: prior would now be the current
    # value "sk-zai" if we blindly rewrote — but we must keep the original "P".
    records = take_over(records, TOOL, "K", prior_value="sk-zai", prior_present=True,
                        set_hash=zai_hash)
    assert records[TOOL]["K"]["prior_value"] == "P"  # original restore point kept


def test_take_over_preserves_restore_point_across_token_rotation():
    """P→Z1→Z2 (rotation, no external drift) keeps the ORIGINAL prior P.

    S3 regression fix (Codex finding, cycle 3): rotating the Z.ai token must
    NOT replace the restore point with the PREVIOUS Z.ai token. The live value
    after Z1 is Z1 (still our value — no drift), so a rotation to Z2 advances
    only ``set_hash`` and keeps the original prior=P. A later ``use default``
    then restores P, not a stale Z1.
    """
    z1, z2, p = "sk-zai-1", "sk-zai-2", "sk-user-original-P"
    records = take_over({}, TOOL, "K", prior_value=p, prior_present=True,
                        set_hash=hash_value(z1))
    # Rotate: live value is now z1 (what we set), new set_hash is z2.
    records = take_over(records, TOOL, "K", prior_value=z1, prior_present=True,
                        set_hash=hash_value(z2))
    assert records[TOOL]["K"]["prior_value"] == p  # original restore point kept
    assert records[TOOL]["K"]["set_hash"] == hash_value(z2)  # hash advanced
    # And revert of z2 restores the ORIGINAL p, not the stale z1.
    decision = revert(records, TOOL, "K", current_value=z2)
    assert decision.action.name == "RESTORE"
    assert decision.prior_value == p


def test_take_over_starts_new_restore_point_on_external_drift():
    """If the live value drifted from our set, start a fresh restore point.

    The no-drift rotation guard preserves the original prior ONLY while the
    live value is still ours. If the user (or another tool) changed it to a
    value we never set, we cannot keep the old prior — the current value is a
    genuine new starting point.
    """
    records = take_over({}, TOOL, "K", prior_value="P", prior_present=True,
                        set_hash=hash_value("sk-zai"))
    # Live value drifted to something we never set ("drifted") → new prior.
    records = take_over(records, TOOL, "K", prior_value="drifted", prior_present=True,
                        set_hash=hash_value("sk-zai-new"))
    assert records[TOOL]["K"]["prior_value"] == "drifted"
    assert records[TOOL]["K"]["set_hash"] == hash_value("sk-zai-new")


def test_revert_decision_is_frozen():
    """RevertDecision is a frozen dataclass (tamper-resistant)."""
    import dataclasses

    d = revert({}, TOOL, "K", current_value=None)
    with pytest.raises(dataclasses.FrozenInstanceError):  # pragma: no cover
        d.action = RevertAction.CLEAR  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Full round-trip: take_over → revert (the headline ADR-004 scenario)
# ---------------------------------------------------------------------------


def test_headline_round_trip_take_over_then_revert_matches():
    """The core S3 guarantee: set a value, then restore the original via revert."""
    original = "sk-user-original"
    activated = "sk-zai-set-token"

    records = take_over({}, TOOL, "ANTHROPIC_AUTH_TOKEN",
                        prior_value=original, prior_present=True,
                        set_hash=hash_value(activated))
    # The value is still the one we set → restore the original.
    decision = revert(records, TOOL, "ANTHROPIC_AUTH_TOKEN", current_value=activated)
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_value == original


# ---------------------------------------------------------------------------
# IO seam: OwnershipJournal (0600, atomic, round-trip)
# ---------------------------------------------------------------------------


class TestOwnershipJournalIO:
    def test_read_returns_empty_dict_when_absent(self, tmp_path):
        journal = OwnershipJournal(tmp_path / "ownership.json")
        assert journal.read() == {}

    def test_write_then_read_round_trips(self, tmp_path):
        journal = OwnershipJournal(tmp_path / "ownership.json")
        records = take_over({}, TOOL, "K", "sk-old", True, hash_value("sk-new"))
        journal.write(records)
        assert journal.read() == records

    def test_write_creates_file_mode_0600(self, tmp_path):
        """The journal may hold credentials → mode 0600 (issue #4 acceptance)."""
        path = tmp_path / "ownership.json"
        OwnershipJournal(path).write(
            take_over({}, TOOL, "ANTHROPIC_AUTH_TOKEN", "secret", True, hash_value("v"))
        )
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_write_creates_parent_dir(self, tmp_path):
        """Writing creates ~/.zai-python-helper/ if missing (no pre-mkdir needed)."""
        path = tmp_path / "nested" / "dir" / "ownership.json"
        OwnershipJournal(path).write({})
        assert path.exists()

    def test_read_empty_file_is_empty_dict(self, tmp_path):
        path = tmp_path / "ownership.json"
        path.write_text("")
        assert OwnershipJournal(path).read() == {}

    def test_read_malformed_raises_configuration_error(self, tmp_path):
        """A corrupted journal is a reportable error, not a bare crash."""
        from zai_python_helper.errors import ConfigurationError

        path = tmp_path / "ownership.json"
        path.write_text("{not valid json")
        with pytest.raises(ConfigurationError):
            OwnershipJournal(path).read()

    def test_read_non_object_top_level_raises(self, tmp_path):
        from zai_python_helper.errors import ConfigurationError

        path = tmp_path / "ownership.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ConfigurationError):
            OwnershipJournal(path).read()

    def test_write_replaces_existing_atomically(self, tmp_path):
        """A write replaces an existing journal wholesale with the given dict.

        write() persists EXACTLY the dict it is passed (the caller merges
        take_over results upstream); it does not merge into existing content.
        A second write with a fresh single-key journal therefore leaves only
        that key.
        """
        path = tmp_path / "ownership.json"
        journal = OwnershipJournal(path)
        journal.write(take_over({}, TOOL, "A", "1", True, hash_value("1")))
        journal.write(take_over({}, TOOL, "B", "2", True, hash_value("2")))
        read_back = journal.read()
        assert "A" not in read_back[TOOL]
        assert read_back[TOOL]["B"]["prior_value"] == "2"

    def test_persisted_json_is_indented_and_utf8(self, tmp_path):
        """The on-disk form is pretty JSON (matches JsonBackend conventions)."""
        path = tmp_path / "ownership.json"
        OwnershipJournal(path).write(
            take_over({}, TOOL, "K", "v", True, hash_value("v"))
        )
        text = path.read_text(encoding="utf-8")
        # Indented (multi-line) + parseable.
        assert "\n" in text
        assert json.loads(text)[TOOL]["K"]["prior_value"] == "v"


def test_paths_ownership_json_is_0600_capable_via_paths(_isolate_home):
    """The journal path resolves under the isolated HOME via Paths."""
    from zai_python_helper.paths import Paths

    paths = Paths.from_home(_isolate_home)
    assert paths.ownership_json == _isolate_home / ".zai-python-helper" / "ownership.json"
    assert not paths.ownership_json.exists()  # no IO yet
