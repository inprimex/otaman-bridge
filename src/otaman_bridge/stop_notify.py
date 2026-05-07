"""Stop-hook helper — surface Claude's end-of-turn questions to Telegram.

Invoked by ``hooks/stop-notify.sh`` only when the AFK flag file is
present (cheap fast-path stays in bash). This script reads the session
transcript, takes the last assistant message, heuristically decides
whether it ends with a question, and posts a ``/notify`` to the bridge
daemon for remote surfacing.

**Debounce** (prevents spam when Claude produces several short turns
in a row, or when the same question is re-emitted):

    - Min 60s between notifications for a given session_id.
    - Content-hash dedup: never re-notify the exact same tail twice
      for the same session.

State lives at ``<maestro-root>/.maestro/stop-notify.state`` as a small
JSON dict keyed by session_id. Entries older than 24h are pruned on
write so the file can't grow unbounded.

**Fail-safe contract**: any failure (no maestro root, no AFK, no
daemon, transcript unreadable, anything) exits 0. This is a
convenience signal — never a blocker.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from otaman_core._resolve import find_maestro_root, read_expected_account  # noqa: E402
from otaman_bridge.afk import read_afk  # noqa: E402


# Tunables — exposed at module level so tests can shrink them.
MIN_DEBOUNCE_SECONDS = 60
PRUNE_OLDER_THAN_SECONDS = 24 * 60 * 60
TAIL_CHARS = 1500       # body length included in the Telegram notification
DETECTION_CHARS = 300   # look at last N chars for the `?` heuristic


def _log_warn(msg: str) -> None:
    print(f"maestro stop-notify: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Transcript parsing


def load_last_assistant_text(transcript_path: Path) -> str | None:
    """Return the text of the last assistant message in the JSONL transcript.

    Claude Code writes transcripts as one JSON object per line with a
    ``type`` field. Assistant messages have
    ``{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}, ...]}}``.
    Other content types (tool_use, thinking) are ignored — we want the
    actual user-facing prose to heuristic-check for a question.
    """
    if not transcript_path.is_file():
        return None
    last_text: str | None = None
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = (entry.get("message") or {}).get("content") or []
                if not isinstance(content, list):
                    continue
                text_parts: list[str] = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        val = c.get("text")
                        if isinstance(val, str) and val:
                            text_parts.append(val)
                if text_parts:
                    last_text = "\n".join(text_parts)
    except OSError:
        return None
    return last_text


def looks_like_question(text: str) -> bool:
    """Cheap heuristic: is the tail of this message a question?

    A `?` in the last DETECTION_CHARS is a strong signal (covers
    "should I...", "which one...", "want me to...", etc.). No deeper
    NLP — the cost of a false positive is "your phone buzzes once for
    a rhetorical question". Cheap; correctable with the debounce.
    """
    if not text:
        return False
    tail = text[-DETECTION_CHARS:]
    return "?" in tail


# ---------------------------------------------------------------------------
# Debounce state


def _load_state(state_path: Path) -> dict:
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _prune_state(state: dict, now: int) -> dict:
    return {
        sid: v for sid, v in state.items()
        if isinstance(v, dict)
        and (now - int(v.get("ts", 0))) < PRUNE_OLDER_THAN_SECONDS
    }


def debounce_ok(state_path: Path, session_id: str, content_hash: str) -> bool:
    """Return True if we should notify, False if we should suppress.

    Updates the state file as a side effect when True is returned.
    Suppression reasons:
      - Last notification for this session was <MIN_DEBOUNCE_SECONDS ago
      - Last notification had the exact same content hash
    """
    now = int(time.time())
    state = _prune_state(_load_state(state_path), now)
    previous = state.get(session_id)
    if isinstance(previous, dict):
        if (now - int(previous.get("ts", 0))) < MIN_DEBOUNCE_SECONDS:
            return False
        if previous.get("hash") == content_hash:
            return False
    state[session_id] = {"ts": now, "hash": content_hash}
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        # Best-effort — failing to persist means we'll potentially
        # re-notify next time; never a crash.
        pass
    return True


# ---------------------------------------------------------------------------
# Account / endpoint resolution (shared shape with bridge_approval.py)


def _derive_account(project_root: Path) -> str | None:
    env_account = os.environ.get("MAESTRO_ACTIVE_ACCOUNT")
    if env_account:
        return env_account
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        base = os.path.basename(config_dir.rstrip("/\\"))
        if base.startswith(".claude-"):
            return base[len(".claude-"):]
        if base in ("", ".claude"):
            return "default"
    marker_account = read_expected_account(project_root)
    if marker_account:
        return marker_account
    return None


def _endpoint_file(account: str) -> Path:
    return Path.home() / ".maestro" / f"bridge-{account}.endpoint"


def _read_endpoint(account: str) -> tuple[int, str] | None:
    path = _endpoint_file(account)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        port = int(data.get("port") or 0)
        token = str(data.get("token") or "")
        if port > 0 and token:
            return port, token
    except (OSError, ValueError):
        return None
    return None


def _session_project(project_root: Path) -> str:
    platform_yaml = project_root / "platform.yaml"
    if not platform_yaml.is_file():
        return project_root.name
    try:
        text = platform_yaml.read_text(encoding="utf-8")
    except OSError:
        return project_root.name
    for line in text.splitlines():
        if line.startswith("project:"):
            _, _, value = line.partition(":")
            value = value.strip().strip("'").strip('"')
            if value:
                return value
    return project_root.name


def _current_agent(project_root: Path) -> str:
    agent_file = project_root / ".agents" / "current-agent"
    if not agent_file.is_file():
        return ""
    try:
        return agent_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def post_notify(
    *,
    port: int,
    token: str,
    account: str,
    project: str,
    agent: str,
    session_id: str,
    body: str,
    timeout: float = 5.0,
) -> None:
    """POST a /notify to the daemon. Raises on failure."""
    payload = {
        "account": account,
        "project": project,
        "severity": "approval",  # yellow = needs attention
        "title": "Claude is waiting for input" + (f" · {agent}" if agent else ""),
        "body": body,
        "source_agent": agent,
        "bus_message_id": session_id,
    }
    url = f"http://127.0.0.1:{port}/notify"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    urllib.request.urlopen(req, timeout=timeout).read()


def main() -> int:
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        return 0

    project_root = find_maestro_root()
    if project_root is None:
        return 0

    # AFK off → we don't surface questions. User is at the keyboard.
    if read_afk(project_root) is None:
        return 0

    transcript_path = hook_input.get("transcript_path")
    if not transcript_path:
        return 0

    text = load_last_assistant_text(Path(transcript_path))
    if not text or not looks_like_question(text):
        return 0

    session_id = str(hook_input.get("session_id") or "unknown")
    tail = text[-TAIL_CHARS:]
    content_hash = hashlib.sha256(tail.encode("utf-8")).hexdigest()[:16]

    state_path = project_root / ".maestro" / "stop-notify.state"
    if not debounce_ok(state_path, session_id, content_hash):
        return 0

    account = _derive_account(project_root)
    if account is None:
        _log_warn("cannot determine account; skipping notification")
        return 0
    endpoint = _read_endpoint(account)
    if endpoint is None:
        _log_warn(f"daemon endpoint missing for account {account!r}; skipping")
        return 0
    port, token = endpoint

    body = f"…{tail}" if len(text) > TAIL_CHARS else tail

    try:
        post_notify(
            port=port, token=token,
            account=account,
            project=_session_project(project_root),
            agent=_current_agent(project_root),
            session_id=session_id,
            body=body,
        )
    except (urllib.error.URLError, OSError) as e:
        _log_warn(f"POST /notify failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
