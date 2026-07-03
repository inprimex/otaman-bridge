"""CE-only fallback regression test — F040 phase 6.

Verifies BridgeDaemon degrades correctly when otaman_bridge_ee is
genuinely absent, closing the gap Fable's review of the F040 refactor
plan flagged: manual ad-hoc checks during phases 1-5 didn't actually
force OTAMAN_AUTH_MODE=oidc, so the EE-conditional imports were never
attempted regardless of whether EE was blocked. This test forces OIDC
mode on AND blocks otaman_bridge_ee, so it's the first check that
actually exercises the fallback path -- and it runs in CI on every PR
via the existing test job (no separate CI matrix leg needed).

Runs in a subprocess rather than monkeypatching sys.modules in-process:
other test modules in this suite import otaman_bridge_ee submodules
directly, so an in-process block would race against whatever's already
cached depending on test collection/execution order.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent("""
    import sys, os, json
    import urllib.request, urllib.error

    os.environ["OTAMAN_AUTH_MODE"] = "oidc"
    os.environ["OIDC_ISSUER"] = "https://example.zitadel.cloud"
    os.environ["OIDC_AUDIENCE_BRIDGE"] = "test-client-id"
    os.environ["OIDC_BRIDGE_REDIRECT_URI"] = "https://bridge.example.com/auth/callback"

    # Block otaman_bridge_ee before anything else can import it.
    sys.modules["otaman_bridge_ee"] = None

    from otaman_bridge.core import Transport, TransportHandle
    import otaman_bridge.daemon as daemon_mod

    class StubTransport(Transport):
        name = "stub"
        async def send_approval(self, req):
            return TransportHandle(chat_id="c", message_id="1")
        async def send_info(self, info):
            pass
        async def update(self, handle, text):
            pass
        async def listen(self):
            if False:
                yield None

    d = daemon_mod.BridgeDaemon(account="ce_ci_check", transport=StubTransport())
    assert d.idp_config is None, "idp_config should be None with EE absent"
    assert d.web_login_flow is None, "web_login_flow should be None with EE absent"
    assert d._ee_dcr_try_handle is None, "_ee_dcr_try_handle should be None with EE absent"
    provider_types = [type(p).__name__ for p in d.auth_provider.providers]
    assert "OIDCAuthProvider" not in provider_types, "OIDCAuthProvider must not be in the chain"
    assert d.get_or_build_dcr_mgmt_client() is None

    d.start()
    try:
        base = f"http://127.0.0.1:{d.port}"
        with urllib.request.urlopen(f"{base}/status", timeout=5) as resp:
            body = json.loads(resp.read())
        assert body["account"] == "ce_ci_check"

        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
            assert json.loads(resp.read())["ok"] is True

        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            html = resp.read().decode()
        assert "not configured" in html

        req = urllib.request.Request(f"{base}/mcp", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected 401 for unauthenticated /mcp")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        d.stop()

    print("CE_ONLY_FALLBACK_OK")
""")


def test_daemon_degrades_gracefully_with_ee_absent():
    """Every EE-conditional daemon attribute must fall back to
    None/loopback-only when otaman_bridge_ee is not installed, and the
    HTTP surface (status/healthz/root/mcp) must still work end-to-end.

    Regression test for the CE/EE split contract documented in
    otaman-meta/strategy/bridge-ce-ee-split.md and re-verified by hand
    across F040 phases 1-6 (bridge-agent/spec-agent bus thread,
    2026-07-03).
    """
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"CE-only fallback check failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "CE_ONLY_FALLBACK_OK" in result.stdout
