"""End-to-end integration test for auto-session-spawn (task 5.1).

Requires a live runner daemon (``~/.otaman/runner.endpoint`` must exist and be
reachable). Skip automatically when the runner is not available.

What this test exercises (all components assembled):
1. ``FileSystemEventSource`` — detects a new .md file in the bus active/ dir
2. ``handle_bus_event`` / ``spawn_decision`` — parses task-assignment, dedup check
3. ``RunnerClient.spawn`` — live HTTP call to the runner's POST /spawn
4. Runner — spawns a real headless Claude Code session with ``OTAMAN_AGENT=spec-agent``
5. ``SqliteSessionRegistry`` — claim on spawn, dedup on second identical message
6. Dedup — dropping same task-assignment a second time produces no second spawn

The runner endpoint is read from ``~/.otaman/runner.endpoint`` at test-collection
time. If missing or unreachable the whole module is skipped.

Note: the spawned session runs ``claude --headless`` in the spec-agent context.
It is killed via ``/kill`` at teardown regardless of its exit status.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path("/home/romans/otaman/otaman-meta").resolve())
PLATFORM_YAML = str(Path("/home/romans/otaman/otaman-meta/platform.yaml").resolve())
OWNED_AGENTS = {"otaman-specs": "spec-agent"}  # repo -> agent_id for this test
ENDPOINT_FILE = Path.home() / ".otaman" / "runner.endpoint"

# ---------------------------------------------------------------------------
# Skip logic — runner must be live
# ---------------------------------------------------------------------------


def _read_endpoint():
    if not ENDPOINT_FILE.is_file():
        return None
    ep = {}
    for line in ENDPOINT_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            ep[k.strip()] = v.strip()
    return ep if ep.get("port") and ep.get("token") else None


def _runner_alive(ep: dict) -> bool:
    url = f"http://{ep.get('host', '127.0.0.1')}:{ep['port']}/sessions"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {ep['token']}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


_EP = _read_endpoint()
pytestmark = pytest.mark.skipif(
    _EP is None or not _runner_alive(_EP),
    reason="live runner not reachable — skipping e2e tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner_request(ep: dict, method: str, path: str, body: dict | None = None):
    url = f"http://{ep.get('host', '127.0.0.1')}:{ep['port']}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"Bearer {ep['token']}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode()
        return exc.code, json.loads(body_txt) if body_txt else {}


def _write_task_assignment(bus_active: Path, *, stem: str, change: str, human: str = "roman") -> Path:
    content = (
        f"---\n"
        f"id: {stem}\n"
        f"from: {human}\n"
        f"to: spec-agent\n"
        f"priority: normal\n"
        f"type: task-assignment\n"
        f"timestamp: 2026-06-08T00:00:00Z\n"
        f"status: pending\n"
        f"change: {change}\n"
        f"---\n"
        f"\n"
        f"## Subject: Tasks assigned from \"{change}\"\n"
        f"\n"
        f"- [ ] 1.1 @otaman-specs [headless] Run `/otaman:check` and report status. *(e2e-test)*\n"
    )
    path = bus_active / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ep():
    return _EP


@pytest.fixture
def bus_dir(tmp_path):
    active = tmp_path / ".agents" / "bus" / "active"
    active.mkdir(parents=True)
    return tmp_path / ".agents" / "bus"


@pytest.fixture
def registry(tmp_path):
    from otaman_bridge.session_registry import SqliteSessionRegistry
    r = SqliteSessionRegistry(db_path=tmp_path / "e2e-sessions.db")
    yield r
    r.close()


@pytest.fixture
def runner_client():
    from otaman_bridge.runner_client import RunnerClient
    return RunnerClient()


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------


class TestE2ESpawnDecision:

    def test_spawn_decision_calls_runner_and_claims_session(
        self, ep, bus_dir, registry, runner_client
    ):
        """Dropping a task-assignment → spawn_decision calls runner; session claimed."""
        from otaman_bridge.spawn_decision import handle_bus_event

        path = _write_task_assignment(
            bus_dir / "active",
            stem="20260608T000001-e2e-spawn-test",
            change="e2e-spawn-test",
        )

        t0 = time.monotonic()
        outcome = handle_bus_event(
            path,
            registry=registry,
            runner_client=runner_client,
            owned_agents=OWNED_AGENTS,
            bus_dir=bus_dir,
            project_root=PROJECT_ROOT,
            this_agent="spec-agent",
            trigger_source="e2e-test",
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        # Spawn decision must complete quickly
        assert elapsed_ms < 5000, f"spawn_decision took {elapsed_ms:.0f}ms (limit 5s)"

        assert outcome is not None, "handle_bus_event returned None (message skipped)"
        assert outcome.action == "spawned", f"Expected 'spawned', got {outcome.action!r}"
        assert outcome.mode == "headless"
        assert outcome.session_id is not None

        # Registry must reflect the claim
        assert registry.is_sessioned("spec-agent", "roman"), \
            "Session not recorded in registry after spawn"

        # Runner must show the session
        status, data = _runner_request(ep, "GET", "/sessions")
        assert status == 200
        session_ids = [s["session_id"] for s in data.get("sessions", [])]
        assert outcome.session_id in session_ids, \
            f"Session {outcome.session_id} not in runner /sessions: {session_ids}"

        # Cleanup — kill the spawned session
        _runner_request(ep, "POST", "/kill", {"session_id": outcome.session_id})
        registry.release_session("spec-agent", "roman", outcome.session_id)

    def test_dedup_no_second_spawn_when_session_warm(
        self, ep, bus_dir, registry, runner_client
    ):
        """Second identical task-assignment → warm-session, no second runner call."""
        from otaman_bridge.spawn_decision import handle_bus_event

        p1 = _write_task_assignment(
            bus_dir / "active",
            stem="20260608T000002-e2e-dedup-first",
            change="e2e-dedup-test",
        )
        p2 = _write_task_assignment(
            bus_dir / "active",
            stem="20260608T000003-e2e-dedup-second",
            change="e2e-dedup-test",
        )

        o1 = handle_bus_event(
            p1,
            registry=registry,
            runner_client=runner_client,
            owned_agents=OWNED_AGENTS,
            bus_dir=bus_dir,
            project_root=PROJECT_ROOT,
            this_agent="spec-agent",
            trigger_source="e2e-test",
        )
        assert o1 is not None and o1.action == "spawned"

        o2 = handle_bus_event(
            p2,
            registry=registry,
            runner_client=runner_client,
            owned_agents=OWNED_AGENTS,
            bus_dir=bus_dir,
            project_root=PROJECT_ROOT,
            this_agent="spec-agent",
            trigger_source="e2e-test",
        )
        assert o2 is not None
        assert o2.action == "warm-session", \
            f"Expected 'warm-session' for duplicate, got {o2.action!r}"

        # Runner must still have only one session for spec-agent/roman (new ones)
        status, data = _runner_request(ep, "GET", "/sessions")
        assert status == 200
        spec_sessions = [
            s for s in data.get("sessions", [])
            if s.get("agent") == "spec-agent" and s.get("user") == "roman"
        ]
        assert len(spec_sessions) == 1, \
            f"Expected 1 spec-agent session, got {len(spec_sessions)}"

        # Cleanup
        _runner_request(ep, "POST", "/kill", {"session_id": o1.session_id})
        registry.release_session("spec-agent", "roman", o1.session_id)

    def test_filesystem_event_source_detects_file_within_500ms(self, bus_dir):
        """FileSystemEventSource delivers the event within 500ms of file creation."""
        from otaman_bridge.triggers.filesystem import BusFileEvent, FileSystemEventSource

        received: list[BusFileEvent] = []

        def handler(evt: BusFileEvent) -> None:
            received.append(evt)

        src = FileSystemEventSource(bus_dir / "active", handler)
        src.start()
        try:
            path = _write_task_assignment(
                bus_dir / "active",
                stem="20260608T000004-e2e-fsevt",
                change="e2e-fs-test",
            )
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not received:
                time.sleep(0.02)

            assert received, "FileSystemEventSource did not deliver event within 500ms"
            assert received[0].path == path
        finally:
            src.stop()
