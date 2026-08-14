"""Session registry — dedup gate for auto-session-spawn.

``SessionRegistry`` is a protocol; ``SqliteSessionRegistry`` is the Mode-1
concrete backend. ``NatsKvSessionRegistry`` is a stub for the Mode-2+ swap.

Schema (task 1.3):
    sessions(agent_id TEXT, human_id TEXT, session_id TEXT, mode TEXT,
             claimed_at TEXT, heartbeat_at TEXT, PRIMARY KEY(agent_id, human_id))

All timestamps are ISO-8601 UTC strings.  Stale rows (no heartbeat for
``stale_threshold_seconds``, default 2 h) are pruned by ``cleanup_stale()``.

The registry is a *cache* of authoritative bus state (per design.md insight
from prior-art survey).  If the bridge restarts, ``process_pending()`` on the
event source reconstructs live sessions from the bus.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_DB_PATH = Path.home() / ".otaman" / "session-registry.db"
DEFAULT_STALE_THRESHOLD = 2 * 3600.0  # seconds; rows older than this are pruned


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _iso_to_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# ---------------------------------------------------------------------------
# Protocol (M-7 migration seam)
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionRegistry(Protocol):
    def is_sessioned(self, agent_id: str, human_id: str) -> bool: ...

    def claim_session(
        self,
        agent_id: str,
        human_id: str,
        session_id: str,
        *,
        mode: str = "headless",
    ) -> bool: ...

    def release_session(self, agent_id: str, human_id: str, session_id: str) -> bool: ...

    def heartbeat(self, agent_id: str, human_id: str, session_id: str) -> bool: ...

    def cleanup_stale(self, *, stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD) -> int: ...


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


@dataclass
class SqliteSessionRegistry:
    """Mode-1 session registry backed by WAL-mode SQLite.

    Atomic ``claim_session`` uses RLock + check-before-upsert.
    ``heartbeat`` updates ``heartbeat_at`` so the linger timer can measure
    inactivity without a separate expires_at column.
    """

    db_path: Path = DEFAULT_DB_PATH

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_sessioned(self, agent_id: str, human_id: str) -> bool:
        with self._lock:
            row = self._one(
                "SELECT 1 FROM sessions WHERE agent_id=? AND human_id=?",
                (agent_id, human_id),
            )
            return row is not None

    def claim_session(
        self,
        agent_id: str,
        human_id: str,
        session_id: str,
        *,
        mode: str = "headless",
    ) -> bool:
        with self._lock:
            existing = self._one(
                "SELECT session_id FROM sessions WHERE agent_id=? AND human_id=?",
                (agent_id, human_id),
            )
            if existing and existing[0] != session_id:
                return False
            now = _now_iso()
            self._conn.execute(  # type: ignore[union-attr]
                "INSERT OR REPLACE INTO sessions"
                " (agent_id, human_id, session_id, mode, claimed_at, heartbeat_at)"
                " VALUES (?,?,?,?,?,?)",
                (agent_id, human_id, session_id, mode, now, now),
            )
            self._conn.commit()  # type: ignore[union-attr]
            return True

    def release_session(self, agent_id: str, human_id: str, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM sessions WHERE agent_id=? AND human_id=? AND session_id=?",
                (agent_id, human_id, session_id),
            )
            self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    def heartbeat(self, agent_id: str, human_id: str, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(  # type: ignore[union-attr]
                "UPDATE sessions SET heartbeat_at=? "
                "WHERE agent_id=? AND human_id=? AND session_id=?",
                (_now_iso(), agent_id, human_id, session_id),
            )
            self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    def cleanup_stale(self, *, stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD) -> int:
        """Remove rows where heartbeat_at is older than stale_threshold_seconds."""
        import time

        cutoff = datetime.fromtimestamp(
            time.time() - stale_threshold_seconds, tz=timezone.utc
        ).isoformat()
        with self._lock:
            cur = self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM sessions WHERE heartbeat_at < ?", (cutoff,)
            )
            self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount

    def list_active(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(  # type: ignore[union-attr]
                "SELECT agent_id, human_id, session_id, mode, claimed_at, heartbeat_at"
                " FROM sessions ORDER BY claimed_at"
            ).fetchall()
            return [
                {
                    "agent_id": r[0],
                    "human_id": r[1],
                    "session_id": r[2],
                    "mode": r[3],
                    "claimed_at": r[4],
                    "heartbeat_at": r[5],
                }
                for r in rows
            ]

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                agent_id    TEXT NOT NULL,
                human_id    TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                mode        TEXT NOT NULL DEFAULT 'headless',
                claimed_at  TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                PRIMARY KEY (agent_id, human_id)
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_heartbeat ON sessions (heartbeat_at)")
        conn.commit()
        self._conn = conn

    def _one(self, sql: str, params: tuple) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# NATS-KV stub (Mode 2+ migration seam)
# ---------------------------------------------------------------------------


class NatsKvSessionRegistry:
    """Stub for the Mode-2+ NATS-KV session registry.

    All methods raise ``NotImplementedError`` — this class exists to confirm
    the ``SessionRegistry`` protocol shape survives the Mode-2+ swap without
    any caller changes.  Replace with a real implementation when NATS lands.
    """

    def is_sessioned(self, agent_id: str, human_id: str) -> bool:
        raise NotImplementedError("NatsKvSessionRegistry not yet implemented")

    def claim_session(
        self, agent_id: str, human_id: str, session_id: str, *, mode: str = "headless"
    ) -> bool:
        raise NotImplementedError

    def release_session(self, agent_id: str, human_id: str, session_id: str) -> bool:
        raise NotImplementedError

    def heartbeat(self, agent_id: str, human_id: str, session_id: str) -> bool:
        raise NotImplementedError

    def cleanup_stale(self, *, stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD) -> int:
        raise NotImplementedError
