"""Account + transport config loader for the bridge.

Reads ``launch-settings.yaml routing.<name>`` (or legacy ``accounts.<name>``) and resolves which
transport to instantiate, plus transport-specific config (with secrets
resolved via ``scripts/_secrets.py``'s tiered source chain).

Config shapes accepted (per design §4.2 + §10.1):

**Implicit null** (T1 baseline — no transport section)::

    accounts:
      personal:
        config_dir: ~/.claude-personal
        label: "Personal"
    # transport defaults to "null"

**Explicit long form** (v2 preferred)::

    accounts:
      personal:
        config_dir: ~/.claude-personal
        transport: telegram
        transport_config:
          group_id: -1001111
          allowed_user_ids: [12345]
          bot_token:
            sources:
              - { type: env,    name: MAESTRO_TG_BOT_PERSONAL }
              - { type: dotenv, name: MAESTRO_TG_BOT_PERSONAL }

**Legacy short form** (backwards compat)::

    accounts:
      personal:
        config_dir: ~/.claude-personal
        telegram:
          group_id: -1001111
          bot_token_env: MAESTRO_TG_BOT_PERSONAL
    # expands to transport: telegram + transport_config: {...}
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    raise

from otaman_core._secrets import SecretRef  # noqa: E402
from otaman_core._secrets import resolve as resolve_secret

# Short-form sugar: account fields that get auto-promoted to transport:
# + transport_config: when `transport:` isn't explicitly set.
_SHORT_FORM_TRANSPORTS = ("telegram", "slack", "discord", "matrix")


@dataclass
class AccountConfig:
    """Resolved account + transport config ready for daemon use."""

    name: str
    config_dir: str = ""
    label: str = ""
    transport: str = "null"
    transport_config: dict[str, Any] = field(default_factory=dict)

    # ``bot_token`` (or similar secrets) are resolved eagerly when ``load()``
    # is called with ``resolve_secrets=True``; the raw SecretRef is kept
    # here only when ``resolve_secrets=False`` (tests).
    unresolved_secrets: dict[str, SecretRef] = field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _extract_transport(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Determine (transport_name, transport_config_dict) from an account block.

    Precedence:
      1. Explicit ``transport:`` + ``transport_config:`` (long form).
      2. Short-form key (``telegram:``, ``slack:``, ...) if no explicit
         ``transport:``. Returns the matching name + its sub-dict.
      3. Default: ``null``, empty config.
    """
    explicit = raw.get("transport")
    if explicit:
        return str(explicit), dict(raw.get("transport_config") or {})

    for name in _SHORT_FORM_TRANSPORTS:
        sub = raw.get(name)
        if isinstance(sub, dict):
            # Normalize the short ``bot_token_env: NAME`` form here so
            # downstream code only sees the long ``bot_token`` shape.
            normalized = dict(sub)
            env_key = normalized.pop("bot_token_env", None)
            if env_key and "bot_token" not in normalized:
                normalized["bot_token"] = env_key
            return name, normalized

    return "null", {}


def _resolve_secrets_in_config(
    transport_config: dict[str, Any],
    *,
    maestro_root: Path,
) -> tuple[dict[str, Any], dict[str, SecretRef]]:
    """Resolve any SecretRef-shaped fields in ``transport_config``.

    Recognized secret-bearing keys: ``bot_token``, ``token``, ``api_key``.
    Each value is coerced through ``SecretRef.from_config`` (so it accepts
    a plain string as env-var-name short form, or the long ``{sources: [...]}``
    form). Resolution uses the same env → dotenv → keyring chain as T1.

    Returns (resolved_config, unresolved_secret_refs_by_key). If a secret
    resolves to None, the key is dropped from resolved_config AND reported
    in unresolved_secret_refs so callers can raise a clear error at
    instantiation time (but not at load time — tests shouldn't need tokens).
    """
    SECRET_KEYS = ("bot_token", "token", "api_key")
    resolved = dict(transport_config)
    unresolved: dict[str, SecretRef] = {}
    for key in SECRET_KEYS:
        if key not in resolved:
            continue
        raw_value = resolved[key]
        try:
            ref = SecretRef.from_config(raw_value)
        except ValueError:
            continue
        value = resolve_secret(ref, maestro_root=maestro_root)
        if value:
            resolved[key] = value
        else:
            del resolved[key]
            unresolved[key] = ref
    return resolved, unresolved


def load_account_config(
    account: str,
    settings_path: Path,
    *,
    maestro_root: Path | None = None,
    resolve_secrets: bool = True,
) -> AccountConfig:
    """Load and resolve an account's config.

    Args:
        account: Account name (must match a key under ``accounts:`` in
            ``launch-settings.yaml``).
        settings_path: Path to ``launch-settings.yaml``.
        maestro_root: Workspace folder path (for ``.otaman/secrets.env``
            resolution). Defaults to ``settings_path.parent``.
        resolve_secrets: When False, leaves SecretRefs unresolved and
            reports them in ``unresolved_secrets``. Useful for tests
            that don't need real tokens.

    Raises:
        KeyError: if ``account`` is not defined in the settings file.
    """
    settings = _load_yaml(settings_path)
    # New name "profiles:" preferred; legacy "accounts:" still honored for
    # one release window. If both present, profiles: wins.
    accounts = settings.get("routing") or settings.get("accounts") or {}
    if not isinstance(accounts, dict) or account not in accounts:
        raise KeyError(f"Routing {account!r} not defined in {settings_path}")

    raw = accounts[account] or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Account {account!r} must be a mapping, got {type(raw).__name__}")

    transport, transport_config = _extract_transport(raw)

    root = maestro_root or settings_path.parent
    unresolved: dict[str, SecretRef] = {}
    if resolve_secrets:
        transport_config, unresolved = _resolve_secrets_in_config(
            transport_config,
            maestro_root=root,
        )
    else:
        # Keep SecretRefs as-is but still report them so callers can inspect.
        for key in ("bot_token", "token", "api_key"):
            if key in transport_config:
                try:
                    unresolved[key] = SecretRef.from_config(transport_config[key])
                except ValueError:
                    pass

    return AccountConfig(
        name=account,
        config_dir=str(raw.get("config_dir", "") or ""),
        label=str(raw.get("label", "") or ""),
        transport=transport,
        transport_config=transport_config,
        unresolved_secrets=unresolved,
    )


def list_accounts_from_settings(settings_path: Path) -> list[str]:
    """Return all routing names defined in ``launch-settings.yaml`` (reads
    both new ``routing:`` and legacy ``accounts:``)."""
    settings = _load_yaml(settings_path)
    accounts = settings.get("routing") or settings.get("accounts") or {}
    if not isinstance(accounts, dict):
        return []
    return sorted(accounts)
