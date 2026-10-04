"""Tests for llm_dispatch.py — llm-router-backend 1.3 routing dispatch + sensitivity guard."""

from __future__ import annotations

import sys
import types

import pytest

from otaman_bridge.llm_dispatch import DispatchDecision, GuardRefusal, call, dispatch

# A litellm-proxy router whose local_only class must never leave the tenant.
_PROXY = {
    "backend": "litellm-proxy",
    "base_url": "http://proxy:4000",
    "local_only_classes": ["cofounder-only"],
}
_CLOUD_AGENT = {"name": "critic", "route": {"family": "openai", "model": "gpt-4o", "local": False}}
_LOCAL_AGENT = {"name": "critic", "route": {"family": "ollama", "model": "llama3", "local": True}}


def _cfg(router=None, agents=None):
    c = {}
    if router is not None:
        c["router"] = router
    if agents is not None:
        c["agents"] = agents
    return c


# ---------------------------------------------------------------------------
# dispatch — routing decision + guard


class TestDispatch:
    def test_no_router_is_native_and_byte_identical(self):
        # No router: block -> DefaultBackend -> native target, even content_classes present.
        d = dispatch(_cfg(agents=[_CLOUD_AGENT]), "critic", {"cofounder-only"})
        assert isinstance(d, DispatchDecision)
        assert d.native is True
        assert d.target.base_url is None

    def test_guarded_content_on_cloud_route_refused_naming_class_and_target(self):
        with pytest.raises(GuardRefusal) as ei:
            dispatch(_cfg(_PROXY, [_CLOUD_AGENT]), "critic", {"cofounder-only"})
        msg = ei.value.refusal
        assert "cofounder-only" in msg  # names the class
        assert "proxy:4000" in msg or "openai" in msg  # names the target

    def test_guarded_content_on_local_route_admitted(self):
        d = dispatch(_cfg(_PROXY, [_LOCAL_AGENT]), "critic", {"cofounder-only"})
        assert d.target.local is True
        assert d.native is False  # proxy base_url set

    def test_unguarded_class_on_cloud_route_admitted(self):
        d = dispatch(_cfg(_PROXY, [_CLOUD_AGENT]), "critic", {"public"})
        assert d.target.base_url == "http://proxy:4000"
        assert d.target.local is False

    def test_no_content_classes_never_refuses(self):
        d = dispatch(_cfg(_PROXY, [_CLOUD_AGENT]), "critic")
        assert d.target.base_url == "http://proxy:4000"

    def test_clause3_guard_is_load_bearing(self):
        # The resolved target (BEFORE the guard) is a tenant-leaving cloud target —
        # so the ONLY thing stopping guarded content reaching the cloud is the guard.
        from otaman_core.llm_router import effective_route, select_backend

        cfg = _cfg(_PROXY, [_CLOUD_AGENT])
        raw = select_backend(cfg).resolve(effective_route(cfg, "critic"))
        assert raw.local is False and raw.base_url == "http://proxy:4000"  # would leave tenant
        # With the guard, dispatch refuses it. Remove the guard and the same route proceeds.
        with pytest.raises(GuardRefusal):
            dispatch(cfg, "critic", {"cofounder-only"})


# ---------------------------------------------------------------------------
# call — dispatch + wire call


class _FakeAdapter:
    last_target = None
    last_messages = None

    def __init__(self, target):
        type(self).last_target = target

    def complete(self, messages, **kw):
        type(self).last_messages = messages
        return {"completion": "ok", "base_url": self.last_target.base_url}


@pytest.fixture
def fake_litellm(monkeypatch):
    """Inject a stub otaman_adapters.litellm so the proxy branch needs no real extra/CI checkout."""
    _FakeAdapter.last_target = None
    _FakeAdapter.last_messages = None
    pkg = types.ModuleType("otaman_adapters")
    mod = types.ModuleType("otaman_adapters.litellm")
    mod.LiteLLMAdapter = _FakeAdapter
    monkeypatch.setitem(sys.modules, "otaman_adapters", pkg)
    monkeypatch.setitem(sys.modules, "otaman_adapters.litellm", mod)
    return _FakeAdapter


class TestCall:
    def test_native_path_returns_none(self):
        # No router -> native -> no wire call, caller proceeds natively.
        assert (
            call(_cfg(agents=[_CLOUD_AGENT]), "critic", [{"role": "user", "content": "hi"}]) is None
        )

    def test_proxy_route_routes_to_litellm(self, fake_litellm):
        msgs = [{"role": "user", "content": "hi"}]
        out = call(_cfg(_PROXY, [_LOCAL_AGENT]), "critic", msgs)
        assert out == {"completion": "ok", "base_url": "http://proxy:4000"}
        assert fake_litellm.last_target.base_url == "http://proxy:4000"
        assert fake_litellm.last_messages is msgs

    def test_guard_refusal_raises_before_any_wire_call(self, fake_litellm):
        with pytest.raises(GuardRefusal):
            call(
                _cfg(_PROXY, [_CLOUD_AGENT]),
                "critic",
                [{"role": "user", "content": "x"}],
                {"cofounder-only"},
            )
        assert fake_litellm.last_target is None  # never constructed the adapter
