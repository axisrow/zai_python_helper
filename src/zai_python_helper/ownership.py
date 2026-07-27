"""Ownership journal (ADR-004) — make ``use default`` non-destructive.

The problem this module solves: ``use zai`` overwrites the user's prior
``ANTHROPIC_*`` values, so a later ``use default`` must restore them. A
naive "blind deletion" (the S2 behaviour) is **destructive** — it throws
away a key the user legitimately set themselves. A frozen first-mutation
``.bak`` is wrong months later if the user changed their key in between
(ADR-004). Instead we keep a **self-invalidating ownership journal**: per
``(tool, key)`` we record the prior value/presence **plus a hash of the
value we set**. On revert we restore the prior value only if the current
value still matches what we set; if it changed externally we refuse to
overwrite it.

Layering (ADR-001: core/IO split):

- **Pure core** (this module's dataclasses + :func:`take_over` /
  :func:`revert` / :func:`hash_value`): operate on a plain ``dict`` of
  journal records. No file access, no env, importable without a
  filesystem. This is the part exported as the public API (issue #18).
- **IO seam** (:class:`OwnershipJournal`): reads/writes that dict to
  ``~/.zai-python-helper/ownership.json`` atomically at mode ``0600``.
  Injected with an explicit path so tests use ``Paths.from_home(tmp)``.

Journal shape on disk::

    {
      "<tool>": {
        "<key>": {
          "prior_value": "<str or null>",
          "prior_present": <bool>,
          "set_hash": "<sha256 hex of the value we set>",
          "active": <bool>
        }, ...
      }, ...
    }

A ``set_hash`` of ``null`` (Python ``None``) means "we took ownership by
*removing* the key" — :func:`revert` treats a missing/None ``set_hash`` as
"nothing we set, so there is nothing to validate against" and restores the
prior value unconditionally (used for ``ANTHROPIC_API_KEY``, which
``use zai`` removes rather than sets).

``active`` is the **cycle-state** (issue #48). A record is ``active=True``
while we still hold ownership of ``(tool, key)``; a successful
:func:`revert` ``RESTORE`` *retires* the record (``active=False``) — the
ownership cycle is complete. A later :func:`take_over` over a retired
record treats the live value as a fresh starting point (records a NEW
prior) rather than preserving the stale one, which closes a symmetric
data-loss path: for ownership-by-removal (``set_hash is None``) there is
no content-addressable proof of continued ownership (key-absence is
ambiguous between "still our removal" and "completed cycle, then the user
removed the key themselves"), so the ``active`` flag is the only way to
tell those apart. Records written before this field existed migrate as
``active=True`` (lenient :meth:`OwnershipRecord.from_dict`), so an
in-flight ownership captured by an older release still restores cleanly.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

# SHA-256 of the credential value we wrote. We never store the value we set
# in cleartext — only its hash, so a leaked journal cannot reveal the active
# Z.ai token. (The *prior* value IS stored in cleartext, because revert needs
# to restore it; the 0600 mode is what protects that.)
_HASH_ALGO = "sha256"


class RevertAction(Enum):
    """What :func:`revert` decided to do with one managed key.

    - ``RESTORE``: the current value still matches what we set → put back the
      prior value (and its presence) we journaled.
    - ``REFUSE``: the value changed externally since we took ownership → do
      NOT overwrite; the caller surfaces a warning.
    - ``CLEAR``: we have no journal entry for this key → drop our managed
      value (the honest inverse when we never owned it).
    """

    RESTORE = "restore"
    REFUSE = "refuse"
    CLEAR = "clear"


@dataclass(frozen=True)
class OwnershipRecord:
    """One per-``(tool, key)`` journal entry (in-memory form).

    Attributes:
        prior_value: The value the key held at the moment we took ownership,
            or ``None`` if the key was absent then. Stored in cleartext
            (revert must restore it); protected by the 0600 journal mode.
        prior_present: Whether the key was present at takeover. Carried
            alongside ``prior_value`` so revert can re-ABSENT a key we
            originally found absent (vs. restoring an explicit empty string).
        set_hash: SHA-256 hex of the value we set when we took ownership, or
            ``None`` if we took ownership by *removing* the key (nothing to
            validate against — revert restores prior unconditionally).
        active: Whether this ownership cycle is still in flight. ``True`` while
            we hold ownership; set ``False`` once :func:`revert` has restored
            the prior (cycle completed). A ``take_over`` over an *inactive*
            record starts a fresh restore point rather than preserving the
            stale prior — this is the cycle-state that disambiguates
            ownership-by-removal's ambiguous key-absence (issue #48). Defaults
            to ``True``; old journal entries written without this field migrate
            as ``True`` (an in-flight ownership from an older release).
    """

    prior_value: str | None
    prior_present: bool
    set_hash: str | None
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk dict shape."""
        return {
            "prior_value": self.prior_value,
            "prior_present": self.prior_present,
            "set_hash": self.set_hash,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OwnershipRecord:
        """Deserialize from the on-disk dict shape (lenient on missing keys).

        ``active`` defaults to ``True`` when absent, so journal entries written
        before the cycle-state field existed (issue #48) migrate as still-in-
        flight: an older release's un-reverted ownership restores cleanly, and
        only a real ``revert`` retires it.
        """
        return cls(
            prior_value=data.get("prior_value"),
            prior_present=bool(data.get("prior_present", False)),
            set_hash=data.get("set_hash"),
            active=bool(data.get("active", True)),
        )


@dataclass(frozen=True)
class RevertDecision:
    """The outcome of :func:`revert` for one key.

    Attributes:
        action: What the caller should do (:class:`RevertAction`).
        key: The env key this decision applies to.
        prior_value: The value to restore when ``action == RESTORE``;
            ``None`` otherwise (or when the prior was itself absent).
        prior_present: Whether the key was present at takeover (RESTORE).
            Tells the caller to re-ABSENT vs. write ``prior_value``.
        reason: Human-readable explanation (for warnings/logging).
    """

    action: RevertAction
    key: str
    prior_value: str | None
    prior_present: bool
    reason: str


# ---------------------------------------------------------------------------
# Pure operations over the journal dict
# ---------------------------------------------------------------------------


def hash_value(value: Any) -> str:
    """Return the SHA-256 hex digest of ``value`` (pure).

    Used to record ``set_hash`` — a fingerprint of the value we set so revert
    can detect an external change without storing the set value in cleartext.

    Coerces to ``str`` first: settings.json ``env`` values may be non-string
    (e.g. ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: 1`` as an int after the
    #28 parity fix). Both the recorded ``set_hash`` and the revert-time
    ``hash_value(current_value)`` go through the same coercion, so an unchanged
    int value still matches its recorded hash.
    """
    return hashlib.new(_HASH_ALGO, str(value).encode("utf-8")).hexdigest()


def _copy_records(records: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-ish copy of the journal dict (records, never mutate input).

    The journal is ``{tool: {key: record_dict}}`` two levels of dicts of
    JSON-shaped leaves; a shallow per-bucket copy with per-record ``dict(...)``
    is enough to make every nested record independent of the source.
    """
    return {
        tool: {k: dict(v) for k, v in bucket.items()}
        for tool, bucket in records.items()
    }


def _with_record(
    records: dict[str, Any],
    tool: str,
    key: str,
    record: OwnershipRecord,
) -> dict[str, Any]:
    """Return a new journal dict with ``(tool, key)`` set to ``record``.

    PURE helper for :func:`revert`'s retire step: copies the journal, then
    writes ``record.to_dict()`` at ``records[tool][key]`` (creating the tool
    bucket if absent). The input is never mutated.
    """
    new_records = _copy_records(records)
    tool_bucket = dict(new_records.get(tool, {}))
    tool_bucket[key] = record.to_dict()
    new_records[tool] = tool_bucket
    return new_records


def take_over(
    records: dict[str, Any],
    tool: str,
    key: str,
    prior_value: str | None,
    prior_present: bool,
    set_hash: str | None,
) -> dict[str, Any]:
    """Record that we now own ``(tool, key)``; return an updated journal copy.

    PURE: takes the current journal ``records`` (a plain dict) and returns a
    NEW dict with the ``(tool, key)`` entry set. The input is never mutated.
    The caller persists the result via :class:`OwnershipJournal`.

    **Idempotent w.r.t. the restore point (ADR-004).** If ``(tool, key)``
    already has a journal entry with the SAME ``set_hash`` — i.e. we are
    re-activating the exact value we already own (a repeat ``use zai`` that
    changes nothing) — the EXISTING entry is preserved untouched, so the
    original prior value/presence (the real restore point) is not overwritten
    by the now-current value. Without this, P→Z→Z would record Z as the prior
    on the second activation and a later ``use default`` would restore Z
    instead of the user's original P. Only when ``set_hash`` DIFFERS (a genuine
    value change, e.g. a rotated token) do we record the new prior.

    **Completed cycle ⇒ fresh restore point (issue #48).** If the existing
    record is INACTIVE (``active=False`` — a prior :func:`revert` ``RESTORE``
    retired it), the old ownership cycle is over: whatever value is live now is
    a new starting point, not a continuation of our old ownership, so we record
    a FRESH prior. This is the symmetric fix to Bug 5 for the removal path,
    where key-absence alone cannot distinguish "our removal is still live" from
    "the cycle completed and the user then removed the restored key themselves"
    — only the ``active`` flag can.
    Args:
        records: The current journal (top-level ``{tool: {key: record}}``).
        tool: The tool name (e.g. ``"claude_code"``).
        key: The env key we are taking ownership of.
        prior_value: The key's value at the moment of takeover (``None`` if
            absent). Stored so revert can restore it.
        prior_present: Whether the key was present at takeover.
        set_hash: Hash of the value we are *setting* (so revert can detect an
            external change). Pass ``None`` if we are taking ownership by
            removing the key (no set value to validate against).

    Returns:
        A new journal dict with the updated entry.
    """
    new_records: dict[str, Any] = {k: dict(v) for k, v in records.items()}
    tool_bucket = dict(new_records.get(tool, {}))

    existing = tool_bucket.get(key)
    if isinstance(existing, dict):
        existing_record = OwnershipRecord.from_dict(existing)

        # Completed-cycle guard (issue #48, cycle-state): if the existing
        # record is INACTIVE, a prior revert already retired it — the value
        # we now see is NOT a continuation of our old ownership, it is a fresh
        # starting point. Record a NEW restore point regardless of path. This
        # is what closes the symmetric removal-path data loss: for ownership-
        # by-removal the key is absent both while our removal is live AND after
        # a completed cycle (revert) + the user deleting the restored key, so
        # absence alone cannot tell those apart — only ``active`` can. Without
        # this guard, P1→remove→default(restore P1)→user deletes P1→re-activate
        # (absent) would keep the STALE prior=P1 and a later ``use default``
        # would resurrect the deleted credential.
        if not existing_record.active:
            record = OwnershipRecord(
                prior_value=prior_value,
                prior_present=prior_present,
                set_hash=set_hash,
            )
            tool_bucket[key] = record.to_dict()
            new_records[tool] = tool_bucket
            return new_records

        # The record is still ACTIVE. Preserve the ORIGINAL restore point only
        # while the live value is still ours (no external drift). This covers:
        #   - re-activating the SAME value (equal set_hash) while it is still
        #     present (P→Z→Z idempotent), AND
        #   - a value ROTATION (P→Z1→Z2) where the live value is still our Z1,
        #     AND
        #   - re-removing a key we own-by-removal (set_hash is None) while it
        #     is still ABSENT (the prior absence is still our live state).
        # In the set-value cases the live value hashes to the recorded
        # ``set_hash``; in the removal case the live value is still absent. In
        # all of them keeping the original prior is correct.
        is_our_removal_still_absent = (
            existing_record.set_hash is None and not prior_present
        )
        no_external_drift = (
            existing_record.set_hash is not None
            and prior_present
            and prior_value is not None
            and hash_value(prior_value) == existing_record.set_hash
        )
        if is_our_removal_still_absent or no_external_drift:
            preserved = OwnershipRecord(
                prior_value=existing_record.prior_value,
                prior_present=existing_record.prior_present,
                set_hash=set_hash,
            )
            tool_bucket[key] = preserved.to_dict()
            new_records[tool] = tool_bucket
            return new_records

        # The live value has DRIFTED from what we last set — even if the new
        # set_hash equals the old one (e.g. P1→Z→default→user=P2→activate the
        # SAME Z again: set_hash matches but the live value is now P2, not our
        # Z). The current value is a genuine new starting point, so record a
        # FRESH restore point. Without this, the second revert would restore
        # the stale P1 and silently destroy the user's P2 (Bug 5, cycle-review
        # on #41). (Note: this branch only fires for a record whose revert
        # never retired it — i.e. ``use default`` was not run between the two
        # activations. The completed-cycle re-activation case is handled above
        # via ``active=False``.)

    record = OwnershipRecord(
        prior_value=prior_value,
        prior_present=prior_present,
        set_hash=set_hash,
    )
    tool_bucket[key] = record.to_dict()
    new_records[tool] = tool_bucket
    return new_records


def revert(
    records: dict[str, Any],
    tool: str,
    key: str,
    current_value: str | None,
) -> tuple[RevertDecision, dict[str, Any]]:
    """Decide how to revert one ``(tool, key)`` given its current value.

    PURE decision over the journal dict, following ADR-004's
    **self-invalidating** rule: we only mutate a key whose current state is
    still attributable to us. The cases:

    1. **Entry exists, ``set_hash`` matches ``current_value``** → ``RESTORE``.
       The value is still the one we set, so restoring the prior
       value/presence is safe and correct.
    2. **Entry exists but the value changed externally** → ``REFUSE``. The
       user (or another tool) edited the key since activation; we must not
       clobber it. The caller warns and leaves the key alone.
    3. **No journal entry** → ``REFUSE``. We have no provenance proving we
       own this key, so we must NOT delete or overwrite a value we cannot
       attribute to ourselves. (This is the fix for the S3 blind-deletion
       regression: ``use default`` with no prior ``use zai`` must not wipe a
       key the user configured by hand.)
    4. **Entry exists but is INACTIVE (``active=False``)** → ``REFUSE``. A
       prior ``revert`` already restored the prior and retired this record —
       the ownership cycle is OVER. The value live now belongs to whoever
       (re)created it after our cycle ended, so a repeat ``use default`` must
       NOT act again: re-matching the retired ``set_hash`` (e.g. the user
       re-set the SAME token we once wrote) would otherwise RESTORE the STALE
       prior and destroy their config, and the removal path would RESURRECT a
       credential the user deleted after the first revert. The ``active`` flag
       gates BOTH halves of the cycle (symmetric to the ``take_over`` check,
       issue #48). This is the Bug 6 fix (issue #54): the journal is left
       untouched (there is no in-flight ownership to retire).

    A ``None`` ``set_hash`` records ownership-by-removal (we deleted the key
    on activation, e.g. ``ANTHROPIC_API_KEY``). The "value we set" is the
    key's ABSENCE, so revert restores the prior only while the key is STILL
    ABSENT (``current_value is None``); if a value has since appeared (the
    user added a new key), that is an external change → ``REFUSE``.

    **Cycle completion (issue #48).** A ``RESTORE`` *retires* the record —
    the returned journal marks ``active=False`` for ``(tool, key)``. The
    ownership cycle is over: a later :func:`take_over` over the retired
    record starts a fresh restore point instead of preserving the stale
    prior, which is what prevents resurrecting a credential the user deleted
    after the revert. ``REFUSE`` and no-entry cases leave the journal
    untouched (REFUSE means we did NOT act, so the cycle is still in flight;
    no-entry means there was never a cycle to retire). The INACTIVE-record
    ``REFUSE`` (case 4) also leaves the journal untouched — the cycle was
    already completed by an earlier revert, so there is nothing to retire.

    Args:
        records: The current journal dict.
        tool: The tool name.
        key: The env key to revert.
        current_value: The key's CURRENT value (``None`` if absent). The
            caller reads this from the live config just before reverting.

    Returns:
        A ``(decision, new_records)`` pair. ``decision`` tells the caller what
        to do; ``new_records`` is a NEW journal dict (input never mutated)
        with the record retired to ``active=False`` when ``decision.action``
        is ``RESTORE``, and unchanged (but still a fresh copy) otherwise. The
        caller persists ``new_records`` so the retirement survives to disk.
    """

    def _retire() -> dict[str, Any]:
        """Return a new journal with ``(tool, key)`` retired (active=False)."""
        retired = OwnershipRecord(
            prior_value=record.prior_value,
            prior_present=record.prior_present,
            set_hash=record.set_hash,
            active=False,
        )
        return _with_record(records, tool, key, retired)

    tool_bucket = records.get(tool) or {}
    raw = tool_bucket.get(key)
    if raw is None:
        # No provenance: we cannot prove we own this key → refuse to touch it
        # (never blindly delete a value we cannot attribute to ourselves).
        return (
            RevertDecision(
                action=RevertAction.REFUSE,
                key=key,
                prior_value=None,
                prior_present=False,
                reason=(
                    f"no journal entry for {tool}/{key} — cannot prove "
                    "ownership, not touching"
                ),
            ),
            _copy_records(records),
        )

    record = OwnershipRecord.from_dict(raw if isinstance(raw, dict) else {})

    # Completed-cycle guard (issue #54, Bug 6): if the record is INACTIVE, a
    # prior revert already RESTORE'd and retired it — the ownership cycle is
    # OVER. Re-running ``use default`` must NOT act again, because the value
    # live now belongs to whoever (re)created it after our cycle ended, not to
    # us. Without this gate the stale ``set_hash``/prior pair stays
    # authoritative: a repeat revert that still matches the retired ``set_hash``
    # (e.g. the user re-set the SAME token we once wrote) would RESTORE the
    # STALE prior and silently destroy their config, and the removal path would
    # RESURRECT a credential the user deleted after the first revert. Symmetric
    # to the ``take_over`` active-check (#48) — the ``active`` flag gates BOTH
    # halves of the cycle. We REFUSE (no-op) and leave the journal untouched:
    # there is no in-flight ownership to retire.
    if not record.active:
        return (
            RevertDecision(
                action=RevertAction.REFUSE,
                key=key,
                prior_value=record.prior_value,
                prior_present=record.prior_present,
                reason=(
                    f"{tool}/{key} ownership cycle already completed "
                    "(inactive record) — not restoring stale prior"
                ),
            ),
            _copy_records(records),
        )

    # We took ownership by REMOVING the key. The "value we set" is absence:
    # restore the prior only while the key is still absent. If a value has
    # since appeared (the user added a new key), that is an external change →
    # refuse to overwrite it.
    if record.set_hash is None:
        if current_value is None:
            return (
                RevertDecision(
                    action=RevertAction.RESTORE,
                    key=key,
                    prior_value=record.prior_value,
                    prior_present=record.prior_present,
                    reason=f"{tool}/{key}: still absent since our removal, restoring prior",
                ),
                _retire(),
            )
        return (
            RevertDecision(
                action=RevertAction.REFUSE,
                key=key,
                prior_value=record.prior_value,
                prior_present=record.prior_present,
                reason=(
                    f"{tool}/{key} reappeared after our removal "
                    "— not overwriting the new value"
                ),
            ),
            _copy_records(records),
        )

    # We set a value; only restore if the current value is still ours.
    if current_value is not None and hash_value(current_value) == record.set_hash:
        return (
            RevertDecision(
                action=RevertAction.RESTORE,
                key=key,
                prior_value=record.prior_value,
                prior_present=record.prior_present,
                reason=f"{tool}/{key} unchanged since activation — restoring prior",
            ),
            _retire(),
        )

    return (
        RevertDecision(
            action=RevertAction.REFUSE,
            key=key,
            prior_value=record.prior_value,
            prior_present=record.prior_present,
            reason=(
                f"{tool}/{key} changed externally since activation "
                "— not overwriting; inspect ownership.json"
            ),
        ),
        _copy_records(records),
    )


# ---------------------------------------------------------------------------
# IO seam: read/write the journal file (0600, atomic)
# ---------------------------------------------------------------------------

# Ownership records may carry a credential (prior_value) → 0600, same posture
# as the secrets file. Atomic write reuses the proven temp+fsync+replace.
_JOURNAL_FILE_MODE = 0o600


class OwnershipJournal:
    """Read/write the ownership journal dict to disk (0600, atomic).

    Thin IO seam over the pure :func:`take_over` / :func:`revert` functions:
    the decisions are pure; this class only knows WHERE the dict lives and
    HOW to persist it safely. Injected with an explicit ``path`` (resolved by
    :class:`~zai_python_helper.paths.Paths`) so tests never touch a real
    ``$HOME``.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        """Parse the journal file → dict, or ``{}`` if absent/empty.

        A malformed journal raises :class:`ConfigurationError` (never a bare
        ``JSONDecodeError``) — a corrupted journal is a real, reportable
        condition, not an internal crash.
        """
        from zai_python_helper.errors import ConfigurationError

        if not self.path.exists():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigurationError(f"Failed to read {self.path}: {e}") from e
        if not text.strip():
            return {}
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON in {self.path}: {e}") from e
        if not isinstance(doc, dict):
            raise ConfigurationError(
                f"{self.path}: expected a JSON object at top level, "
                f"got {type(doc).__name__}"
            )
        return doc

    def write(self, records: dict[str, Any]) -> None:
        """Serialize ``records`` to pretty JSON and write atomically at 0600.

        ``indent=2`` + insertion-order preservation + trailing newline. The
        file is created mode ``0600`` (credentials may be present) via the
        temp+fsync+``os.replace`` path, so a crash never leaves a partial or
        world-readable journal. A missing record dict is normalized to ``{}``.
        """
        text = json.dumps(records or {}, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_secret(self.path, text.encode("utf-8"))


def _atomic_write_secret(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically at mode ``0600``.

    Local copy of the atomic-write primitive so :mod:`ownership` has no
    import cycle with :mod:`backends` (which imports planner types). Same
    guarantees: temp in the same dir, fsync before replace, dir fsync after,
    0600 mode on the temp so the replaced file is never world-readable even
    transiently.
    """
    from zai_python_helper.errors import ConfigurationError

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            # 0600 BEFORE we write the bytes, so the temp is never readable
            # by group/other even for the instant it exists.
            os.chmod(tmp_path, _JOURNAL_FILE_MODE)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            _fsync_dir(path.parent)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except OSError as e:
        raise ConfigurationError(f"Failed to write {path}: {e}") from e


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort ``fsync`` of a directory (ignores unsupported filesystems)."""
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
