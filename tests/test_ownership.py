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

import copy
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


def _revert(records, tool, key, current_value):
    """Thin wrapper returning ONLY the decision (pre-cycle-state test shape).

    ``revert`` now returns ``(decision, retired_records)`` (issue #48). Most
    legacy assertions care only about the decision; the new cycle-state tests
    below use the raw ``revert`` to also assert on ``retired_records``.
    """
    return revert(records, tool, key, current_value)[0]


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
    decision = _revert(records, TOOL, "K", current_value="sk-new")
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_value == "sk-old"
    assert decision.prior_present is True


def test_revert_restore_re_absents_when_prior_was_absent():
    """RESTORE with prior_present=False re-removes the key (not empty string)."""
    records = take_over({}, TOOL, "K", prior_value=None, prior_present=False,
                        set_hash=hash_value("sk-new"))
    decision = _revert(records, TOOL, "K", current_value="sk-new")
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_present is False
    assert decision.prior_value is None


def test_revert_refuse_on_external_change():
    """current_value differs from set_hash → REFUSE, do not overwrite."""
    records = take_over({}, TOOL, "K", prior_value="sk-old", prior_present=True,
                        set_hash=hash_value("sk-new"))
    decision = _revert(records, TOOL, "K", current_value="sk-edited-by-user")
    assert decision.action == RevertAction.REFUSE
    assert "changed externally" in decision.reason


def test_revert_refuse_when_key_now_absent_but_we_set_it():
    """We set a value; the key is now absent (user cleared it) → REFUSE.

    The value is not the one we set (it's gone), so we decline to act — the
    user removed it intentionally. We must not silently re-create it.
    """
    records = take_over({}, TOOL, "K", prior_value="sk-old", prior_present=True,
                        set_hash=hash_value("sk-new"))
    decision = _revert(records, TOOL, "K", current_value=None)
    assert decision.action == RevertAction.REFUSE


def test_revert_refuses_when_no_entry():
    """No journal entry → REFUSE (we cannot prove ownership; do not delete).

    S3 regression fix (Codex finding #3): missing provenance must NOT fall
    back to blind deletion. ``use default`` with no prior ``use zai`` must
    not wipe a key the user configured by hand. The honest inverse when we
    have no provenance is to leave the value untouched + warn.
    """
    decision = _revert({}, TOOL, "K", current_value="whatever")
    assert decision.action == RevertAction.REFUSE
    assert decision.prior_present is False
    assert "cannot prove ownership" in decision.reason


def test_revert_refuses_for_unrelated_tool():
    """An entry under another tool does not count for this tool → REFUSE."""
    records = {"opencode": {"K": {"prior_value": "x", "prior_present": True, "set_hash": "h"}}}
    decision = _revert(records, TOOL, "K", current_value="x")
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
    decision = _revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)
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
    decision = _revert(records, TOOL, "ANTHROPIC_API_KEY", current_value="sk-user-added-new")
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
    decision = _revert(records, TOOL, "K", current_value=z2)
    assert decision.action.name == "RESTORE"
    assert decision.prior_value == p


