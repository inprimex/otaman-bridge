"""Shared test fixtures — otaman bus isolation.

Adopts the fleet-shared isolation primitive (bus-test-isolation task 3.1,
superseding this repo's interim env-strip harden from PR #48): every test
runs with OTAMAN_ROOT/MAESTRO_ROOT/OTAMAN_AGENT stripped, root resolution
pinned to a per-test tmp sandbox, and the OTAMAN_TEST_MODE sentinel exported
so any resolver that slips past the fixture refuses non-tmp roots.

Tests that exercise the resolution chain from scratch (expect no-root /
marker / walk-up results) should monkeypatch.delenv("OTAMAN_ROOT") in a
module-local fixture — the autouse fixture runs first, so overrides win
(see the bus-test-isolation tasks.md footgun note).

Subprocess tests build their env via test_bridge_cli._env_with_home, which
strips the same vars independently.
"""

from __future__ import annotations

import pytest
from otaman_core.testing import isolate_bus  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Redirect the confirmation ledger (deliberately OUTSIDE tmp isolation,
    at ~/.otaman/confirmations.log) so in-process tests never read or write
    the human's real ledger. Mirrors the cli reference adoption."""
    import otaman_core.confirmations as _conf

    ledger = tmp_path / "test-confirmations.log"
    monkeypatch.setattr(_conf, "default_ledger_path", lambda: ledger)
    return ledger
