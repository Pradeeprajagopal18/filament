"""Sign-in settings an admin can change in the web app.

The IGNITE_* environment gives the defaults; whatever an admin saves under Settings → Sign-in is
kept in the database and wins. Every request refreshes the copy in memory (one small query), so
all API processes pick up a change straight away.

Secrets (the LDAP service password, the SSO client secret) are never stored: the form takes a
reference such as ${env:LDAP_BIND_PASSWORD}, resolved from the server's environment when used.

Safeguards, because a wrong change could lock everyone out:
* at least one sign-in method stays on;
* local accounts can only be turned off once an admin has signed in through LDAP or SSO;
* LDAP and SSO can only be turned on with their required settings filled in (and both have a
  test button);
* turning sign-in off entirely (auth_mode=none) is not possible from the web app;
* `ign admin reset-auth`, run on the server, drops the saved settings and turns local accounts
  back on if something still goes wrong.
"""

from __future__ import annotations

import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from igniteai.config import Settings, get_settings
from igniteai.models import AuthSetting, User

SECRET_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")

# What the web app may change, with the type each value must have.
FIELDS: dict[str, type] = {
    "auth_methods": list,
    "workspace_default_role": str,
    "admin_emails": list,
    "password_min_length": int,
    "max_failed_logins": int,
    "lockout_minutes": int,
    "session_hours": int,
    "ldap_url": str, "ldap_start_tls": bool, "ldap_ca_file": str, "ldap_bind_dn": str,
    "ldap_bind_password": str, "ldap_base_dn": str, "ldap_user_filter": str,
    "ldap_email_attr": str, "ldap_name_attr": str, "ldap_group_attr": str,
    "ldap_github_attr": str, "ldap_timeout_s": int,
    "oidc_issuer": str, "oidc_client_id": str, "oidc_client_secret": str, "oidc_scopes": str,
    "oidc_groups_claim": str, "oidc_github_claim": str,
}
SECRETS = {"ldap_bind_password", "oidc_client_secret"}

_override: dict = {}


def refresh(session: Session) -> None:
    """Load the saved settings (called at the start of each request)."""
    global _override
    try:
        row = session.get(AuthSetting, "auth")
    except Exception:  # noqa: BLE001 - table not there yet (very first start)
        return
    _override = dict(row.data) if row else {}


def cfg() -> Settings:
    """The settings in effect: environment defaults with the saved changes on top, and secret
    references resolved."""
    base = get_settings()
    upd = {k: v for k, v in _override.items() if k in FIELDS}
    for key in SECRETS:
        val = upd.get(key, getattr(base, key))
        if isinstance(val, str) and (m := SECRET_REF.match(val)):
            upd[key] = os.environ.get(m.group(1))
    return base.model_copy(update=upd) if upd else base


def view() -> dict:
    """For the settings page: each value and where it comes from. Secrets only as references."""
    base, out = get_settings(), {}
    for key in FIELDS:
        saved = key in _override
        val = _override[key] if saved else getattr(base, key)
        if key in SECRETS:
            ref = val if isinstance(val, str) and SECRET_REF.match(val) else None
            m = SECRET_REF.match(ref) if ref else None
            out[key] = {"value": ref, "source": "saved" if saved else "environment",
                        "set": bool(val), "resolves": bool(m and os.environ.get(m.group(1)))
                        if ref else bool(val)}
        else:
            out[key] = {"value": val, "source": "saved" if saved else "environment"}
    return out


class ConfigError(ValueError):
    pass


def validate(session: Session, changes: dict, actor_source: str | None) -> dict:
    """Check a change and return the full set to save (current saved values + changes)."""
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ConfigError(f"Can't change {sorted(unknown)} here")
    clean: dict = {}
    for key, val in changes.items():
        typ = FIELDS[key]
        if val is None or (val == "" and typ is not list):
            clean[key] = None  # back to the environment default
            continue
        if typ is int:
            try:
                val = int(val)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key} must be a number") from exc
            if val < 1:
                raise ConfigError(f"{key} must be at least 1")
        elif typ is bool:
            val = bool(val)
        elif typ is list:
            if not isinstance(val, list):
                raise ConfigError(f"{key} must be a list")
            val = [str(v).strip() for v in val if str(v).strip()]
        else:
            val = str(val).strip()
        if key in SECRETS and val and not SECRET_REF.match(val):
            raise ConfigError(f"Don't paste the {key.replace('_', ' ')} itself: put it in the "
                              "server's environment and enter a reference such as "
                              f"${{env:IGNITE_{key.upper()}}}")
        clean[key] = val
    merged = {**_override, **clean}
    merged = {k: v for k, v in merged.items() if v is not None}  # None: back to the environment
    eff = get_settings().model_copy(update=merged)
    methods = [m for m in eff.auth_methods]
    if not methods:
        raise ConfigError("Keep at least one sign-in method on")
    if set(methods) - {"local", "ldap", "oidc"}:
        raise ConfigError("Sign-in methods are local, ldap and oidc")
    if eff.workspace_default_role not in {"member", "none"}:
        raise ConfigError("New people join as 'member' or not at all ('none')")
    if eff.password_min_length < 8:
        raise ConfigError("Passwords need at least 8 characters")
    if "ldap" in methods:
        if not (eff.ldap_url and eff.ldap_url.startswith(("ldap://", "ldaps://"))):
            raise ConfigError("LDAP needs a server URL starting with ldaps:// or ldap://")
        if not eff.ldap_base_dn:
            raise ConfigError("LDAP needs the base DN to search for people")
        if "{username}" not in (eff.ldap_user_filter or ""):
            raise ConfigError("The LDAP user filter must contain {username}")
    if "oidc" in methods:
        if not (eff.oidc_issuer and eff.oidc_issuer.startswith("https://")):
            raise ConfigError("SSO needs the issuer URL (https://...)")
        if not eff.oidc_client_id:
            raise ConfigError("SSO needs the client ID")
    if "local" not in methods:
        others = set(methods) - {"local"}
        ok = session.scalar(select(User).where(User.is_admin.is_(True), User.active.is_(True),
                                               User.auth_source.in_(others),
                                               User.last_login_at.is_not(None)))
        if ok is None:
            raise ConfigError("Turning off Ignite accounts would lock admins out: first sign "
                              "in as an admin through " + " or ".join(
                                  {"ldap": "LDAP", "oidc": "SSO"}[m] for m in sorted(others))
                              + " (add your email to the admin emails), then try again")
        if actor_source == "local":
            raise ConfigError("You're signed in with an Ignite account. Sign in through LDAP "
                              "or SSO before turning Ignite accounts off")
    return merged


def save(session: Session, data: dict, by: str) -> None:
    global _override
    row = session.get(AuthSetting, "auth")
    if row is None:
        row = AuthSetting(id="auth", data={})
        session.add(row)
    row.data, row.updated_by = data, by
    _override = dict(data)


def reset(session: Session) -> None:
    global _override
    row = session.get(AuthSetting, "auth")
    if row is not None:
        session.delete(row)
    _override = {}
