"""Tests for otaman_bridge.web_session -- session store + cookie helper.

Pure data-structure tests, no HTTP, no Zitadel.
"""

from __future__ import annotations

import pytest

from otaman_bridge.web_session import (
    DEFAULT_SESSION_TTL,
    SESSION_COOKIE_NAME,
    Session,
    SessionCookie,
    SessionStore,
)


class TestSessionStore:
    def test_create_returns_session_with_unguessable_id(self):
        store = SessionStore()
        s = store.create(user_id="u1", email="a@b", roles=("otaman:viewer",))
        assert isinstance(s, Session)
        assert s.user_id == "u1"
        assert s.email == "a@b"
        assert s.roles == ("otaman:viewer",)
        assert len(s.id) >= 40
        assert len(store) == 1

    def test_get_returns_stored_session(self):
        store = SessionStore()
        s = store.create(user_id="u1", email=None, roles=())
        assert store.get(s.id) is s

    def test_get_unknown_returns_none(self):
        store = SessionStore()
        assert store.get("nope") is None
        assert store.get("") is None
        assert store.get(None) is None

    def test_delete_removes_session(self):
        store = SessionStore()
        s = store.create(user_id="u1", email=None, roles=())
        assert store.delete(s.id) is True
        assert store.get(s.id) is None
        assert store.delete(s.id) is False

    def test_expired_session_auto_removed_on_get(self):
        clock = [1000.0]
        store = SessionStore(ttl=60.0, clock=lambda: clock[0])
        s = store.create(user_id="u1", email=None, roles=())
        assert store.get(s.id) is s
        clock[0] += 70
        assert store.get(s.id) is None
        assert len(store) == 0

    def test_purge_expired_drops_only_expired(self):
        clock = [1000.0]
        store = SessionStore(ttl=60.0, clock=lambda: clock[0])
        old = store.create(user_id="u1", email=None, roles=())
        clock[0] += 70
        fresh = store.create(user_id="u2", email=None, roles=())
        n = store.purge_expired()
        assert n == 1
        assert store.get(old.id) is None
        assert store.get(fresh.id) is fresh

    def test_two_sessions_get_different_ids(self):
        store = SessionStore()
        a = store.create(user_id="u1", email=None, roles=())
        b = store.create(user_id="u2", email=None, roles=())
        assert a.id != b.id
        assert a.csrf_token != b.csrf_token

    def test_default_ttl_is_8_hours(self):
        assert DEFAULT_SESSION_TTL == 8 * 3600.0


class TestSessionCookie:
    def test_parse_extracts_value(self):
        c = SessionCookie()
        sid = c.parse(f"{SESSION_COOKIE_NAME}=abc123")
        assert sid == "abc123"

    def test_parse_handles_multiple_cookies(self):
        c = SessionCookie()
        sid = c.parse(f"foo=bar; {SESSION_COOKIE_NAME}=session-id-here; baz=qux")
        assert sid == "session-id-here"

    def test_parse_returns_none_when_absent(self):
        c = SessionCookie()
        assert c.parse("foo=bar") is None
        assert c.parse("") is None
        assert c.parse(None) is None

    def test_parse_ignores_empty_value(self):
        c = SessionCookie()
        assert c.parse(f"{SESSION_COOKIE_NAME}=") is None

    def test_set_header_includes_required_attributes(self):
        c = SessionCookie(secure=True)
        h = c.set_header("abc", max_age=3600)
        assert "Path=/" in h
        assert "HttpOnly" in h
        assert "SameSite=Lax" in h
        assert "Secure" in h
        assert "Max-Age=3600" in h
        assert h.startswith(f"{SESSION_COOKIE_NAME}=abc;")

    def test_set_header_drops_secure_when_disabled(self):
        c = SessionCookie(secure=False)
        h = c.set_header("abc", max_age=3600)
        assert "Secure" not in h
        assert "HttpOnly" in h
        assert "SameSite=Lax" in h

    def test_clear_header_zeros_max_age(self):
        c = SessionCookie(secure=True)
        h = c.clear_header()
        assert "Max-Age=0" in h
        assert h.startswith(f"{SESSION_COOKIE_NAME}=;")
        assert "HttpOnly" in h

    def test_custom_cookie_name_round_trip(self):
        c = SessionCookie(name="custom_sid", secure=False)
        h = c.set_header("xyz", max_age=60)
        sid = c.parse(h.split(";")[0])
        assert sid == "xyz"


class TestSession:
    def test_csrf_token_auto_populated(self):
        s = Session(id="sid", user_id="u", email="a@b", roles=(), expires_at=999.0)
        assert len(s.csrf_token) >= 40

    def test_two_sessions_have_distinct_csrf(self):
        s1 = Session(id="sid1", user_id="u", email=None, roles=(), expires_at=0.0)
        s2 = Session(id="sid2", user_id="u", email=None, roles=(), expires_at=0.0)
        assert s1.csrf_token != s2.csrf_token

    def test_session_is_immutable(self):
        s = Session(id="sid", user_id="u", email=None, roles=(), expires_at=0.0)
        with pytest.raises((AttributeError, Exception)):
            s.user_id = "other"
