"""Shared test fixtures — otaman env isolation.

Interim hardening per deploy-agent's fleet mitigation broadcast
(20260816T193911): live sessions can carry a stale OTAMAN_ROOT pointing at
a real org root; resolution is marker → env → walk-up, so a test that
inherits session env and exercises bus-write code can spray fixtures into
a live bus it silently creates (the 2026-08-16 org-level leakage incident).

Stripping the vars here makes every in-process test resolve from its own
tmp_path (or not at all) instead of the session's workspace. Subprocess
tests build their env via test_bridge_cli._env_with_home, which strips the
same vars independently.

To be converged onto the shared `otaman_core.testing` isolation primitive
when bus-test-isolation task 1.1 lands (our adoption is task 3.1).
"""

from __future__ import annotations

import pytest

# legacy: MAESTRO_ROOT is the pre-rename alias, removed at otaman-core 1.0
_OTAMAN_ENV_VARS = ("OTAMAN_ROOT", "MAESTRO_ROOT", "OTAMAN_AGENT")


@pytest.fixture(autouse=True)
def _isolate_otaman_env(monkeypatch):
    """Strip workspace-resolution env vars so tests never see the live root."""
    for var in _OTAMAN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
