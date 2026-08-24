"""Tests for the Telegram HUMAN-DECISION confirmation mechanism (hitl 2.1)."""

from __future__ import annotations

import json
from pathlib import Path

import otaman_bridge.hitl_telegram as hitl
from otaman_bridge.hitl_telegram import (
    TelegramConfirmResult,
    confirm_via_telegram,
    is_enrolled,
    telegram_enrolled_emails,
)


def _write_hitl(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "hitl.yaml"
    p.write_text(content, encoding="utf-8")
    return p


_TELEGRAM_ENROLL = """\
enrollment:
  roman@inprimex.com:
    messenger:
      adapter: telegram
      address_ref: TELEGRAM_ROMAN
"""


# ---------------------------------------------------------------------------
# Enrollment / is_enrolled
# ---------------------------------------------------------------------------


class TestEnrollment:
    def test_missing_file_not_enrolled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        assert telegram_enrolled_emails(tmp_path / "nope.yaml") == []
        assert is_enrolled(path=tmp_path / "nope.yaml") is False

    def test_telegram_binding_enrolled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(tmp_path, _TELEGRAM_ENROLL)
        assert telegram_enrolled_emails(p) == ["roman@inprimex.com"]
        assert is_enrolled(path=p) is True
        assert is_enrolled("roman@inprimex.com", path=p) is True
        assert is_enrolled("someone@else.com", path=p) is False

    def test_extra_absent_is_no_op(self, tmp_path, monkeypatch):
        """Enrolled but the telegram transport isn't installed -> not configured."""
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: False)
        p = _write_hitl(tmp_path, _TELEGRAM_ENROLL)
        assert is_enrolled(path=p) is False
        assert is_enrolled("roman@inprimex.com", path=p) is False

    def test_totp_only_does_not_collide(self, tmp_path, monkeypatch):
        """A TOTP-only human is NOT a telegram enrollment (distinct fields)."""
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(
            tmp_path,
            "enrollment:\n  roman@inprimex.com:\n    totp_secret_ref: TOTP_ROMAN\n",
        )
        assert telegram_enrolled_emails(p) == []
        assert is_enrolled(path=p) is False

    def test_human_can_hold_both(self, tmp_path, monkeypatch):
        """One human with BOTH totp_secret_ref and messenger.telegram — both read."""
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(
            tmp_path,
            "enrollment:\n"
            "  roman@inprimex.com:\n"
            "    totp_secret_ref: TOTP_ROMAN\n"
            "    messenger:\n"
            "      adapter: telegram\n"
            "      address_ref: TELEGRAM_ROMAN\n",
        )
        assert telegram_enrolled_emails(p) == ["roman@inprimex.com"]

    def test_messenger_without_address_ref_not_enrolled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(
            tmp_path,
            "enrollment:\n  x@y.com:\n    messenger:\n      adapter: telegram\n",
        )
        assert telegram_enrolled_emails(p) == []

    def test_non_telegram_messenger_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(
            tmp_path,
            "enrollment:\n  x@y.com:\n    messenger:\n"
            "      adapter: signal\n      address_ref: SIG_X\n",
        )
        assert telegram_enrolled_emails(p) == []

    def test_unparseable_file_not_enrolled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(tmp_path, ":: not: [yaml")
        assert telegram_enrolled_emails(p) == []
        assert is_enrolled(path=p) is False

    def test_multiple_enrolled_sorted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hitl, "_telegram_extra_available", lambda: True)
        p = _write_hitl(
            tmp_path,
            "enrollment:\n"
            "  zoe@x.com:\n    messenger:\n      adapter: telegram\n      address_ref: T_ZOE\n"
            "  ana@x.com:\n    messenger:\n      adapter: telegram\n      address_ref: T_ANA\n",
        )
        assert telegram_enrolled_emails(p) == ["ana@x.com", "zoe@x.com"]


# ---------------------------------------------------------------------------
# confirm_via_telegram — drives the /approval surface, fail-closed
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, decision: str, responder: str = ""):
        self._body = json.dumps({"decision": decision, "responder": responder}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _arm(monkeypatch, *, enrolled=True, endpoint=("acct", 8091, "TKN"), decision=None):
    """Wire confirm_via_telegram's seams: enrollment, endpoint, and the POST."""
    monkeypatch.setattr(hitl, "is_enrolled", lambda *a, **k: enrolled)
    monkeypatch.setattr(hitl, "_sole_enrolled_email", lambda *a, **k: "roman@inprimex.com")
    monkeypatch.setattr(hitl, "_resolve_account_and_endpoint", lambda: endpoint)
    if decision is not None:
        monkeypatch.setattr(
            hitl.urllib.request,
            "urlopen",
            lambda req, timeout=None: _FakeResp(decision),
        )


class TestConfirm:
    def test_not_enrolled_fails_closed(self, monkeypatch):
        _arm(monkeypatch, enrolled=False)
        assert confirm_via_telegram("delete prod") == TelegramConfirmResult(False, None)

    def test_daemon_unreachable_fails_closed(self, monkeypatch):
        _arm(monkeypatch, enrolled=True, endpoint=None)
        assert confirm_via_telegram("delete prod") == TelegramConfirmResult(False, None)

    def test_allow_approves_with_human_id(self, monkeypatch):
        _arm(monkeypatch, decision="allow")
        res = confirm_via_telegram("delete prod")
        assert res.approved is True
        assert res.human_id == "roman@inprimex.com"

    def test_deny_declines_named(self, monkeypatch):
        _arm(monkeypatch, decision="deny")
        res = confirm_via_telegram("delete prod")
        assert res.approved is False
        assert res.human_id == "roman@inprimex.com"  # who declined, for the audit trail

    def test_timeout_not_confirmed(self, monkeypatch):
        _arm(monkeypatch, decision="timeout")
        assert confirm_via_telegram("delete prod") == TelegramConfirmResult(False, None)

    def test_unknown_decision_not_confirmed(self, monkeypatch):
        _arm(monkeypatch, decision="ask")
        assert confirm_via_telegram("delete prod") == TelegramConfirmResult(False, None)

    def test_post_error_fails_closed(self, monkeypatch):
        _arm(monkeypatch, decision="allow")

        def _boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(hitl.urllib.request, "urlopen", _boom)
        assert confirm_via_telegram("delete prod") == TelegramConfirmResult(False, None)

    def test_explicit_email_used_as_human_id(self, monkeypatch):
        _arm(monkeypatch, decision="allow")
        res = confirm_via_telegram("delete prod", email="ana@x.com")
        assert res.human_id == "ana@x.com"
