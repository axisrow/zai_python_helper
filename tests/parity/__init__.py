"""Parity tests against the upstream ``@z_ai/coding-helper`` (issue #17).

This package holds the MINIMAL smoke scaffolding for #17 — a subset that only
asserts the ``-v`` / ``--version`` FORMAT matches the upstream tool. The full
parity suite (e.g. ``use zai`` output, the headless path) is gated behind the
upstream headless-path research (issue #9) and will land later.

Two-phase parity strategy (recorded on #17):
  - Phase 1 (before our features ship): behavior must match the upstream 1:1.
    This version-format test is a Phase-1 surface.
  - Phase 2 (once our features land): divergence is allowed and EXPECTED for
    modes, headless CLI, the importable core, and the journal. Those go in an
    allowlist, not in strict-assertion tests.

The version NUMBER always differs (we are not the upstream package); the test
normalizes the number away and compares only the FORMAT.
"""
