"""Integration: daemon + transport + listener loop end-to-end.

Covers the full hook → daemon → transport → decision → hook path that
T2b wires up for the first time. Uses NullTransport for deterministic
control (push_reply injects decisions directly into the queue that the
daemon's listener loop drains).
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest


from otaman_bridge.core import InboundReply
from otaman_bridge.daemon import BridgeDaemon
from otaman_bridge.transports.null import NullTransport


def _post(url: str, body: dict, token: str | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


@pytest.fixture
def running_daemon(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-int.endpoint"
    daemon = BridgeDaemon(
        account="int",
        transport=transport,
        endpoint_file=endpoint,
    )
    daemon.start()
    try:
        yield daemon, transport
    finally:
        daemon.stop()


def _approval_body(request_id: str, timeout: int = 10):
    return {
        "account": "personal",
        "project": "demo",
        "repo": "auth",
        "agent": "backend",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "reason": "test",
        "priority": "normal",
        "timeout_seconds": timeout,
        "request_id": request_id,
    }


class TestListenerLoop:
    def test_approve_action_from_transport_resolves_as_allow(self, running_daemon):
        """Simulates a button tap: InboundReply(action=approve) → decision=allow."""
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "int-approve-1"
        body = _approval_body(rid)
        result: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token)
                result["body"] = json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=do_request)
        t.start()

        # Wait until transport has the approval
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)
        assert transport.sent_approvals

        # Simulate a button tap by pushing an InboundReply
        transport.push_reply(InboundReply(
            request_id=rid,
            action="approve",
            responder="telegram:12345",
            comment="",
        ))

        t.join(timeout=5.0)
        assert "error" not in result, result.get("error")
        assert result["body"]["decision"] == "allow"
        assert result["body"]["responder"] == "telegram:12345"

    def test_reject_action_resolves_as_deny(self, running_daemon):
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "int-reject-1"
        body = _approval_body(rid)
        result: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token)
                result["body"] = json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=do_request)
        t.start()

        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)

        transport.push_reply(InboundReply(
            request_id=rid,
            action="reject",
            responder="telegram:12345",
            comment="too risky",
        ))

        t.join(timeout=5.0)
        assert result["body"]["decision"] == "deny"
        assert result["body"]["message"] == "too risky"

    def test_details_action_does_not_resolve(self, running_daemon):
        """Details is not a decision — the approval stays pending."""
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "int-details-1"
        body = _approval_body(rid, timeout=2)  # short timeout
        result: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token)
                result["body"] = json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=do_request)
        t.start()

        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)

        # Simulate a "Details" tap — should NOT resolve the approval.
        transport.push_reply(InboundReply(
            request_id=rid, action="details",
            responder="telegram:12345", comment="",
        ))

        t.join(timeout=5.0)
        # Approval should have timed out because details didn't resolve it
        assert result["body"]["decision"] == "timeout"

    def test_details_action_sends_full_payload_info(self, running_daemon):
        """Tapping Details surfaces the full tool_input as a follow-up info."""
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "int-details-full-1"
        # Craft a request with tool_input that would be TRUNCATED in the
        # original approval card. Details should show the whole thing.
        long_command = "npm install " + " ".join(f"pkg-{i}@1.0.0" for i in range(30))
        assert len(long_command) > 200  # exceeds approval-card truncation
        body = _approval_body(rid, timeout=10)
        body["tool_input"] = {"command": long_command, "extra": {"nested": "value"}}

        result: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token)
                result["body"] = json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=do_request)
        t.start()
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)
        assert transport.sent_approvals

        # Tap Details.
        transport.push_reply(InboundReply(
            request_id=rid, action="details",
            responder="telegram:12345", comment="",
        ))

        # Give the async dispatch a beat to call send_info.
        for _ in range(40):
            if transport.sent_infos:
                break
            time.sleep(0.05)
        assert transport.sent_infos, "send_info was never called"
        info = transport.sent_infos[0]
        assert info.title.startswith("Details ")
        # Full command (NOT truncated) must be in the body.
        assert long_command in info.body
        assert "pkg-29" in info.body  # last package — would be cut by truncation
        # Nested structure survives.
        assert "nested" in info.body

        # Now approve it so the request returns and the test cleans up.
        transport.push_reply(InboundReply(
            request_id=rid, action="approve",
            responder="telegram:12345", comment="",
        ))
        t.join(timeout=5.0)
        assert result["body"]["decision"] == "allow"

    def test_details_for_unknown_request_silent(self, running_daemon):
        """Details on an already-resolved / nonexistent approval is
        logged + skipped, not a crash or false send."""
        _, transport = running_daemon
        transport.push_reply(InboundReply(
            request_id="no-such-approval",
            action="details",
            responder="telegram:12345",
            comment="",
        ))
        # Give the listener a beat — nothing should reach sent_infos.
        time.sleep(0.3)
        assert not transport.sent_infos

    def test_reply_for_unknown_request_id_is_silent(self, running_daemon):
        """Stray reply (typo'd request_id) doesn't break the listener loop."""
        daemon, transport = running_daemon
        # Push a reply with no matching pending approval
        transport.push_reply(InboundReply(
            request_id="nonexistent-123",
            action="approve",
            responder="test",
            comment="",
        ))
        # Give the listener a beat to process
        time.sleep(0.3)

        # Subsequent approval + reply should still work
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "int-healthy-1"
        body = _approval_body(rid)
        result: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token)
                result["body"] = json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=do_request)
        t.start()
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)
        transport.push_reply(InboundReply(
            request_id=rid, action="approve", responder="t", comment="",
        ))
        t.join(timeout=5.0)
        assert result["body"]["decision"] == "allow"


class TestSnooze:
    """Snooze defers an approval: edits original card, extends deadline,
    re-posts a fresh card after the snooze window."""

    def _short_snooze(self, monkeypatch, seconds: float = 0.8):
        """Shrink SNOOZE_SECONDS so tests run in <5s instead of 15min."""
        import otaman_bridge.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod, "SNOOZE_SECONDS", seconds)

    def test_snooze_edits_original_card(self, running_daemon, monkeypatch):
        """Tapping Snooze edits the original card to strip buttons +
        show re-post time."""
        self._short_snooze(monkeypatch, 0.5)
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "snooze-edit-1"
        body = _approval_body(rid, timeout=5)

        result: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token)
                result["body"] = json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=do_request)
        t.start()
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)
        assert transport.sent_approvals

        # Tap snooze.
        transport.push_reply(InboundReply(
            request_id=rid, action="snooze",
            responder="telegram:12345", comment="",
        ))

        # Wait for the re-post + approve it to unblock the test.
        for _ in range(80):
            if len(transport.sent_approvals) >= 2:
                break
            time.sleep(0.05)
        assert len(transport.sent_approvals) >= 2, "snooze should re-post the card"

        # Check the original card was updated with snooze text.
        assert transport.updates, "transport.update wasn't called to edit original"
        _, status = transport.updates[0]
        assert "snooze" in status.lower() or "re-post" in status.lower()

        transport.push_reply(InboundReply(
            request_id=rid, action="approve",
            responder="telegram:12345", comment="",
        ))
        t.join(timeout=5.0)
        assert result["body"]["decision"] == "allow"

    def test_snooze_extends_deadline_beyond_original_timeout(
        self, running_daemon, monkeypatch,
    ):
        """Without the deadline extension, the hook would time out
        during the snooze window and the re-post would be orphaned.
        Verify the approval survives long enough to be approved after
        re-post, even though its original timeout was shorter than
        snooze + clock time."""
        # Snooze for 1s; original timeout 0.5s. Without extension the
        # approval would time out at t=0.5. With extension, it survives
        # until t=1+30s, so the re-post + approve round-trips cleanly.
        self._short_snooze(monkeypatch, 1.0)

        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "snooze-extend-1"
        body = _approval_body(rid, timeout=1)  # small; would time out mid-snooze
        result: dict = {}

        def do_request():
            resp = _post(url, body, token=daemon.token, timeout=30)
            result["body"] = json.loads(resp.read().decode("utf-8"))

        t = threading.Thread(target=do_request)
        t.start()
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)

        transport.push_reply(InboundReply(
            request_id=rid, action="snooze",
            responder="telegram:12345", comment="",
        ))

        # Wait for re-post (> original 1s timeout) then approve.
        for _ in range(60):
            if len(transport.sent_approvals) >= 2:
                break
            time.sleep(0.05)
        assert len(transport.sent_approvals) >= 2

        transport.push_reply(InboundReply(
            request_id=rid, action="approve",
            responder="telegram:12345", comment="",
        ))
        t.join(timeout=5.0)
        assert result["body"]["decision"] == "allow", (
            f"expected approval to survive snooze, got {result['body']}"
        )

    def test_snooze_repost_skipped_if_resolved(
        self, running_daemon, monkeypatch,
    ):
        """If the user Approves before the snooze fires, the re-post
        must NOT happen (no second notification for a decided approval)."""
        self._short_snooze(monkeypatch, 0.8)

        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "snooze-resolved-1"
        body = _approval_body(rid, timeout=5)
        result: dict = {}

        def do_request():
            resp = _post(url, body, token=daemon.token, timeout=30)
            result["body"] = json.loads(resp.read().decode("utf-8"))

        t = threading.Thread(target=do_request)
        t.start()
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)

        transport.push_reply(InboundReply(
            request_id=rid, action="snooze",
            responder="telegram:12345", comment="",
        ))
        # Approve quickly — before snooze fires at t=0.8s.
        transport.push_reply(InboundReply(
            request_id=rid, action="approve",
            responder="telegram:12345", comment="",
        ))
        t.join(timeout=5.0)
        assert result["body"]["decision"] == "allow"

        # Wait past the snooze deadline to confirm no re-post happens.
        time.sleep(1.2)
        assert len(transport.sent_approvals) == 1, (
            f"re-post should skip when already resolved; got "
            f"{len(transport.sent_approvals)} send_approval calls"
        )

    def test_snooze_for_unknown_request_silent(self, running_daemon):
        """Snooze for a nonexistent / resolved request_id is a no-op."""
        _, transport = running_daemon
        original_approvals = len(transport.sent_approvals)
        transport.push_reply(InboundReply(
            request_id="no-such-snooze",
            action="snooze",
            responder="telegram:12345",
            comment="",
        ))
        time.sleep(0.3)
        assert len(transport.sent_approvals) == original_approvals

class TestUpdateOnDecision:
    """After a decision, the daemon should schedule a transport.update()."""

    def test_update_called_on_allow(self, running_daemon):
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        rid = "int-update-allow"
        body = _approval_body(rid)
        result: dict = {}

        def do_request():
            resp = _post(url, body, token=daemon.token)
            result["body"] = json.loads(resp.read().decode("utf-8"))

        t = threading.Thread(target=do_request)
        t.start()
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)

        transport.push_reply(InboundReply(
            request_id=rid, action="approve",
            responder="telegram:12345", comment="",
        ))
        t.join(timeout=5.0)

        # Give update() a beat to fire
        for _ in range(20):
            if transport.updates:
                break
            time.sleep(0.05)
        assert transport.updates, "transport.update was never called"
        _, status = transport.updates[0]
        assert "approved" in status.lower()

    def test_update_called_on_timeout(self, tmp_path):
        """Timeout should also trigger an update (⏱️ expired)."""
        transport = NullTransport(allowlist={"*"})
        daemon = BridgeDaemon(
            account="to", transport=transport,
            endpoint_file=tmp_path / ".maestro" / "bridge-to.endpoint",
        )
        daemon.start()
        try:
            url = f"http://127.0.0.1:{daemon.port}/approval"
            rid = "int-update-timeout"
            body = _approval_body(rid, timeout=1)
            resp = _post(url, body, token=daemon.token, timeout=10)
            data = json.loads(resp.read().decode("utf-8"))
            assert data["decision"] == "timeout"

            for _ in range(20):
                if transport.updates:
                    break
                time.sleep(0.05)
            assert transport.updates
            _, status = transport.updates[0]
            assert "expired" in status.lower()
        finally:
            daemon.stop()


# ---------------------------------------------------------------------------
# Bus watcher integration (T2d-2)


def _write_bus_msg(
    project_root: Path,
    stem: str,
    *,
    type: str = "spec-change-request",
    from_: str = "backend-agent",
    to: str = "human",
    priority: str = "normal",
    subject: str = "please approve",
) -> Path:
    bus = project_root / ".agents" / "bus" / "active"
    bus.mkdir(parents=True, exist_ok=True)
    p = bus / f"{stem}.md"
    p.write_text(
        f"---\n"
        f"id: {stem}\n"
        f"from: {from_}\n"
        f"to: {to}\n"
        f"priority: {priority}\n"
        f"type: {type}\n"
        f"timestamp: 2026-04-24T10:00:00Z\n"
        f"---\n\n## Subject: {subject}\n\nbody of the message\n",
        encoding="utf-8",
    )
    return p


class TestDaemonBusWatcher:
    """Daemon-side wiring for T2d-2: on start, a BusWatcher is spawned in
    the daemon's async loop; on stop, it's cancelled cleanly."""

    def test_daemon_spawns_watcher_and_surfaces_new_messages(self, tmp_path):
        """Drop a spec-change-request into .agents/bus/active/ and confirm
        it reaches the transport via the watcher (routed to send_approval
        post-T2d-3, with an entry registered in _pending_bus)."""
        project_root = tmp_path / "maestro"
        project_root.mkdir()
        (project_root / ".agents" / "bus" / "active").mkdir(parents=True)
        (project_root / "platform.yaml").write_text(
            "project: t2d\nversion: '1.0'\nrepos: []\n", encoding="utf-8",
        )

        transport = NullTransport(allowlist={"*"})
        daemon = BridgeDaemon(
            account="busint",
            transport=transport,
            endpoint_file=tmp_path / ".maestro" / "bridge-busint.endpoint",
            bus_watcher_root=project_root,
            bus_watcher_project="t2d-test",
        )
        # Speed up the poll so we don't wait 2s for the first scan.
        import otaman_bridge.bus_watcher as bw_mod
        original_poll = bw_mod.POLL_INTERVAL_SECONDS
        bw_mod.POLL_INTERVAL_SECONDS = 0.1
        try:
            daemon.start()
            try:
                scr_stem = "20260424T100000-scr-1"
                _write_bus_msg(project_root, scr_stem,
                               type="spec-change-request", to="human",
                               subject="approve endpoint v2")
                # Info broadcast should NOT surface (never rule).
                _write_bus_msg(project_root, "20260424T100000-broadcast",
                               type="info", to="all")

                # Interactive messages flow via send_approval.
                for _ in range(80):
                    if transport.sent_approvals:
                        break
                    time.sleep(0.05)
                assert transport.sent_approvals, \
                    "watcher should have surfaced the spec-change-request"
                req = transport.sent_approvals[0]
                assert req.request_id == scr_stem
                assert req.tool_name == "bus:spec-change-request"
                # Info broadcast must not have appeared.
                assert not any(
                    r.request_id.endswith("broadcast")
                    for r in transport.sent_approvals
                )

                # Pending registry should hold the bus decision context.
                assert scr_stem in daemon._pending_bus

                # Dedup state should now list the SCR.
                import json as _json
                state_file = project_root / ".otaman" / "bus-surfaced.state"
                assert state_file.is_file()
                state = _json.loads(state_file.read_text(encoding="utf-8"))
                assert scr_stem in state
            finally:
                daemon.stop()
        finally:
            bw_mod.POLL_INTERVAL_SECONDS = original_poll

        # Watcher must have been torn down — stopped event set, future gone.
        assert daemon._bus_watcher_future is None

    def test_daemon_without_watcher_root_does_not_start_watcher(self, tmp_path):
        """When bus_watcher_root is None, no watcher is spawned."""
        transport = NullTransport(allowlist={"*"})
        daemon = BridgeDaemon(
            account="nowatch",
            transport=transport,
            endpoint_file=tmp_path / ".maestro" / "bridge-nowatch.endpoint",
        )
        daemon.start()
        try:
            assert daemon._bus_watcher is None
            assert daemon._bus_watcher_future is None
        finally:
            daemon.stop()


# ---------------------------------------------------------------------------
# Bus decision buttons (T2d-3)


def _run_daemon_with_bus(tmp_path, account: str = "busdec"):
    """Helper: start a daemon with an active bus watcher pointed at tmp_path."""
    project_root = tmp_path / "maestro"
    (project_root / ".agents" / "bus" / "active").mkdir(parents=True)
    (project_root / "platform.yaml").write_text(
        "project: t2d3\nversion: '1.0'\nrepos: []\n", encoding="utf-8",
    )
    transport = NullTransport(allowlist={"*"})
    daemon = BridgeDaemon(
        account=account,
        transport=transport,
        endpoint_file=tmp_path / ".maestro" / f"bridge-{account}.endpoint",
        bus_watcher_root=project_root,
        bus_watcher_project="t2d3-test",
    )
    return daemon, transport, project_root


class TestBusDecisionButtons:
    """T2d-3: Approve/Reject taps on bus cards write the ack + broadcast."""

    def _fast_poll(self, monkeypatch):
        import otaman_bridge.bus_watcher as bw_mod
        monkeypatch.setattr(bw_mod, "POLL_INTERVAL_SECONDS", 0.1)

    def test_approve_writes_ack_and_broadcast(self, tmp_path, monkeypatch):
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path)
        daemon.start()
        try:
            stem = "20260424T100000-backend-to-human-approve"
            _write_bus_msg(project_root, stem, subject="pagination v1")

            # Wait for the watcher to surface as an approval.
            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            assert transport.sent_approvals, "watcher should send_approval for SCR"
            req = transport.sent_approvals[0]
            assert req.request_id == stem
            assert req.tool_name == "bus:spec-change-request"

            # Tap Approve.
            transport.push_reply(InboundReply(
                request_id=stem,
                action="approve",
                responder="telegram:roman",
                comment="",
            ))

            # Ack file appears.
            ack = project_root / ".agents" / "bus" / "active" / "acks" / f"{stem}.human.ack"
            for _ in range(40):
                if ack.is_file():
                    break
                time.sleep(0.05)
            assert ack.is_file(), "approve tap should write human.ack"
            assert ack.read_text(encoding="utf-8").strip() == "approved"

            # Broadcast file appears.
            active = project_root / ".agents" / "bus" / "active"
            approved = list(active.glob("*-human-to-all-spec-change-approved.md"))
            assert approved, "approve tap should broadcast spec-change-approved"
            content = approved[0].read_text(encoding="utf-8")
            assert stem in content
            assert "telegram:roman" in content
        finally:
            daemon.stop()

    def test_reject_writes_rejection_to_proposer(self, tmp_path, monkeypatch):
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "busrej")
        daemon.start()
        try:
            stem = "20260424T100000-frontend-to-human-reject"
            _write_bus_msg(project_root, stem,
                           from_="frontend-agent",
                           subject="react 19 upgrade")

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            assert transport.sent_approvals

            transport.push_reply(InboundReply(
                request_id=stem,
                action="reject",
                responder="telegram:roman",
                comment="not now, focus on MVP",
            ))

            ack = project_root / ".agents" / "bus" / "active" / "acks" / f"{stem}.human.ack"
            for _ in range(40):
                if ack.is_file():
                    break
                time.sleep(0.05)
            assert ack.read_text(encoding="utf-8").strip() == "rejected"

            active = project_root / ".agents" / "bus" / "active"
            rejected = list(active.glob(
                "*-human-to-frontend-agent-spec-change-rejected.md"
            ))
            assert rejected, "reject tap should broadcast to the original proposer"
            content = rejected[0].read_text(encoding="utf-8")
            assert "not now, focus on MVP" in content
        finally:
            daemon.stop()

    def test_second_tap_after_approve_is_noop(self, tmp_path, monkeypatch):
        """Once decided, tapping again doesn't overwrite or duplicate."""
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "busdup")
        daemon.start()
        try:
            stem = "20260424T100000-a-to-human-dup"
            _write_bus_msg(project_root, stem)

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)

            transport.push_reply(InboundReply(
                request_id=stem, action="approve",
                responder="tg:one", comment="",
            ))
            time.sleep(0.3)
            approved_files_first = list(
                (project_root / ".agents" / "bus" / "active")
                .glob("*-spec-change-approved.md")
            )
            assert len(approved_files_first) == 1

            # Second tap — should be ignored (registry entry cleared).
            transport.push_reply(InboundReply(
                request_id=stem, action="reject",
                responder="tg:two", comment="",
            ))
            time.sleep(0.3)

            approved_files_second = list(
                (project_root / ".agents" / "bus" / "active")
                .glob("*-spec-change-approved.md")
            )
            rejected_files = list(
                (project_root / ".agents" / "bus" / "active")
                .glob("*-spec-change-rejected.md")
            )
            assert len(approved_files_second) == 1
            assert not rejected_files, \
                "second tap must not create a conflicting decision"
        finally:
            daemon.stop()

    def test_details_tap_does_not_resolve_bus_decision(self, tmp_path, monkeypatch):
        """Details shows the full payload but leaves the decision pending."""
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "busdet")
        daemon.start()
        try:
            stem = "20260424T100000-a-to-human-det"
            _write_bus_msg(project_root, stem, subject="endpoint refactor")

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)

            transport.push_reply(InboundReply(
                request_id=stem, action="details",
                responder="tg:roman", comment="",
            ))
            # Wait for send_info follow-up
            for _ in range(40):
                if transport.sent_infos:
                    break
                time.sleep(0.05)
            assert transport.sent_infos, "details should send a follow-up info"

            # No ack should exist yet.
            ack = (project_root / ".agents" / "bus" / "active" / "acks"
                   / f"{stem}.human.ack")
            assert not ack.exists(), \
                "details tap must not resolve the decision"

            # Pending entry should still be registered.
            assert stem in daemon._pending_bus

            # Clean up: approve so the test exits with a clean state.
            transport.push_reply(InboundReply(
                request_id=stem, action="approve",
                responder="tg:roman", comment="",
            ))
            for _ in range(40):
                if ack.is_file():
                    break
                time.sleep(0.05)
            assert ack.is_file()
        finally:
            daemon.stop()