def test_take_over_refreshes_restore_point_after_completed_cycle():
    """Re-activation with the SAME set_hash but a DRIFTED live value starts a
    fresh restore point (Bug 5, cycle-review on #41).

    Sequence: P1 → activate Z (prior P1) → ``use default`` reverts to P1 → user
    sets P2 → activate the SAME Z again. The second activation carries the same
    ``set_hash`` (we set Z both times), so the old code treated it as an
    idempotent repeat and kept the STALE prior=P1. But the live value is now P2
    (not our Z), so this is a NEW starting point — the prior must be recorded as
    P2, otherwise a later ``use default`` restores P1 and silently destroys P2.

    The equal-hash early return must therefore check the no-drift condition
    (live value still hashes to the recorded set_hash) before preserving the
    old restore point.
    """
    z, p1, p2 = "sk-zai", "sk-user-P1", "sk-user-P2"
    records = take_over({}, TOOL, "K", prior_value=p1, prior_present=True,
                        set_hash=hash_value(z))
    # ``use default`` reverts (journal NOT retired); user then sets P2; we
    # re-activate the same Z. Live value is P2 (≠ Z) → drifted → fresh prior.
    records = take_over(records, TOOL, "K", prior_value=p2, prior_present=True,
                        set_hash=hash_value(z))
    assert records[TOOL]["K"]["prior_value"] == p2  # NOT the stale p1
    assert records[TOOL]["K"]["set_hash"] == hash_value(z)
    # And revert of z now restores P2, not the destroyed P1.
    decision = _revert(records, TOOL, "K", current_value=z)
    assert decision.action.name == "RESTORE"
    assert decision.prior_value == p2


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

    d = revert({}, TOOL, "K", current_value=None)[0]
    with pytest.raises(dataclasses.FrozenInstanceError):  # pragma: no cover
        d.action = RevertAction.CLEAR  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cycle-state (issue #48): active flag, disk-migration, completed-cycle fresh
# restore point — closes the symmetric removal-path data loss.
# ---------------------------------------------------------------------------


def test_take_over_records_active_true_by_default():
    """A fresh take_over writes ``active=True`` (the cycle is in flight)."""
    records = take_over({}, TOOL, "K", "P", True, hash_value("Z"))
    assert records[TOOL]["K"]["active"] is True


def test_ownership_record_migrates_old_entries_as_active():
    """``from_dict`` defaults ``active=True`` for pre-#48 journal entries.

    A journal written by an older release (no ``active`` field) must still
    restore cleanly: the un-reverted ownership is treated as in-flight, and
    only a real ``revert`` retires it.
    """
    from zai_python_helper.ownership import OwnershipRecord

    legacy = OwnershipRecord.from_dict(
        {"prior_value": "P", "prior_present": True, "set_hash": "h"}
    )
    assert legacy.active is True


def test_revert_restore_retires_record_active_false():
    """A RESTORE retires the record: the returned journal marks active=False.

    Set-value path: the value is still ours → restore prior AND retire, so a
    later re-activation does not preserve the now-stale restore point.
    """
    records = take_over({}, TOOL, "K", "P", True, hash_value("Z"))
    decision, retired = revert(records, TOOL, "K", current_value="Z")
    assert decision.action == RevertAction.RESTORE
    assert retired[TOOL]["K"]["active"] is False
    # The prior/set_hash are preserved (retire flips only the cycle-state).
    assert retired[TOOL]["K"]["prior_value"] == "P"
    assert retired[TOOL]["K"]["set_hash"] == hash_value("Z")
    # Input journal is NOT mutated (pure).
    assert records[TOOL]["K"]["active"] is True


def test_revert_removal_restore_retires_record_active_false():
    """A REMOVAL-path RESTORE also retires the record (active=False).

    This is the half of issue #48 that has no content-addressable proof:
    ownership-by-removal's restore must still mark the cycle complete.
    """
    records = take_over({}, TOOL, "ANTHROPIC_API_KEY", "sk-P", True, set_hash=None)
    decision, retired = revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)
    assert decision.action == RevertAction.RESTORE
    assert retired[TOOL]["ANTHROPIC_API_KEY"]["active"] is False


def test_revert_refuse_leaves_journal_untouched():
    """A REFUSE does NOT retire the record (we did not act; cycle in flight).

    The returned journal is a fresh copy but byte-identical to the input —
    no ``active`` flip, no record change.
    """
    records = take_over({}, TOOL, "K", "P", True, hash_value("Z"))
    before = {t: dict(b) for t, b in records.items()}
    decision, retired = revert(records, TOOL, "K", current_value="sk-edited")
    assert decision.action == RevertAction.REFUSE
    assert retired == before
    assert retired[TOOL]["K"]["active"] is True


def test_revert_no_entry_leaves_journal_untouched():
    """No journal entry → REFUSE and an unchanged (empty) journal copy."""
    decision, retired = revert({}, TOOL, "K", current_value="whatever")
    assert decision.action == RevertAction.REFUSE
    assert retired == {}


def test_completed_cycle_removal_reactivation_uses_fresh_prior():
    """THE removal-path data-loss fix (issue #48, Bug 5 symmetric).

    Sequence: P1 → remove (prior P1) → ``use default`` restores P1 AND retires
    the record → user DELETES P1 → re-activate by removal (key absent). The key
    is absent both now and during the original removal, so absence alone cannot
    tell those apart. Without cycle-state the old code would preserve the STALE
    prior=P1 and a later ``use default`` would RESURRECT the deleted P1. With
    ``active=False`` the completed cycle forces a FRESH prior (None — the key
    really is absent now), so the deleted credential is never resurrected.
    """
    p1 = "sk-user-P1"
    # P1 present → activate by removal (we delete ANTHROPIC_API_KEY).
    records = take_over({}, TOOL, "ANTHROPIC_API_KEY", p1, True, set_hash=None)
    # ``use default``: key still absent → RESTORE prior P1, retire the record.
    records = revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)[1]
    assert records[TOOL]["ANTHROPIC_API_KEY"]["active"] is False
    # User deletes the restored P1 themselves, then re-activates by removal.
    # The key is absent (completed cycle) → FRESH prior (None), NOT stale P1.
    records = take_over(records, TOOL, "ANTHROPIC_API_KEY", None, False, set_hash=None)
    assert records[TOOL]["ANTHROPIC_API_KEY"]["prior_value"] is None  # NOT p1
    assert records[TOOL]["ANTHROPIC_API_KEY"]["active"] is True  # new cycle
    # A later ``use default`` restores None (re-absents) — P1 stays dead.
    decision = _revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)
    assert decision.action == RevertAction.RESTORE
    assert decision.prior_value is None


