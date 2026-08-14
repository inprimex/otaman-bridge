"""F185 — end-to-end wiring: BridgeDaemon -> AuthStack -> IdpConfig must
actually read platform.yaml's terminal.dcr_shim_trust, not just the
unit-level precedence logic in test_dcr_shim.py.

The precedence logic itself (platform.yaml > env > "protected" default,
invalid values fall back to "protected") is unit-tested against
IdpConfig.from_env() directly in test_dcr_shim.py -- this file only
covers the one thing those unit tests can't: that BridgeDaemon actually
threads bus_watcher_root through to AuthStack to IdpConfig.from_env()
as project_root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon
from otaman_bridge.transports.null import NullTransport


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".agents" / "bus" / "active").mkdir(parents=True)
    return tmp_path


def _make_daemon(tmp_path: Path, workspace: Path) -> BridgeDaemon:
    return BridgeDaemon(
        account="test",
        transport=NullTransport(allowlist={"*"}),
        endpoint_file=tmp_path / ".maestro" / "bridge-test.endpoint",
        bus_watcher_root=workspace,
    )


class TestDcrShimTrustWiring:
    def _enable_shim_env(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_DCR_SHIM", "1")
        monkeypatch.setenv("OIDC_ISSUER", "http://idp.example")

    def test_platform_yaml_dcr_shim_trust_reaches_idp_config(
        self,
        tmp_path,
        workspace,
        monkeypatch,
    ):
        self._enable_shim_env(monkeypatch)
        (workspace / "platform.yaml").write_text(
            "terminal:\n  dcr_shim_trust: open\n",
            encoding="utf-8",
        )
        daemon = _make_daemon(tmp_path, workspace)
        assert daemon.idp_config is not None
        assert daemon.idp_config.registration_trust == "open"

    def test_no_platform_yaml_key_defaults_to_protected(
        self,
        tmp_path,
        workspace,
        monkeypatch,
    ):
        self._enable_shim_env(monkeypatch)
        (workspace / "platform.yaml").write_text(
            "project: test\n",
            encoding="utf-8",
        )
        daemon = _make_daemon(tmp_path, workspace)
        assert daemon.idp_config is not None
        assert daemon.idp_config.registration_trust == "protected"

    def test_no_bus_watcher_root_falls_back_to_env(
        self,
        tmp_path,
        monkeypatch,
    ):
        """env-only / --no-config mode: no project_root to read
        platform.yaml from at all -- env var still works."""
        self._enable_shim_env(monkeypatch)
        monkeypatch.setenv("OTAMAN_DCR_SHIM_TRUST", "open")
        daemon = BridgeDaemon(
            account="test2",
            transport=NullTransport(allowlist={"*"}),
            endpoint_file=tmp_path / ".maestro" / "bridge-test2.endpoint",
        )
        assert daemon.idp_config is not None
        assert daemon.idp_config.registration_trust == "open"
