"""Tests for ShellBackend + the pure shell_block transforms (ADR-003).

Covers the load-bearing ADR-003 guarantee: foreign lines survive every
operation; only our owned marker-fenced block is added/removed. Both the pure
transforms (:mod:`zai_python_helper.shell_block`) and the IO
:class:`~zai_python_helper.backends.ShellBackend` are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zai_python_helper.backends import ShellBackend
from zai_python_helper.shell_block import (
    MANAGED_BLOCK_BEGIN,
    MANAGED_BLOCK_END,
    install_owned_block,
    managed_block_lines,
    owns_owned_block,
    remove_owned_block,
)

FOREIGN = 'export PATH=/usr/local/bin:$PATH\nalias ll="ls -la"\n'


# ---------------------------------------------------------------------------
# Pure transforms
# ---------------------------------------------------------------------------


class TestPureInstall:
    def test_install_appends_block_to_nonempty_file(self):
        out = install_owned_block(FOREIGN)
        assert owns_owned_block(out)
        # Foreign lines untouched and in order, before the block.
        assert out.startswith(FOREIGN.rstrip("\n") + "\n\n")
        assert "alias ll" in out

    def test_install_on_empty_file(self):
        out = install_owned_block("")
        assert owns_owned_block(out)
        assert out.startswith(MANAGED_BLOCK_BEGIN)

    def test_install_idempotent(self):
        once = install_owned_block(FOREIGN)
        twice = install_owned_block(once)
        assert once == twice

    def test_install_never_modifies_foreign_lines(self):
        out = install_owned_block(FOREIGN)
        # Every original line must still be present verbatim.
        for line in FOREIGN.splitlines():
            assert line in out


class TestPureRemove:
    def test_remove_strips_only_our_block(self):
        text = install_owned_block(FOREIGN)
        out = remove_owned_block(text)
        assert not owns_owned_block(out)
        # Foreign lines fully restored.
        for line in FOREIGN.splitlines():
            assert line in out
        assert MANAGED_BLOCK_BEGIN not in out
        assert MANAGED_BLOCK_END not in out

    def test_remove_idempotent(self):
        once = remove_owned_block(install_owned_block(FOREIGN))
        twice = remove_owned_block(once)
        assert once == twice

    def test_remove_noop_when_absent(self):
        assert remove_owned_block(FOREIGN) == FOREIGN

    def test_remove_leaves_foreign_between_blocks_intact(self):
        """Foreign lines sitting between our fences are NOT removed.

        Although well-formed installs keep the block contiguous, a user who
        accidentally pastes foreign content inside the fences must not lose
        it silently on remove — only the fenced region is removed, so this is
        a documented edge: content inside fences IS removed. This test pins
        the current contract so a future change is deliberate.
        """
        # Block absent → foreign inside nothing is preserved trivially.
        text = (
            "before\n"
            + MANAGED_BLOCK_BEGIN
            + "\n"
            + "INSIDE\n"
            + MANAGED_BLOCK_END
            + "\nafter\n"
        )
        out = remove_owned_block(text)
        assert "before" in out
        assert "after" in out
        assert "INSIDE" not in out  # fenced content removed (documented)


class TestForeignSurvival:
    """The core ADR-003 guarantee across install/remove round-trips."""

    def test_round_trip_restores_original_exactly(self):
        out = remove_owned_block(install_owned_block(FOREIGN))
        assert out == FOREIGN

    def test_round_trip_with_comments_and_blank_lines(self):
        text = "# my config\n\nexport FOO=bar\n\n# end\n"
        out = remove_owned_block(install_owned_block(text))
        assert out == text

    def test_remove_preserves_unrelated_blank_lines(self):
        text = (
            "before\n\n"
            "export VALUE='line1\n\nline3'\n\n"
            + MANAGED_BLOCK_BEGIN
            + "\nlegacy\n"
            + MANAGED_BLOCK_END
            + "\n"
        )
        assert remove_owned_block(text) == ("before\n\nexport VALUE='line1\n\nline3'\n")

    def test_round_trip_on_empty_file_is_empty(self):
        """Regression (issue #144): the block is the whole file (EOF edge).

        ``install_owned_block("")`` produces just the block + trailing
        newline; removing it must yield the empty file, not a dangling
        ``"\\n"`` left over from the split of the trailing newline.
        """
        assert remove_owned_block(install_owned_block("")) == ""

    def test_round_trip_block_at_eof_without_trailing_newline(self):
        """Issue #144, second EOF edge: block ends the file with NO trailing
        newline. Removal must yield the content lines byte-exactly — no
        fabricated trailing newline, no lost one.
        """
        block = "\n".join(managed_block_lines())
        assert remove_owned_block("content\n\n" + block) == "content"

    def test_remove_block_only_file_with_extra_blank_line(self):
        """Issue #144: block + one blank trailing line. Removing the block
        leaves exactly the blank line — and nothing else.
        """
        block = "\n".join(managed_block_lines())
        assert remove_owned_block(block + "\n\n") == "\n"

    def test_round_trip_preserves_extra_trailing_blank_lines(self):
        """Regression (issue #154): an rc file ending in SEVERAL newlines must
        survive install→remove byte-for-byte. Install used to rstrip the whole
        trailing run and re-append exactly one newline, collapsing e.g. three
        trailing newlines to one. Upstream (checked against
        @z_ai/coding-helper 0.0.7 in the parity Docker stand) never collapses
        trailing blank lines on its rc mutation, so we preserve them too.
        """
        text = "export FOO=1\n\n\n"
        assert remove_owned_block(install_owned_block(text)) == text

    def test_install_keeps_trailing_run_and_adds_separator_blank(self):
        """Install shape for a multi-newline tail: the original trailing run is
        kept verbatim, the block goes after it (still one blank line before
        the fence), and the file keeps ending with exactly one newline.
        """
        out = install_owned_block("export FOO=1\n\n\n")
        assert out.startswith("export FOO=1\n\n\n\n")
        assert out.endswith(MANAGED_BLOCK_END + "\n")

    def test_round_trip_preserves_extra_trailing_blank_lines_on_disk(self, tmp_path):
        rc = tmp_path / ".zshrc"
        raw = "export FOO=1\n\n\n"
        rc.write_bytes(raw.encode())
        assert ShellBackend.install_block(rc)
        assert ShellBackend.remove_block(rc)
        assert rc.read_bytes() == raw.encode()

    def test_multiple_installs_single_block(self):
        text = install_owned_block(install_owned_block(install_owned_block(FOREIGN)))
        assert text.count(MANAGED_BLOCK_BEGIN) == 1
        assert text.count(MANAGED_BLOCK_END) == 1


class TestMalformedMarkersFailClosed:
    """Regression (Codex finding F5): malformed fences must NOT truncate the
    user's file. ``owns`` returns False and ``remove``/``install`` are no-ops
    for reordered, duplicated, or lone fences — we never edit a file whose
    block we cannot identify unambiguously.
    """

    def test_reversed_markers_remove_is_noop(self):
        """END before BEGIN: remove must leave the file byte-for-byte intact
        (previously it deleted everything after the stray BEGIN).
        """
        text = (
            "export A=1\n"
            + MANAGED_BLOCK_END
            + "\n"
            + MANAGED_BLOCK_BEGIN
            + "\nexport CRITICAL=1\n"
        )
        assert not owns_owned_block(text)
        assert remove_owned_block(text) == text
        # The critical foreign line is preserved.
        assert "export CRITICAL=1" in remove_owned_block(text)

    def test_lone_begin_remove_is_noop(self):
        text = "export A=1\n" + MANAGED_BLOCK_BEGIN + "\nexport B=2\n"
        assert not owns_owned_block(text)
        assert remove_owned_block(text) == text

    def test_lone_end_remove_is_noop(self):
        text = "export A=1\n" + MANAGED_BLOCK_END + "\nexport B=2\n"
        assert not owns_owned_block(text)
        assert remove_owned_block(text) == text

    def test_duplicate_begin_is_noop(self):
        text = (
            MANAGED_BLOCK_BEGIN
            + "\n"
            + MANAGED_BLOCK_BEGIN
            + "\n"
            + MANAGED_BLOCK_END
            + "\n"
        )
        # Ambiguous — two BEGINs. Refuse to edit.
        assert not owns_owned_block(text)
        assert remove_owned_block(text) == text

    def test_install_refuses_when_malformed(self):
        """install must not append a second block over a malformed fence set."""
        text = MANAGED_BLOCK_END + "\n" + MANAGED_BLOCK_BEGIN + "\nexport X=1\n"
        out = install_owned_block(text)
        # Left untouched — no second block added.
        assert out == text
        assert out.count(MANAGED_BLOCK_BEGIN) == 1


# ---------------------------------------------------------------------------
# IO backend (atomic write, file lifecycle)
# ---------------------------------------------------------------------------


class TestShellBackendIO:
    def test_install_creates_file_when_absent(self, tmp_path):
        rc = tmp_path / ".zshrc"
        assert ShellBackend.install_block(rc)

    def test_remove_preserves_symlinked_file(self, tmp_path):
        target = tmp_path / "dotfiles.zshrc"
        target.write_text(install_owned_block(FOREIGN))
        rc = tmp_path / ".zshrc"
        rc.symlink_to(target)

        assert ShellBackend.remove_block(rc)
        assert rc.is_symlink()
        assert target.read_text() == FOREIGN

    def test_install_mode_matches_upstream(self, tmp_path):
        rc = tmp_path / ".zshrc"
        assert ShellBackend.install_block(rc)
        text = rc.read_text()
        assert owns_owned_block(text)
        assert rc.stat().st_mode & 0o777 == 0o644

    def test_install_idempotent_on_disk(self, tmp_path):
        rc = tmp_path / ".zshrc"
        rc.write_text(FOREIGN)
        assert ShellBackend.install_block(rc)  # added
        assert not ShellBackend.install_block(rc)  # already present → no-op

    def test_install_preserves_foreign_lines_on_disk(self, tmp_path):
        rc = tmp_path / ".zshrc"
        rc.write_text(FOREIGN)
        ShellBackend.install_block(rc)
        text = rc.read_text()
        for line in FOREIGN.splitlines():
            assert line in text

    def test_remove_strips_block_keeps_foreign_on_disk(self, tmp_path):
        rc = tmp_path / ".zshrc"
        rc.write_text(FOREIGN)
        ShellBackend.install_block(rc)
        assert ShellBackend.remove_block(rc)  # removed
        assert not ShellBackend.remove_block(rc)  # no-op second time
        text = rc.read_text()
        assert not owns_owned_block(text)
        for line in FOREIGN.splitlines():
            assert line in text

    def test_remove_noop_when_file_absent(self, tmp_path):
        rc = tmp_path / ".zshrc"
        # Must not raise on a missing file.
        assert not ShellBackend.remove_block(rc)

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert ShellBackend.read(tmp_path / "nope") == ""


class TestCrlfRoundTrip:
    """Issue #152: CRLF rc files keep their ``\\r\\n`` endings through every
    backend mutation — foreign lines round-trip byte-for-byte, matching the
    upstream fs layer (verified against @z_ai/coding-helper 0.0.7 in the
    parity Docker stand). The appended block uses the file's dominant EOL
    (CRLF), and fences are recognized despite the trailing ``\\r``.
    """

    CRLF_FOREIGN = 'export A=1\r\n\r\nalias ll="ls -l"\r\n'

    def test_remove_preserves_foreign_crlf_bytes(self, tmp_path):
        """The issue #152 reproducer: removing the block from a CRLF file
        must not silently convert the file to LF."""
        rc = tmp_path / "rc"
        crlf = self.CRLF_FOREIGN + "\r\n".join(managed_block_lines()) + "\r\n"
        rc.write_bytes(crlf.encode())
        assert ShellBackend.remove_block(rc)
        assert rc.read_bytes() == self.CRLF_FOREIGN.encode()

    def test_remove_is_noop_when_block_absent_crlf(self, tmp_path):
        """No block → no write at all; the CRLF file is left untouched."""
        rc = tmp_path / "rc"
        crlf = self.CRLF_FOREIGN + MANAGED_BLOCK_BEGIN + "\r\n"
        rc.write_bytes(crlf.encode())
        assert not ShellBackend.remove_block(rc)  # lone BEGIN: fail-closed
        assert rc.read_bytes() == crlf.encode()

    def test_install_appends_block_in_crlf(self, tmp_path):
        rc = tmp_path / "rc"
        rc.write_bytes(self.CRLF_FOREIGN.encode())
        assert ShellBackend.install_block(rc)
        raw = rc.read_bytes().decode()
        assert owns_owned_block(raw)
        # Foreign prefix intact, block written in the file's dominant EOL.
        assert raw.startswith(self.CRLF_FOREIGN)
        assert "\r\n".join(managed_block_lines()) + "\r\n" in raw

    def test_install_then_remove_round_trips_crlf_exactly(self, tmp_path):
        rc = tmp_path / "rc"
        rc.write_bytes(self.CRLF_FOREIGN.encode())
        assert ShellBackend.install_block(rc)
        assert not ShellBackend.install_block(rc)  # found despite CRLF → NOOP
        assert ShellBackend.remove_block(rc)
        assert rc.read_bytes() == self.CRLF_FOREIGN.encode()

    def test_pure_install_dominant_eol_lf_wins_in_mostly_lf_file(self):
        mixed = "a\nb\r\nc\n"
        out = install_owned_block(mixed)
        # LF dominates → block in LF; the one CRLF foreign line survives.
        assert out.startswith("a\nb\r\nc\n\n")
        assert "\n".join(managed_block_lines()) in out

    def test_pure_remove_collapses_crlf_separator_blank_line(self):
        crlf = "before\r\n\r\n" + "\r\n".join(managed_block_lines()) + "\r\n"
        # "before" keeps its own \r\n ending (the LF analog yields "before\n").
        assert remove_owned_block(crlf) == "before\r\n"

    def test_pure_owns_finds_block_despite_trailing_cr(self):
        crlf = "\r\n".join(managed_block_lines())
        assert owns_owned_block(crlf)

    def test_pure_malformed_fences_still_fail_closed_in_crlf(self):
        reversed_crlf = MANAGED_BLOCK_END + "\r\n" + MANAGED_BLOCK_BEGIN + "\r\nx\r\n"
        assert not owns_owned_block(reversed_crlf)
        assert remove_owned_block(reversed_crlf) == reversed_crlf


@pytest.mark.parametrize("path_attr", ["claude_settings", "zshrc"])
def test_backend_paths_resolve_via_paths(path_attr, tmp_path):
    """Smoke: Paths + backend cooperate on a tmp home (HOME isolation seam)."""
    from zai_python_helper.paths import Paths

    paths = Paths.from_home(tmp_path, state_home=tmp_path)
    target: Path = getattr(paths, path_attr)
    ShellBackend.install_block(target) if path_attr == "zshrc" else None
    if path_attr == "zshrc":
        assert owns_owned_block(target.read_text())
