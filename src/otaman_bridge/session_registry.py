from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol (M-7 migration seam)
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionRegistry(Protocol):
    # Protocol interface for the session registry.
    # The SQLite implementation is the Mode-1 concrete backend.
    # When M-7 (stateless bridge) lands, a new backend (PostgreSQL / NATS-KV)
    # implements this same protocol; callers (spawn_decision.py) are unchanged.

    def is_sessioned(self, agent: str, human: str) -> bool:
        # Return True if an active session exists for (agent, human).
        ...

    def claim_session(
        self,
        agent: str,
        human: str,
        session_id: str,
        *,
        ttl_seconds: float = 3600.0,
    ) -> bool:
        # Atomically claim a session slot for (agent, human).
        # Returns True if the slot was acquired; False if already occupied.
        # Sets the session_id and an expiry based on ttl_seconds.
        ...

    def release_session(self, agent: str, human: str, session_id: str) -> bool:
        # Release the session slot for (agent, human).
        # Returns True if the slot was released; False if it was not claimed
        # by this session_id (or had already expired).
        ...

    def heartbeat(
        self,
        agent: str,
        human: str,
        session_id: str,
        *,
        ttl_seconds: float = 3600.0,
    ) -> bool:
        # Renew the TTL on an active session. Returns True on success.
        ...

    def cleanup_expired(self) -> int:
        # Remove all expired sessions. Returns the count removed.
        ...


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


@dataclass
class SqliteSessionRegistry:
    # Mode-1 session registry backed by SQLite.
    #
    # Design notes (per auto-session-spawn design.md Q5):
    #
    # 1. The session registry is a CACHE of authoritative bus state, not the
    #    source of truth. Rows are transient; they expire and are reconstructed
    #    from bus state if the daemon restarts.
    #
    # 2. is_sessioned() / claim_session() / release_session() form the dedup
    #    primitive. The spawn-decision component calls these to enforce
    #    "at most one session per (agent, human) pair".
    #
    # 3. claim_session() is idempotent with the same session_id; a re-spawn
    #    of an already-sessioned agent returns False so the caller can no-op.
    #
    # 4. M-7 migration: SqliteSessionRegistry can be swapped for a
    #    PostgresSessionRegistry or NatsKVSessionRegistry that implements the
    #    same SessionRegistry protocol. The db_path moves from a local file to
    #    an external connection string. No callers change.

    db_path: Path

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Public API (satisfies SessionRegistry protocol)
    # ------------------------------------------------------------------

    def is_sessioned(self, agent: str, human: str) -> bool:
        with self._lock:
            row = self._exec_one(
                "SELECT 1 FROM sessions WHERE agent=? AND human=? AND expires_at > ?",
                (agent, human, time.time()),
            )
            return row is not None

    def claim_session(
        self,
        agent: str,
        human: str,
        session_id: str,
        *,
        ttl_seconds: float = 3600.0,
    ) -> bool:
        # Atomic upsert: if (agent, human) is already claimed by a DIFFERENT
        # session_id and not expired, return False. Otherwise insert/update.
        with self._lock:
            expires_at = time.time() + ttl_seconds
            now = time.time()
            # Check for existing non-expired claim by a different session_id.
            existing = self._exec_one(
                "SELECT session_id FROM sessions WHERE agent=? AND human=? AND expires_at > ?",
                (agent, human, now),
            )
            if existing and existing[0] != session_id:
                return False  # already owned by a different session
            # Upsert (replace if same session_id or expired).
            self._conn.execute(  # type: ignore[union-attr]
                "INSERT OR REPLACE INTO sessions (agent, human, session_id, claimed_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (agent, human, session_id, now, expires_at),
            )
            self._conn.commit()  # type: ignore[union-attr]
            return True

    def release_session(self, agent: str, human: str, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM sessions WHERE agent=? AND human=? AND session_id=?",
                (agent, human, session_id),
            )
            self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    def heartbeat(
        self,
        agent: str,
        human: str,
        session_id: str,
        *,
        ttl_seconds: float = 3600.0,
    ) -> bool:
        with self._lock:
            expires_at = time.time() + ttl_seconds
            cur = self._conn.execute(  # type: ignore[union-attr]
                "UPDATE sessions SET expires_at=? WHERE agent=? AND human=? AND session_id=?",
                (expires_at, agent, human, session_id),
            )
            self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    def cleanup_expired(self) -> int:
        with self._lock:
            cur = self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM sessions WHERE expires_at <= ?", (time.time(),)
            )
            self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount

    def list_active(self) -> list[dict]:
        # Returns all non-expired sessions. Useful for audit / diagnostics.
        with self._lock:
            rows = self._conn.execute(  # type: ignore[union-attr]
                "SELECT agent, human, session_id, claimed_at, expires_at"
                " FROM sessions WHERE expires_at > ? ORDER BY claimed_at",
                (time.time(),),
            ).fetchall()
            return [
                {"agent": r[0], "human": r[1], "session_id": r[2],
                 "claimed_at": r[3], "expires_at": r[4]}
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                agent       TEXT NOT NULL,
                human       TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                claimed_at  REAL NOT NULL,   -- unix timestamp
                expires_at  REAL NOT NULL,   -- unix timestamp; row is stale when < now()
                PRIMARY KEY (agent, human)
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expires ON sessions (expires_at)"
        )
        conn.commit()
        self._conn = conn

    def _exec_one(self, sql: str, params: tuple) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()  # type: ignore[union-attr]

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
