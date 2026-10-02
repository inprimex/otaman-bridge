"""LLM routing dispatch + sensitivity guard (llm-router-backend 1.3).

The bridge half of the Otaman LLM router. The three-part split (confirmed with
core-agent 20261002T131112):

- **core** (:mod:`otaman_core.llm_router`) resolves WHERE a routed call goes
  (:func:`effective_route` → backend ``resolve`` → :class:`BackendTarget`) and
  supplies the guard PREDICATE (:func:`guard_route` / :func:`route_leaves_tenant`).
- **adapters** (:mod:`otaman_adapters.litellm`) is the WIRE CALL behind the proxy.
- **bridge (here, 1.3)** is the dispatch path: resolve the route, ENFORCE the
  sensitivity guard (WHETHER the call may go there), and dispatch through the
  resolved backend.

The guard lives HERE, at the dispatch, never in prompt convention: content marked
with a ``local_only`` sensitivity class may not take a route whose target leaves the
tenant, and the refusal names the class and the target. A cloud route for guarded
content is a conformance defect, so the guard is load-bearing — remove the
:func:`dispatch` guard check and guarded content reaches the cloud (the clause-3
test asserts exactly this).

**Opt-in / byte-identical.** With no ``router:`` block, ``select_backend`` returns
the :class:`DefaultBackend` and the resolved target has ``base_url is None`` — the
native path, byte-identical to pre-router behavior; :func:`call` returns ``None`` so
the caller proceeds natively and no proxy is contacted.

**Standalone primitive.** There is deliberately no router dispatch path elsewhere in
the fleet yet; the eventual invoker (JTBD-58 / spec-agent's call) wires this in. This
module ships the mechanism — the same "primitive now, caller later" shape as the
team-mode acting-human writer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from otaman_core.llm_router import (
    BackendTarget,
    effective_route,
    guard_route,
    parse_router_config,
    select_backend,
)

_log = logging.getLogger("maestro.bridge.llm_dispatch")  # legacy: logger renamed at otaman-core 1.0


class GuardRefusal(RuntimeError):
    """A routed call the sensitivity guard refused.

    ``refusal`` is core's message (it names the offending class and the target).
    Raised at the dispatch path so a tenant-leaving route for local-only content
    can never reach the wire call.
    """

    def __init__(self, refusal: str) -> None:
        super().__init__(refusal)
        self.refusal = refusal


@dataclass(frozen=True)
class DispatchDecision:
    """The routing decision for a call: where it goes, and whether that's the native path."""

    target: BackendTarget

    @property
    def native(self) -> bool:
        """True when the call takes the byte-identical native path (no proxy)."""
        return self.target.base_url is None


def dispatch(
    config: Mapping[str, Any],
    agent: str,
    content_classes: frozenset[str] | set[str] | None = None,
) -> DispatchDecision:
    """Resolve *agent*'s route and enforce the sensitivity guard; return the decision.

    Composes core's single resolution point and guard predicate:
    ``effective_route`` → ``select_backend().resolve`` → ``guard_route``. Raises
    :class:`GuardRefusal` (naming the class + target) when ``content_classes`` carries
    a ``local_only`` class and the resolved target leaves the tenant. Otherwise returns
    the admissible :class:`DispatchDecision`. With no ``router:`` config the decision is
    the native path (``target.base_url is None``) and the guard never triggers.

    This is the dispatch path's WHETHER + WHERE; :func:`call` adds the wire call.
    """
    classes = frozenset(content_classes or ())
    route = effective_route(config, agent)
    target = select_backend(config).resolve(route)
    refusal = guard_route(parse_router_config(config), target, classes)
    if refusal is not None:
        _log.warning("llm dispatch refused for %s: %s", agent, refusal)
        raise GuardRefusal(refusal)
    return DispatchDecision(target=target)


def call(
    config: Mapping[str, Any],
    agent: str,
    messages: Sequence[Mapping[str, Any]],
    content_classes: frozenset[str] | set[str] | None = None,
    **complete_kwargs: Any,
) -> Any | None:
    """Dispatch a chat call: guard, then route it through the resolved backend.

    Enforces the guard via :func:`dispatch` (a refusal raises :class:`GuardRefusal`
    before any wire call). For a proxy target, makes the wire call through adapters'
    :class:`~otaman_adapters.litellm.LiteLLMAdapter` and returns its
    ``ChatCompletion``. For the native path (``base_url is None``) returns ``None`` —
    the caller proceeds on the unchanged native path, byte-identical to pre-router
    behavior. The ``otaman_adapters`` import is lazy/optional (mirrors the pm-sync
    easy8 seam); an absent extra surfaces as a clear ImportError only when a proxy
    route is actually taken.
    """
    decision = dispatch(config, agent, content_classes)
    if decision.native:
        return None
    from otaman_adapters.litellm import LiteLLMAdapter  # noqa: PLC0415 — optional dep, lazy

    return LiteLLMAdapter(decision.target).complete(messages, **complete_kwargs)


__all__ = ["DispatchDecision", "GuardRefusal", "call", "dispatch"]
