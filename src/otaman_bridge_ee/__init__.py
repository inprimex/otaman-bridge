"""Enterprise Edition extensions for otaman-bridge.

This package houses code that will be proprietary in the eventual
CE/EE split (see otaman-meta/strategy/bridge-ce-ee-split.md). Today
both packages ship from the same private repo + same install; the
seam exists so we can later extract this package into its own
repository with the prep harness at
``strategy/ce-ee-prep/sync-ce.sh``.

Public API (imported by the CE daemon via conditional import):

- ``auth_oidc.OIDCAuthProvider`` — JWT + session-cookie identity.
- (future) ``dcr_shim`` — RFC 7591 DCR shim in front of Zitadel.
- (future) ``routes_dcr`` — /.well-known/* + /oauth/register routes.

The CE daemon ``otaman_bridge.daemon`` tries to import this package
on startup. If it's not installed (eventual public-CE-only build),
the daemon falls back to CE-only composition: no OIDC, no DCR, no
session-cookie path — just loopback + simple identity.
"""

__all__: list[str] = []
