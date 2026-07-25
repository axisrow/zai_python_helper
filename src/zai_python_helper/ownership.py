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
          "set_hash": "<sha256 hex of the value we set>"
        }, ...
      }, ...
    }

A ``set_hash`` of ``null`` (Python ``None``) means "we took ownership by
*removing* the key" — :func:`revert` treats a missing/None ``set_hash`` as
"nothing we set, so there is nothing to validate against" and restores the
prior value unconditionally (used for ``ANTHROPIC_API_KEY``, which
``use zai`` removes rather than sets).
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
    """

    prior_value: str | None
    prior_present: bool
    set_hash: str | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk dict shape."""
        return {
            "prior_value": self.prior_value,
            "prior_present": self.prior_present,
            "set_hash": self.set_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OwnershipRecord:
        """Deserialize from the on-disk dict shape (lenient on missing keys)."""
        return cls(
            prior_value=data.get("prior_value"),
            prior_present=bool(data.get("prior_present", False)),
            set_hash=data.get("set_hash"),
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
        # Re-activating the SAME value we already own: keep the ORIGINAL
        # restore point (do not let a repeat activation overwrite the prior
        # with the now-current value).
        if existing_record.set_hash == set_hash:
            return new_records  # entry unchanged — original prior preserved

        # VALUE ROTATION (different set_hash), but only safe to preserve the
        # original restore point when the live value has NOT drifted from what
        # we last set. If the value present now (``prior_value``) still hashes
        # to the existing ``set_hash``, it is still our value (e.g. P→Z1→Z2:
        # after Z1 the live value is Z1, which is what we set) — so we keep the
        # ORIGINAL prior (P) and only advance ``set_hash`` to the new value.
        # Without this, a token rotation would record the PREVIOUS Z.ai token
        # (Z1) as the prior, and ``use default`` would restore a stale Z.ai
        # credential against the default endpoint.
        # Only when the live value has drifted externally (hash ≠ existing
        # set_hash) do we treat the current value as a genuinely new starting
        # point and record a fresh prior.
        no_external_drift = (
            existing_record.set_hash is not None
            and prior_present
            and prior_value is not None
            and hash_value(prior_value) == existing_record.set_hash
        )
        if no_external_drift:
            preserved = OwnershipRecord(
                prior_value=existing_record.prior_value,
                prior_present=existing_record.prior_present,
                set_hash=set_hash,
            )
            tool_bucket[key] = preserved.to_dict()
            new_records[tool] = tool_bucket
            return new_records

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
) -> RevertDecision:
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

    A ``None`` ``set_hash`` records ownership-by-removal (we deleted the key
    on activation, e.g. ``ANTHROPIC_API_KEY``). The "value we set" is the
    key's ABSENCE, so revert restores the prior only while the key is STILL
    ABSENT (``current_value is None``); if a value has since appeared (the
    user added a new key), that is an external change → ``REFUSE``.

    Args:
        records: The current journal dict.
        tool: The tool name.
        key: The env key to revert.
        current_value: The key's CURRENT value (``None`` if absent). The
            caller reads this from the live config just before reverting.

    Returns:
        A :class:`RevertDecision` the caller acts on.
    """
    tool_bucket = records.get(tool) or {}
    raw = tool_bucket.get(key)
    if raw is None:
        # No provenance: we cannot prove we own this key → refuse to touch it
        # (never blindly delete a value we cannot attribute to ourselves).
        return RevertDecision(
            action=RevertAction.REFUSE,
            key=key,
            prior_value=None,
            prior_present=False,
            reason=(
                f"no journal entry for {tool}/{key} — cannot prove ownership, "
                "not touching"
            ),
        )

    record = OwnershipRecord.from_dict(raw if isinstance(raw, dict) else {})

    # We took ownership by REMOVING the key. The "value we set" is absence:
    # restore the prior only while the key is still absent. If a value has
    # since appeared (the user added a new key), that is an external change →
    # refuse to overwrite it.
    if record.set_hash is None:
        if current_value is None:
            return RevertDecision(
                action=RevertAction.RESTORE,
                key=key,
                prior_value=record.prior_value,
                prior_present=record.prior_present,
                reason=f"{tool}/{key}: still absent since our removal, restoring prior",
            )
        return RevertDecision(
            action=RevertAction.REFUSE,
            key=key,
            prior_value=record.prior_value,
            prior_present=record.prior_present,
            reason=(
                f"{tool}/{key} reappeared after our removal "
                "— not overwriting the new value"
            ),
        )

    # We set a value; only restore if the current value is still ours.
    if current_value is not None and hash_value(current_value) == record.set_hash:
        return RevertDecision(
            action=RevertAction.RESTORE,
            key=key,
            prior_value=record.prior_value,
            prior_present=record.prior_present,
            reason=f"{tool}/{key} unchanged since activation — restoring prior",
        )

    return RevertDecision(
        action=RevertAction.REFUSE,
        key=key,
        prior_value=record.prior_value,
        prior_present=record.prior_present,
        reason=(
            f"{tool}/{key} changed externally since activation "
            "— not overwriting; inspect ownership.json"
        ),
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
