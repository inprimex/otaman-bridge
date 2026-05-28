# Spike: SqliteSessionRegistry -- WAL-mode SQLite Backend

**Task**: 1.4 -- Auto-session-spawn-on-bus-events
**Date**: 2026-05-28
**Author**: bridge-agent
**Status**: COMPLETE -- prototype validated, all tests pass

---

## Summary

The `SessionRegistry` is a dedup gate that prevents two Claude Code sessions from being
spawned for the same (agent, human) pair. `SqliteSessionRegistry` implements the protocol
using WAL-mode SQLite with a `threading.RLock` for atomic claim operations. All 18 tests
pass, including concurrent-claim races with 20 threads.

---

## Protocol definition

```python
@runtime_checkable
class SessionRegistry(Protocol):
    def is_sessioned(self, agent: str, human: str) -> bool: ...
    def claim_session(
        self, agent: str, human: str, session_id: str, *, ttl_seconds: float = 3600.0
    ) -> bool: ...
    def release_session(self, agent: str, human: str, session_id: str) -> bool: ...
    def heartbeat(
        self, agent: str, human: str, session_id: str, *, ttl_seconds: float = 3600.0
    ) -> bool: ...
    def cleanup_expired(self) -> int: ...
```

The protocol is `@runtime_checkable` so `isinstance(reg, SessionRegistry)` works for
dependency-injection checks. Using `Protocol` (not ABC) means any object with the right
methods satisfies it -- no inheritance required.

---

## SqliteSessionRegistry design

### Schema

```sql
CREATE TABLE sessions (
    agent       TEXT NOT NULL,
    human       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    claimed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (agent, human)
);
CREATE INDEX idx_expires ON sessions (expires_at);
```

- **PRIMARY KEY (agent, human)**: enforces the one-session-per-pair invariant at the
  database level. A second INSERT for the same pair must either fail or replace.
- **INDEX on expires_at**: makes `cleanup_expired()` a fast range scan.
- **WAL mode**: `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`.
  WAL allows concurrent readers even while a writer holds the lock, which matters
  when multiple threads call `is_sessioned()` while a spawn is in progress.

### Claim algorithm

```python
def claim_session(self, agent, human, session_id, *, ttl_seconds=3600.0) -> bool:
    with self._lock:
        now = time.time()
        # Check for existing non-expired claim
        existing = db.execute(
            "SELECT session_id FROM sessions WHERE agent=? AND human=? AND expires_at>?",
            (agent, human, now),
        ).fetchone()
        if existing and existing[0] != session_id:
            return False   # another session already holds the slot
        # Upsert: insert or replace (covers expired + new)
        db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
            (agent, human, session_id, now, now + ttl_seconds),
        )
        db.commit()
        return True
```

The `threading.RLock` ensures the check-then-write is atomic within a process.
Between processes, WAL mode + SQLite serialisation handles safety.

---

## Prototype results

Location: `src/otaman_bridge/session_registry.py`
Tests: `tests/test_session_registry.py` -- **18/18 passed**

### Test coverage

| Class | Tests | Focus |
|-------|-------|-------|
| TestProtocol | 1 | SqliteSessionRegistry satisfies SessionRegistry protocol |
| TestIsSessioned | 4 | empty DB, active claim, expired claim, different pair |
| TestClaimSession | 5 | new claim, idempotent, conflict, expired-replace, ttl |
| TestReleaseSession | 3 | normal, wrong session_id blocked, missing OK |
| TestHeartbeat | 2 | extend TTL, wrong session_id blocked |
| TestCleanupExpired | 1 | removes expired, keeps active |
| TestRaceConditions | 2 | concurrent claim (20 threads), release+reclaim |

### Race condition results

| Test | Threads | Winners | Expected |
|------|---------|---------|----------|
| concurrent_claim | 20 | exactly 1 | 1 |
| release_and_reclaim | 2 | at most 1 | <=1 |

The `RLock` + check-before-upsert pattern is correct under concurrent access.

---

## Prior-art analysis

Three approaches were considered for the session registry backend:

| Option | Pros | Cons |
|--------|------|------|
| In-memory dict | Zero deps, fast | Lost on restart; no cross-process |
| SQLite WAL | Persistent, fast, stdlib | Single-host only |
| PostgreSQL / NATS-KV | Multi-host | Infrastructure overhead |

**Decision**: SQLite WAL for v1. The design.md insight is that the session registry is
a *cache of authoritative bus state* -- the bus `active/` directory is the source of
truth. If the registry is lost (process crash), the spawn logic re-reads the bus on
restart and reconstructs registry state. This makes durability less critical.

SQLite also aligns with the existing bridge architecture (no external infra required for
CE tier). The `M-7 migration seam` is the `SessionRegistry` Protocol: swapping to
Postgres or NATS-KV later is a drop-in replacement of `SqliteSessionRegistry`.

---

## Key findings

1. **Protocol + dataclass pattern works well**. `@runtime_checkable` Protocol for the
   interface; `@dataclass` for the concrete class. Clean, testable, swappable.

2. **WAL mode is essential for concurrent reads**. Without WAL, `is_sessioned()` calls
   block while a `claim_session()` transaction is in progress. WAL allows both.

3. **`RLock` scope must cover check + upsert atomically**. If the lock only wraps the
   upsert, two threads can both see `existing=None` and both INSERT, with the last-write
   winning -- violating the one-winner guarantee.

4. **`expires_at` is wall-clock `time.time()`, not monotonic**. Monotonic is fine for
   measuring durations but is not comparable across restarts or processes.

5. **Heartbeat is required for long sessions**. The default TTL is 1 hour. A Claude Code
   session that runs longer must call `heartbeat()` periodically to avoid expiry.

6. **`cleanup_expired()` should run periodically**. Without cleanup, the sessions table
   grows without bound. A background timer calling `cleanup_expired()` every 10 minutes
   is sufficient.

---

## Integration notes

The session registry integrates with `FileSystemEventSource` in the spawn pipeline:

```python
async def handle_bus_event(evt: BusFileEvent, registry: SessionRegistry) -> None:
    msg = parse_bus_message(evt.path)
    if msg is None:
        return
    session_id = str(uuid.uuid4())
    claimed = registry.claim_session(msg.to_agent, msg.from_human, session_id)
    if not claimed:
        # Session already active for this pair -- message will be picked up by running session
        return
    await spawn_claude_session(msg.to_agent, session_id)
```

On session exit, the process calls `registry.release_session(agent, human, session_id)`
before terminating, allowing the next message to trigger a new spawn.

---

## Open questions (for spec-agent)

- **Q1**: Should the `claim_session` TTL be configurable per-agent, or global in
  `.otaman/platform.yaml`?
- **Q2**: What is the correct behavior when a session crashes without calling
  `release_session`? The TTL provides eventual recovery, but is 1 hour acceptable,
  or should there be a crash-detection mechanism?
- **Q3**: The `human` field in `(agent, human)` -- is this the human username, the
  terminal tty, or a session token from the OS? The prototype uses a string parameter;
  the spec should nail down the canonical identifier.
- **Q4**: Multi-host deployments -- is SQLite sufficient for v1, or does the spec
  already anticipate distributed session coordination?

---

## Conclusion

SQLite WAL with `RLock`-protected atomic claim is validated and correct. The
`SessionRegistry` Protocol provides the M-7 migration seam. Prototype code at
`src/otaman_bridge/session_registry.py` is ready to promote once the spec is approved.
