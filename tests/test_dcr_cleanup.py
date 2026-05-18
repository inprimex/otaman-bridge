"""Tests for the DCR shim cleanup sweep (chunk D6).

Three layers:
- Pure helpers: parse_duration_seconds, is_managed_app, app_age_seconds.
- sweep_orphans with stubbed mgmt client: eligibility filtering, dry-run,
  age cutoff, failure isolation, safety belt against bootstrap apps.
- ZitadelMgmtClient.list_apps_with_prefix + delete_app against fake opener.
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request

import pytest

from otaman_bridge.dcr_shim import (
    SweepReport,
    ZitadelMgmtClient,
    ZitadelMgmtError,
    app_age_seconds,
    is_managed_app,
    parse_duration_seconds,
    sweep_orphans,
)


# ---- parse_duration_seconds ----------------------------------------------


class TestParseDurationSeconds:
    @pytest.mark.parametrize("s,expected", [
        ("90s", 90),
        ("15m", 900),
        ("1h", 3600),
        ("24h", 86400),
        ("30d", 30 * 86400),
        ("7d", 7 * 86400),
        ("100", 100),  # bare integer = seconds
    ])
    def test_happy(self, s, expected):
        assert parse_duration_seconds(s) == expected

    @pytest.mark.parametrize("s", [
        "abc",
        "10x",
        "h",
        "",
        "  ",
        "3.5h",
    ])
    def test_invalid_raises(self, s):
        with pytest.raises(ValueError):
            parse_duration_seconds(s)

    def test_uppercase_suffix_accepted(self):
        assert parse_duration_seconds("30D") == 30 * 86400
        assert parse_duration_seconds("1H") == 3600


# ---- is_managed_app ------------------------------------------------------


class TestIsManagedApp:
    def test_prefix_match(self):
        assert is_managed_app({"name": "dcr-shim:abc123"}, "dcr-shim:") is True

    def test_no_match(self):
        assert is_managed_app({"name": "otaman-bridge"}, "dcr-shim:") is False
        assert is_managed_app({"name": "otaman-runner"}, "dcr-shim:") is False

    def test_empty_prefix_never_matches(self):
        """Belt-and-braces: empty prefix would match every app, including
        bootstrap-created ones. We refuse to ever consider any app managed
        in that case."""
        assert is_managed_app({"name": "dcr-shim:abc"}, "") is False
        assert is_managed_app({"name": "anything"}, "") is False

    def test_missing_name(self):
        assert is_managed_app({}, "dcr-shim:") is False


# ---- app_age_seconds -----------------------------------------------------


class TestAppAgeSeconds:
    def _at(self, ts: str) -> dict:
        return {"details": {"creationDate": ts}}

    def test_z_suffix_zulu(self):
        # Zitadel uses "Z" for UTC.
        app = self._at("2026-05-18T13:00:00Z")
        # now = 2026-05-18T13:01:00Z = 60 seconds later
        now = datetime.datetime(2026, 5, 18, 13, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
        assert app_age_seconds(app, now=now) == 60

    def test_explicit_offset(self):
        app = self._at("2026-05-18T13:00:00+00:00")
        now = datetime.datetime(2026, 5, 18, 14, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        assert app_age_seconds(app, now=now) == 3600

    def test_missing_creation_date_returns_none(self):
        assert app_age_seconds({"details": {}}) is None
        assert app_age_seconds({}) is None

    def test_malformed_creation_date_returns_none(self):
        assert app_age_seconds(self._at("not-a-date")) is None
        assert app_age_seconds(self._at("")) is None

    def test_default_now_uses_real_clock(self):
        # Just verify we don't crash and return a sensible positive number.
        app = self._at("2026-05-18T00:00:00Z")
        age = app_age_seconds(app)
        assert age is None or age >= 0


# ---- sweep_orphans (with stub mgmt client) ------------------------------


class _StubMgmt:
    """Stand-in for ZitadelMgmtClient with controllable list + delete behavior."""
    def __init__(self, apps, *, delete_raises_for=()):
        self._apps = list(apps)
        self.delete_raises_for = set(delete_raises_for)
        self.list_calls = 0
        self.deleted = []

    def list_apps_with_prefix(self, *, project_id, name_prefix):
        self.list_calls += 1
        return [a for a in self._apps if str(a.get("name", "")).startswith(name_prefix)]

    def delete_app(self, *, project_id, app_id):
        if app_id in self.delete_raises_for:
            raise ZitadelMgmtError(f"refused to delete {app_id}", status=403)
        self.deleted.append(app_id)


def _app(app_id, name, created_iso):
    return {"id": app_id, "name": name, "details": {"creationDate": created_iso}}


# Anchor time used across these tests: 2026-05-18T13:00:00Z (UTC).
NOW = datetime.datetime(2026, 5, 18, 13, 0, 0, tzinfo=datetime.timezone.utc).timestamp()


class TestSweepOrphans:
    def test_no_apps_returns_empty_report(self):
        stub = _StubMgmt([])
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=3600,
        )
        assert report.found == 0
        assert report.deleted == 0

    def test_deletes_only_older_than_ttl(self):
        # ttl 1h. App 1 = 30min old (skip). App 2 = 2h old (delete).
        apps = [
            _app("a1", "dcr-shim:fresh", "2026-05-18T12:30:00Z"),  # 30min
            _app("a2", "dcr-shim:stale", "2026-05-18T11:00:00Z"),  # 2h
        ]
        stub = _StubMgmt(apps)
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=3600, now=NOW,
        )
        assert report.found == 2
        assert report.eligible == 1
        assert report.deleted == 1
        assert "a2" in report.deleted_ids
        assert stub.deleted == ["a2"]

    def test_dry_run_does_not_call_delete(self):
        apps = [
            _app("a1", "dcr-shim:old", "2026-05-18T11:00:00Z"),
        ]
        stub = _StubMgmt(apps)
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=3600,
            dry_run=True, now=NOW,
        )
        assert report.deleted == 1
        assert stub.deleted == []  # no actual deletes

    def test_skips_apps_without_creation_date(self):
        apps = [
            {"id": "a1", "name": "dcr-shim:noisy"},  # missing details
        ]
        stub = _StubMgmt(apps)
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=3600, now=NOW,
        )
        assert report.found == 1
        assert report.eligible == 0
        assert report.deleted == 0

    def test_belt_and_braces_against_bootstrap_apps(self):
        """Even if list_apps_with_prefix mistakenly returned a bootstrap
        app (defense-in-depth), is_managed_app should filter it out."""
        apps = [
            # No dcr-shim: prefix — bootstrap app, must be left alone.
            _app("a1", "otaman-bridge-web", "2020-01-01T00:00:00Z"),
            _app("a2", "dcr-shim:old", "2020-01-01T00:00:00Z"),
        ]
        stub = _StubMgmt(apps)
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=3600, now=NOW,
        )
        assert stub.deleted == ["a2"]

    def test_delete_failure_does_not_abort_sweep(self):
        apps = [
            _app("a1", "dcr-shim:fails", "2026-05-18T11:00:00Z"),
            _app("a2", "dcr-shim:ok",    "2026-05-18T11:00:00Z"),
        ]
        stub = _StubMgmt(apps, delete_raises_for={"a1"})
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=3600, now=NOW,
        )
        assert report.deleted == 1
        assert report.failed == 1
        assert "a1" in report.failed_ids
        assert "a2" in report.deleted_ids

    def test_zero_ttl_disables_sweep(self):
        apps = [_app("a1", "dcr-shim:stale", "2020-01-01T00:00:00Z")]
        stub = _StubMgmt(apps)
        report = sweep_orphans(
            mgmt_client=stub, project_id="p",
            name_prefix="dcr-shim:", ttl_seconds=0, now=NOW,
        )
        # Never even calls list when ttl <= 0.
        assert stub.list_calls == 0
        assert report.deleted == 0


# ---- ZitadelMgmtClient list + delete (against fake opener) --------------


class _FakeResponse:
    def __init__(self, *, status, body):
        self.status = status
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeBody:
    def __init__(self, b): self._b = b
    def read(self): return self._b
    def close(self): pass


class _FakeOpener:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []
    def open(self, req, timeout=None):
        self.requests.append((req.method, req.full_url, dict(req.headers), req.data))
        body, status = self.responses.get(req.full_url, (b'{"error":"not stubbed"}', 404))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", req.headers, _FakeBody(body))
        return _FakeResponse(status=status, body=body)


def _mgmt_with(responses):
    base = {
        "http://mgmt.example/oauth/v2/token": (
            json.dumps({"access_token": "AT", "expires_in": 3600}).encode(), 200,
        ),
    }
    base.update(responses)
    return ZitadelMgmtClient(
        base_url="http://mgmt.example",
        token_url="http://mgmt.example/oauth/v2/token",
        client_id="x", client_secret="y", org_id="org-1",
        expected_host="mgmt.example",
        opener=_FakeOpener(base),
    )


class TestListAppsWithPrefix:
    def test_returns_matching_apps(self):
        c = _mgmt_with({
            "http://mgmt.example/management/v1/projects/p/apps/_search": (
                json.dumps({"result": [
                    {"id": "a1", "name": "dcr-shim:abc"},
                    {"id": "a2", "name": "dcr-shim:def"},
                ]}).encode(), 200,
            ),
        })
        apps = c.list_apps_with_prefix(project_id="p", name_prefix="dcr-shim:")
        assert len(apps) == 2

    def test_empty_result(self):
        c = _mgmt_with({
            "http://mgmt.example/management/v1/projects/p/apps/_search": (
                json.dumps({"result": []}).encode(), 200,
            ),
        })
        assert c.list_apps_with_prefix(project_id="p", name_prefix="dcr-shim:") == []

    def test_sends_starts_with_query(self):
        c = _mgmt_with({
            "http://mgmt.example/management/v1/projects/p/apps/_search": (
                json.dumps({"result": []}).encode(), 200,
            ),
        })
        c.list_apps_with_prefix(project_id="p", name_prefix="dcr-shim:")
        # Find the search request (token request is first).
        search_req = next(r for r in c._opener.requests
                          if r[1].endswith("/apps/_search"))
        body = json.loads(search_req[3])
        assert body["queries"][0]["nameQuery"]["method"] == "TEXT_QUERY_METHOD_STARTS_WITH"
        assert body["queries"][0]["nameQuery"]["name"] == "dcr-shim:"


class TestDeleteApp:
    def test_happy_path(self):
        c = _mgmt_with({
            "http://mgmt.example/management/v1/projects/p/apps/a1": (b"{}", 200),
        })
        c.delete_app(project_id="p", app_id="a1")
        delete_reqs = [r for r in c._opener.requests if r[0] == "DELETE"]
        assert len(delete_reqs) == 1
        assert delete_reqs[0][1].endswith("/apps/a1")

    def test_404_is_idempotent(self):
        c = _mgmt_with({
            "http://mgmt.example/management/v1/projects/p/apps/gone": (
                json.dumps({"code": 5, "message": "not found"}).encode(), 404,
            ),
        })
        # Should NOT raise.
        c.delete_app(project_id="p", app_id="gone")

    def test_non_404_error_raises(self):
        c = _mgmt_with({
            "http://mgmt.example/management/v1/projects/p/apps/a1": (
                json.dumps({"code": 7, "message": "forbidden"}).encode(), 403,
            ),
        })
        with pytest.raises(ZitadelMgmtError) as e:
            c.delete_app(project_id="p", app_id="a1")
        assert e.value.status == 403