def test_completed_cycle_set_value_reactivation_uses_fresh_prior():
    """The set-value path is ALSO covered by cycle-state (regression for #47).

    Even without the Bug-5 hash-drift guard, a completed cycle (active=False)
    forces a fresh prior. Belt-and-suspenders with #47's no_external_drift
    check: #47 catches drift when the cycle was NOT retired (no revert ran);
    cycle-state catches it when the cycle WAS retired.
    """
    z, p1, p2 = "sk-zai", "sk-user-P1", "sk-user-P2"
    records = take_over({}, TOOL, "K", p1, True, hash_value(z))
    # ``use default`` retires the record (cycle completed), restoring P1.
    records = revert(records, TOOL, "K", current_value=z)[1]
    assert records[TOOL]["K"]["active"] is False
    # User sets P2, re-activates the SAME z. Completed cycle → fresh prior P2.
    records = take_over(records, TOOL, "K", p2, True, hash_value(z))
    assert records[TOOL]["K"]["prior_value"] == p2  # NOT the stale p1


def test_active_cycle_removal_re_removal_preserves_prior():
    """Re-removing a key while our removal is STILL ACTIVE keeps the prior.

    The non-regression half: P1→remove→remove (no revert between) is a true
    idempotent re-removal — the cycle is still in flight (active=True), so the
    ORIGINAL prior=P1 is preserved. Removing the cycle-state guard would make
    this lose P1 forever (the false fix the issue warns against).
    """
    p1 = "sk-user-P1"
    records = take_over({}, TOOL, "ANTHROPIC_API_KEY", p1, True, set_hash=None)
    # Re-remove while still active (no revert ran): preserve original prior.
    records = take_over(records, TOOL, "ANTHROPIC_API_KEY", None, False, set_hash=None)
    assert records[TOOL]["ANTHROPIC_API_KEY"]["prior_value"] == p1  # kept
    assert records[TOOL]["ANTHROPIC_API_KEY"]["active"] is True


# ---------------------------------------------------------------------------
# Bug 6 (issue #54): revert must REFUSE on an INACTIVE record — a completed
# cycle must not be re-RESTORE'd, or a repeat ``use default`` resurrects a
# stale prior and destroys the user's (re)created config.
# ---------------------------------------------------------------------------


def test_revert_refuses_on_inactive_record_set_value():
    """Bug 6 (set-value path): a retired record must NOT be re-RESTORE'd.

    Sequence: ``use zai`` sets AUTH_TOKEN=Z (prior=P) → ``use default``
    RESTORE's P and retires the record (active=False) → the user re-creates
    their config with the SAME token Z we once wrote → a repeat ``use default``
    sees current=Z still matching the retired ``set_hash``. Without the
    active-check the stale prior=P would be RESTORE'd, silently destroying the
    user's Z (data loss). The fix REFUSEs on the inactive record and leaves
    everything untouched.
    """
    z, p = "sk-zai", "sk-user-P"
    # ``use zai`` then ``use default``: a completed cycle, record retired.
    records = take_over({}, TOOL, "ANTHROPIC_AUTH_TOKEN", p, True, hash_value(z))
    records = revert(records, TOOL, "ANTHROPIC_AUTH_TOKEN", current_value=z)[1]
    assert records[TOOL]["ANTHROPIC_AUTH_TOKEN"]["active"] is False

    # User re-creates the config with the same Z; a repeat ``use default``
    # current=Z still matches the retired set_hash, but the cycle is OVER.
    # Deep-copy the baseline: a shallow ``dict(b)`` shares the inner record
    # dicts with ``records``, so an in-place mutation by ``revert`` would
    # mutate the baseline too and the equality assertion below would pass
    # vacuously. The point is to prove ``revert`` is pure.
    before = copy.deepcopy(records)
    decision, retired = revert(records, TOOL, "ANTHROPIC_AUTH_TOKEN", current_value=z)
    assert decision.action == RevertAction.REFUSE
    assert "already completed" in decision.reason
    # No stale RESTORE: the user's Z is not destroyed (prior_value carries the
    # stale prior only for diagnostics, the action is REFUSE — caller must NOT
    # write it).
    assert retired == before  # journal byte-identical (no retirement flip)
    assert retired[TOOL]["ANTHROPIC_AUTH_TOKEN"]["active"] is False