class TestBusCommentAndAcknowledge:
    """T2d-4: comment writes a reply bus message; acknowledge closes a
    `to: human` card."""

    def _fast_poll(self, monkeypatch):
        import otaman_bridge.bus_watcher as bw_mod
        monkeypatch.setattr(bw_mod, "POLL_INTERVAL_SECONDS", 0.1)

    def test_comment_on_scr_writes_reply_and_keeps_pending(self, tmp_path, monkeypatch):
        """Comment on an SCR: reply bus message written, decision still pending."""
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "buscomment")
        daemon.start()
        try:
            stem = "20260424T100000-backend-to-human-comment"
            _write_bus_msg(project_root, stem,
                           type="spec-change-request",
                           from_="backend-agent",
                           to="human",
                           subject="pagination v1")

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            assert transport.sent_approvals
            assert stem in daemon._pending_bus

            # Send a comment (reply text).
            transport.push_reply(InboundReply(
                request_id=stem,
                action="comment",
                responder="telegram:roman",
                comment="Use cursor-based pagination, not offset.",
            ))

            active = project_root / ".agents" / "bus" / "active"
            for _ in range(40):
                replies = list(active.glob("*-reply.md"))
                if replies:
                    break
                time.sleep(0.05)
            replies = list(active.glob("*-reply.md"))
            assert replies, "comment should produce a reply bus message"
            reply_content = replies[0].read_text(encoding="utf-8")
            assert "cursor-based pagination" in reply_content
            assert "to: backend-agent" in reply_content
            assert f"in_reply_to: {stem}" in reply_content

            # Decision must still be pending — no ack yet.
            ack = (project_root / ".agents" / "bus" / "active" / "acks"
                   / f"{stem}.human.ack")
            assert not ack.exists(), \
                "comment must not resolve the decision"
            assert stem in daemon._pending_bus

            # Now follow-up with an approve so the test state is clean.
            transport.push_reply(InboundReply(
                request_id=stem, action="approve",
                responder="telegram:roman", comment="",
            ))
            for _ in range(40):
                if ack.is_file():
                    break
                time.sleep(0.05)
            assert ack.is_file()
        finally:
            daemon.stop()

    def test_empty_comment_is_noop(self, tmp_path, monkeypatch):
        """Whitespace-only comments don't create reply files."""
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "busempty")
        daemon.start()
        try:
            stem = "20260424T100000-a-to-human-empty"
            _write_bus_msg(project_root, stem)

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)

            transport.push_reply(InboundReply(
                request_id=stem, action="comment",
                responder="tg:roman", comment="   ",
            ))
            time.sleep(0.3)

            active = project_root / ".agents" / "bus" / "active"
            replies = list(active.glob("*-reply.md"))
            assert not replies, "empty comment must not create a reply file"
        finally:
            daemon.stop()

    def test_approve_on_non_scr_writes_acknowledge(self, tmp_path, monkeypatch):
        """For `to: human` non-SCR cards, Approve → acknowledge (not
        a spec-change-approved broadcast)."""
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "busack")
        daemon.start()
        try:
            # Urgent to-human message: always surfaces interactive, but
            # type != spec-change-request so approve should be acknowledge.
            stem = "20260424T100000-ops-to-human-urgent"
            _write_bus_msg(project_root, stem,
                           type="info",
                           from_="ops-agent",
                           to="human",
                           priority="urgent",
                           subject="prod pager fired")

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            assert transport.sent_approvals

            transport.push_reply(InboundReply(
                request_id=stem, action="approve",
                responder="telegram:roman", comment="",
            ))

            ack = (project_root / ".agents" / "bus" / "active" / "acks"
                   / f"{stem}.human.ack")
            for _ in range(40):
                if ack.is_file():
                    break
                time.sleep(0.05)
            assert ack.is_file()
            assert ack.read_text(encoding="utf-8").strip() == "acknowledged"

            # No spec-change-approved broadcast should exist.
            active = project_root / ".agents" / "bus" / "active"
            scr_approvals = list(active.glob("*-spec-change-approved.md"))
            assert not scr_approvals, \
                "non-SCR card must not create spec-change-approved"
        finally:
            daemon.stop()

    def test_acknowledge_action_with_comment_writes_ack_and_reply(
        self, tmp_path, monkeypatch,
    ):
        """Acknowledge + comment: ack file + reply message."""
        self._fast_poll(monkeypatch)
        daemon, transport, project_root = _run_daemon_with_bus(tmp_path, "busackc")
        daemon.start()
        try:
            stem = "20260424T100000-ops-to-human-ack"
            _write_bus_msg(project_root, stem,
                           type="info",
                           from_="ops-agent",
                           to="human",
                           priority="urgent")

            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)

            transport.push_reply(InboundReply(
                request_id=stem, action="acknowledge",
                responder="telegram:roman",
                comment="rolling back now",
            ))

            ack = (project_root / ".agents" / "bus" / "active" / "acks"
                   / f"{stem}.human.ack")
            for _ in range(40):
                if ack.is_file():
                    break
                time.sleep(0.05)
            assert ack.is_file()
            assert ack.read_text(encoding="utf-8").strip() == "acknowledged"

            # Comment should also have written a reply message.
            active = project_root / ".agents" / "bus" / "active"
            replies = list(active.glob("*-human-to-ops-agent-reply.md"))
            assert replies
            assert "rolling back now" in replies[0].read_text(encoding="utf-8")
        finally:
            daemon.stop()
