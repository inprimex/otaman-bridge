# Tests for SqliteSessionRegistry -- updated for heartbeat-based schema (task 1.3).
# Schema: sessions(agent_id, human_id, session_id, mode, claimed_at, heartbeat_at)
# Expiry is driven by cleanup_stale(stale_threshold_seconds), NOT by a per-row TTL.
from __future__ import annotations

import concurrent.futures
import time

import pytest

from otaman_bridge.session_registry import SessionRegistry, SqliteSessionRegistry


@pytest.fixture
def registry(tmp_path):
    r = SqliteSessionRegistry(db_path=tmp_path / "sessions.db")
    yield r
    r.close()


class TestSessionRegistryProtocol:
    def test_sqlite_satisfies_protocol(self, registry):
        assert isinstance(registry, SessionRegistry)


class TestIsSessioned:
    def test_false_when_nothing_claimed(self, registry):
        assert registry.is_sessioned("cli-agent", "roman") is False

    def test_true_after_claim(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        assert registry.is_sessioned("cli-agent", "roman") is True

    def test_false_after_release(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        registry.release_session("cli-agent", "roman", "sess-001")
        assert registry.is_sessioned("cli-agent", "roman") is False

    def test_false_after_cleanup_stale(self, tmp_path):
        # Claim, wait, then cleanup_stale with threshold smaller than elapsed time.
        r = SqliteSessionRegistry(db_path=tmp_path / "stale.db")
        r.claim_session("cli-agent", "roman", "sess-exp")
        time.sleep(0.08)
        r.cleanup_stale(stale_threshold_seconds=0.04)
        assert r.is_sessioned("cli-agent", "roman") is False
        r.close()


class TestClaimSession:
    def test_first_claim_returns_true(self, registry):
        assert registry.claim_session("cli-agent", "roman", "sess-001") is True

    def test_second_claim_same_session_id_returns_true(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        assert registry.claim_session("cli-agent", "roman", "sess-001") is True

    def test_second_claim_different_session_id_returns_false(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        assert registry.claim_session("cli-agent", "roman", "sess-002") is False

    def test_claim_after_stale_cleanup_returns_true(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "stale.db")
        r.claim_session("cli-agent", "roman", "sess-001")
        time.sleep(0.08)
        r.cleanup_stale(stale_threshold_seconds=0.04)
        assert r.claim_session("cli-agent", "roman", "sess-002") is True
        r.close()

    def test_different_pairs_independent(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-a")
        assert registry.claim_session("spec-agent", "roman", "sess-b") is True
        assert registry.claim_session("cli-agent", "alice", "sess-c") is True

    def test_mode_stored_and_returned(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001", mode="headless")
        active = registry.list_active()
        assert len(active) == 1
        assert active[0]["mode"] == "headless"

    def test_mode_defaults_to_headless(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        active = registry.list_active()
        assert active[0]["mode"] == "headless"


class TestReleaseSession:
    def test_returns_true_on_success(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        assert registry.release_session("cli-agent", "roman", "sess-001") is True

    def test_returns_false_wrong_session_id(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        assert registry.release_session("cli-agent", "roman", "sess-999") is False

    def test_returns_false_when_not_claimed(self, registry):
        assert registry.release_session("cli-agent", "roman", "sess-001") is False


class TestHeartbeat:
    def test_returns_true_for_known_session(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-hb")
        assert registry.heartbeat("cli-agent", "roman", "sess-hb") is True

    def test_updates_heartbeat_at(self, tmp_path):
        # Claim, age out via cleanup_stale, then heartbeat prevents the next cleanup.
        r = SqliteSessionRegistry(db_path=tmp_path / "hb.db")
        r.claim_session("cli-agent", "roman", "sess-hb")
        time.sleep(0.08)
        # Heartbeat refreshes heartbeat_at to now
        r.heartbeat("cli-agent", "roman", "sess-hb")
        # cleanup_stale with threshold < elapsed since heartbeat (nearly 0 ms) should NOT remove it
        r.cleanup_stale(stale_threshold_seconds=0.04)
        assert r.is_sessioned("cli-agent", "roman") is True
        r.close()

    def test_without_heartbeat_cleanup_removes_row(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "nohb.db")
        r.claim_session("cli-agent", "roman", "sess-nohb")
        time.sleep(0.08)
        # No heartbeat — cleanup_stale removes the stale row
        r.cleanup_stale(stale_threshold_seconds=0.04)
        assert r.is_sessioned("cli-agent", "roman") is False
        r.close()

    def test_returns_false_for_unknown_session(self, registry):
        assert registry.heartbeat("cli-agent", "roman", "sess-ghost") is False


class TestCleanupStale:
    def test_removes_stale_rows(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "gc.db")
        r.claim_session("a1", "roman", "s1")
        r.claim_session("a2", "roman", "s2")
        r.claim_session("a3", "roman", "s3")
        time.sleep(0.08)
        # Refresh a3's heartbeat
        r.heartbeat("a3", "roman", "s3")
        removed = r.cleanup_stale(stale_threshold_seconds=0.04)
        assert removed == 2
        active = r.list_active()
        assert len(active) == 1
        assert active[0]["session_id"] == "s3"
        r.close()

    def test_keeps_fresh_rows(self, registry):
        registry.claim_session("a1", "roman", "s1")
        registry.claim_session("a2", "roman", "s2")
        removed = registry.cleanup_stale(stale_threshold_seconds=3600.0)
        assert removed == 0
        assert len(registry.list_active()) == 2


class TestListActive:
    def test_empty_when_nothing_claimed(self, registry):
        assert registry.list_active() == []

    def test_contains_claimed_sessions(self, registry):
        registry.claim_session("a1", "roman", "s1", mode="headless")
        registry.claim_session("a2", "alice", "s2", mode="interactive")
        active = registry.list_active()
        assert len(active) == 2
        ids = {r["session_id"] for r in active}
        assert ids == {"s1", "s2"}

    def test_has_expected_fields(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-001")
        row = registry.list_active()[0]
        assert set(row.keys()) == {
            "agent_id",
            "human_id",
            "session_id",
            "mode",
            "claimed_at",
            "heartbeat_at",
        }


class TestRaceConditions:
    def test_concurrent_claim_only_one_wins(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "race.db")
        results = []
        lock = __import__("threading").Lock()

        def try_claim(i: int) -> None:
            ok = r.claim_session("cli-agent", "roman", f"sess-{i:03d}")
            with lock:
                results.append(ok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futs = [ex.submit(try_claim, i) for i in range(20)]
            concurrent.futures.wait(futs)

        r.close()
        wins = [x for x in results if x is True]
        print(f"\nConcurrent claim: {len(wins)}/20 won")
        assert len(wins) == 1, f"Expected exactly 1 winner, got {len(wins)}"

    def test_concurrent_release_and_reclaim(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "reclaim.db")
        r.claim_session("cli-agent", "roman", "sess-original")

        release_wins = []
        claim_wins = []
        lock = __import__("threading").Lock()

        def releaser():
            ok = r.release_session("cli-agent", "roman", "sess-original")
            with lock:
                release_wins.append(ok)

        def reclaimer(i: int):
            ok = r.claim_session("cli-agent", "roman", f"sess-new-{i}")
            with lock:
                claim_wins.append(ok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=11) as ex:
            futs = [ex.submit(releaser)] + [ex.submit(reclaimer, i) for i in range(10)]
            concurrent.futures.wait(futs)

        r.close()
        print(
            f"\nRelease+reclaim: release={release_wins}, "
            f"claim_wins={sum(1 for x in claim_wins if x)}"
        )
        assert sum(1 for x in claim_wins if x) <= 1