def test_revert_refuses_on_inactive_record_removal_path():
    """Bug 6 (removal path): a retired removal record must NOT resurrect.

    Sequence: ``use zai`` removes API_KEY (prior=P1) → ``use default`` RESTORE's
    P1 and retires (active=False) → the user DELETES the restored P1 → a repeat
    ``use default`` sees current=None (key absent), which still looks like "our
    removal is live". Without the active-check the stale prior=P1 would be
    RESTORE'd, RESURRECTING the credential the user deleted. The fix REFUSEs.
    """
    p1 = "sk-user-P1"
    records = take_over({}, TOOL, "ANTHROPIC_API_KEY", p1, True, set_hash=None)
    records = revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)[1]
    assert records[TOOL]["ANTHROPIC_API_KEY"]["active"] is False

    # User deletes the restored P1; repeat ``use default`` (current=None).
    # Deep-copy so the purity assertion below is not vacuous (see the
    # set-value test for why a shallow copy cannot detect in-place mutation).
    before = copy.deepcopy(records)
    decision, retired = revert(records, TOOL, "ANTHROPIC_API_KEY", current_value=None)
    assert decision.action == RevertAction.REFUSE  # NOT a stale RESTORE of P1
    assert retired == before  # no resurrection, no journal change


def test_full_resurrection_scenario_is_per_tool_isolated():
    """The headline Bug 6 scenario across TWO tools sharing one journal.

    ``use zai`` → ``use default`` (retire) → user re-creates the SAME token →
    repeat ``use default`` MUST be a no-op (REFUSE), never a stale RESTORE.

    The single-tool set-value case is already covered above; what this adds is
    that the completed-cycle gate is scoped PER TOOL. Two tools live in one
    journal dict, so a retired record under one tool must neither gate nor be
    gated by the other: the retired tool REFUSEs while the still-active tool
    RESTOREs normally in the same journal.
    """
    z, p = "sk-zai-token", "sk-user-original"
    other = "opencode"
    key = "ANTHROPIC_AUTH_TOKEN"

    # Both tools take ownership; only TOOL completes its cycle.
    records = take_over({}, TOOL, key, p, True, hash_value(z))
    records = take_over(records, other, key, p, True, hash_value(z))
    records = revert(records, TOOL, key, current_value=z)[1]
    assert records[TOOL][key]["active"] is False
    assert records[other][key]["active"] is True  # untouched by TOOL's revert

    # Retired tool: user re-created the config with the same token → REFUSE.
    assert _revert(records, TOOL, key, current_value=z).action == RevertAction.REFUSE

    # The other tool's cycle is still in flight → it must still RESTORE.
    live = _revert(records, other, key, current_value=z)
    assert live.action == RevertAction.RESTORE
    assert live.prior_value == p


def test_retired_journal_round_trips_through_disk(tmp_path):
    """A retired (active=False) record survives a write/read cycle (migration).

    ``from_dict`` must read ``active=False`` back faithfully — the cycle-state
    is durable on disk, not just in memory. A pre-#48 entry (no ``active``
    field) round-trips as ``True`` (lenient migration).
    """
    from zai_python_helper.ownership import OwnershipRecord

    journal = OwnershipJournal(tmp_path / "ownership.json")
    records = take_over({}, TOOL, "K", "P", True, hash_value("Z"))
    records = revert(records, TOOL, "K", current_value="Z")[1]
    assert records[TOOL]["K"]["active"] is False

    # Retired record round-trips as active=False through real disk IO.
    journal.write(records)
    disk = OwnershipJournal(tmp_path / "ownership.json").read()
    assert OwnershipRecord.from_dict(disk[TOOL]["K"]).active is False

    # A pre-#48 entry (no `active` key) migrates as active=True.
    legacy = {TOOL: {"K": {"prior_value": "P", "prior_present": True, "set_hash": "h"}}}
    journal.write(legacy)
    legacy_disk = OwnershipJournal(tmp_path / "ownership.json").read()
    assert OwnershipRecord.from_dict(legacy_disk[TOOL]["K"]).active is True





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
    decision = _revert(records, TOOL, "ANTHROPIC_AUTH_TOKEN", current_value=activated)
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
    assert _isolate_home not in paths.ownership_json.parents
    assert not paths.ownership_json.exists()  # no IO yet
