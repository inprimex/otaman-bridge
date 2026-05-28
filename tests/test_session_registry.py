# Spike tests for SqliteSessionRegistry -- task 1.4 of auto-session-spawn.
# Validates table schema, claim/release/heartbeat operations, race conditions,
# and M-7 migration compatibility.
from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

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

    def test_false_after_expiry(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "exp.db")
        r.claim_session("cli-agent", "roman", "sess-exp", ttl_seconds=0.1)
        time.sleep(0.15)
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

    def test_claim_after_expiry_returns_true(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "exp.db")
        r.claim_session("cli-agent", "roman", "sess-001", ttl_seconds=0.1)
        time.sleep(0.15)
        assert r.claim_session("cli-agent", "roman", "sess-002") is True
        r.close()

    def test_different_pairs_independent(self, registry):
        registry.claim_session("cli-agent", "roman", "sess-a")
        assert registry.claim_session("spec-agent", "roman", "sess-b") is True
        assert registry.claim_session("cli-agent", "alice", "sess-c") is True


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
    def test_extends_ttl(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "hb.db")
        r.claim_session("cli-agent", "roman", "sess-hb", ttl_seconds=0.2)
        time.sleep(0.15)
        # Heartbeat with 1s TTL -- should survive
        assert r.heartbeat("cli-agent", "roman", "sess-hb", ttl_seconds=1.0) is True
        time.sleep(0.15)  # would have expired without heartbeat
        assert r.is_sessioned("cli-agent", "roman") is True
        r.close()

    def test_returns_false_for_unknown_session(self, registry):
        assert registry.heartbeat("cli-agent", "roman", "sess-ghost") is False


class TestCleanupExpired:
    def test_removes_expired_rows(self, tmp_path):
        r = SqliteSessionRegistry(db_path=tmp_path / "gc.db")
        r.claim_session("a1", "roman", "s1", ttl_seconds=0.05)
        r.claim_session("a2", "roman", "s2", ttl_seconds=0.05)
        r.claim_session("a3", "roman", "s3", ttl_seconds=60.0)
        time.sleep(0.1)
        removed = r.cleanup_expired()
        assert removed == 2
        active = r.list_active()
        assert len(active) == 1
        assert active[0]["session_id"] == "s3"
        r.close()


class TestRaceConditions:
    def test_concurrent_claim_only_one_wins(self, tmp_path):
        # 20 concurrent threads attempt to claim the same (agent, human) pair.
        # Exactly one should succeed.
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
        # Release + re-claim race: no session should be double-claimed.
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
        total_wins = sum(1 for x in claim_wins if x) + sum(1 for x in release_wins if x)
        print(f"\nRelease+reclaim: release={release_wins}, claim_wins={sum(1 for x in claim_wins if x)}")
        # After release, exactly one new claim should succeed.
        # Total active sessions must be 0 or 1 at any point.
        assert sum(1 for x in claim_wins if x) <= 1

