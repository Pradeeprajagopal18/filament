"""Sign-in, sessions and personal API tokens.

Three ways to sign in, any mix of which can be on at once (IGNITE_AUTH_METHODS):

* local: accounts kept in Ignite (scrypt-hashed passwords). A new install has admin / admin,
  which must be changed at first sign-in. Repeated failures lock the account for a while.
* ldap: the company directory (LDAP or Active Directory). Ignite looks the person up with a
  service account, then binds as them to check the password; their groups map to teams.
* oidc: the company SSO, described below.

SSO flow: /api/auth/oidc/login redirects to the identity provider (Authorization Code +
PKCE). The IdP redirects back to /api/auth/callback; we exchange the code, verify the id_token
against the IdP's published keys (issuer, audience, expiry, nonce), create or update the User,
sync SSO groups into team memberships, and set a signed, HttpOnly session cookie.

CLI and MCP clients use personal tokens (Authorization: Bearer ign_...). Only a SHA-256 hash is
stored; the token is shown once.

With IGNITE_AUTH_MODE=none every request is the local admin (laptop use, tests).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from igniteai import access
from igniteai.authconfig import cfg
from igniteai.db import get_session
from igniteai.models import ApiToken, Membership, Team, User

log = logging.getLogger(__name__)

SESSION_COOKIE = "ignite_session"
FLOW_COOKIE = "ignite_oidc"
TOKEN_PREFIX = "ign_"


class AuthError(RuntimeError):
    pass


def enabled() -> bool:
    return cfg().auth_mode != "none"


def methods() -> list[str]:
    s = cfg()
    out = [m for m in s.auth_methods if m in {"local", "ldap", "oidc"}]
    if s.auth_mode == "oidc" and "oidc" not in out:  # older setting: auth_mode=oidc
        out.append("oidc")
    return out or ["local"]


_secret: str | None = None


def secret() -> str:
    """The key that signs session cookies: IGNITE_SESSION_SECRET, else one generated once and
    kept (owner-only) in the data directory so sessions survive restarts."""
    global _secret
    s = cfg()
    if s.session_secret:
        return s.session_secret
    if _secret is None:
        path = Path(s.data_dir) / "session_secret"
        try:
            _secret = path.read_text().strip()
        except OSError:
            _secret = ""
        if len(_secret) < 32:
            _secret = secrets.token_urlsafe(48)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(_secret)
            except OSError:
                log.warning("Could not save the session key; sessions end when Ignite restarts")
    return _secret


def redirect_url() -> str:
    s = cfg()
    return s.oidc_redirect_url or f"{s.ui_url.rstrip('/')}/api/auth/callback"


# -- OIDC -----------------------------------------------------------------------------------------
@lru_cache(maxsize=4)
def _discovery(issuer: str, _bucket: int) -> dict:
    import httpx

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"Could not read the SSO configuration at {url}: {exc}") from exc


def discovery() -> dict:
    s = cfg()
    if not (s.oidc_issuer and s.oidc_client_id):
        raise AuthError("SSO is not configured: set IGNITE_OIDC_ISSUER and "
                        "IGNITE_OIDC_CLIENT_ID")
    return _discovery(s.oidc_issuer, int(time.time() // 3600))


@lru_cache(maxsize=4)
def _jwks(uri: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(uri, cache_keys=True, lifespan=3600)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def start_login(next_path: str = "/") -> tuple[str, str]:
    """(authorize URL, flow cookie value)."""
    s, conf = cfg(), discovery()
    verifier, state, nonce = _b64(secrets.token_bytes(32)), _b64(secrets.token_bytes(16)), \
        _b64(secrets.token_bytes(16))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    from urllib.parse import urlencode

    url = conf["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": s.oidc_client_id, "redirect_uri": redirect_url(),
        "scope": s.oidc_scopes, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    flow = jwt.encode({"state": state, "nonce": nonce, "verifier": verifier,
                       "next": _safe_next(next_path), "exp": int(time.time()) + 600},
                      secret(), algorithm="HS256")
    return url, flow


def _safe_next(path: str | None) -> str:
    # Only same-site paths, so the login can't be used as an open redirect.
    return path if path and path.startswith("/") and not path.startswith("//") else "/"


def read_flow(cookie: str | None, state: str) -> dict:
    try:
        flow = jwt.decode(cookie or "", secret(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("The sign-in took too long or was started elsewhere; try again") from exc
    if not secrets.compare_digest(flow.get("state", ""), state or ""):
        raise AuthError("Sign-in state mismatch; try again")
    return flow


def exchange(code: str, flow: dict) -> dict:
    """Swap the code for tokens and return the verified id_token claims."""
    import httpx

    s, conf = cfg(), discovery()
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_url(),
            "client_id": s.oidc_client_id, "code_verifier": flow["verifier"]}
    if s.oidc_client_secret:
        data["client_secret"] = s.oidc_client_secret
    try:
        r = httpx.post(conf["token_endpoint"], data=data, timeout=20,
                       headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise AuthError(f"SSO token endpoint unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise AuthError(f"SSO rejected the sign-in ({r.status_code})")
    id_token = r.json().get("id_token")
    if not id_token:
        raise AuthError("SSO returned no id_token (is the 'openid' scope enabled?)")
    return verify_id_token(id_token, flow["nonce"], conf)


def verify_id_token(id_token: str, nonce: str, conf: dict, key=None) -> dict:
    s = cfg()
    try:
        signing = key or _jwks(conf["jwks_uri"]).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, signing, algorithms=["RS256", "ES256", "PS256"],
                            audience=s.oidc_client_id, issuer=conf.get("issuer", s.oidc_issuer),
                            options={"require": ["exp", "iat", "sub"]}, leeway=60)
    except jwt.PyJWTError as exc:
        raise AuthError(f"Invalid id_token: {exc}") from exc
    if claims.get("nonce") != nonce:
        raise AuthError("id_token nonce mismatch")
    return claims


# -- local accounts -------------------------------------------------------------------------------
SCRYPT = (2**14, 8, 1)
COMMON = {"password", "changeme", "admin", "administrator", "letmein", "welcome", "qwerty",
          "123456789012", "password1234", "ignite", "igniteadmin"}


def hash_password(password: str) -> str:
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def check_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             dklen=32)
        return hmac.compare_digest(got, _unb64(digest))
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password(secrets.token_urlsafe(12))


def password_problem(new: str, user: User, current: str | None = None) -> str | None:
    s = cfg()
    if len(new) < s.password_min_length:
        return f"Use at least {s.password_min_length} characters"
    low = new.lower()
    if low in COMMON or low in {(user.username or "").lower(), user.email.lower()}:
        return "That password is too easy to guess"
    if current is not None and new == current:
        return "Choose a password different from the current one"
    return None


def set_password(user: User, new: str, must_change: bool = False) -> None:
    user.password_hash = hash_password(new)
    user.must_change_password = must_change
    user.password_changed_at = datetime.now(UTC)  # ends sessions started before this
    user.failed_logins, user.locked_until = 0, None


def bootstrap(session: Session) -> User | None:
    """A new install gets admin / admin (must be changed at first sign-in), so someone can get in
    before LDAP or SSO is set up. Only when local accounts are on and there's no admin yet."""
    if not enabled() or "local" not in methods():
        return None
    if session.scalar(select(User).where(User.is_admin.is_(True))) is not None:
        return None
    if session.scalar(select(User).where(User.username == "admin")) is not None:
        return None
    user = User(sub="local:admin", username="admin", email="", name="Administrator",
                auth_source="local", is_admin=True, active=True, groups=[])
    set_password(user, cfg().bootstrap_admin_password, must_change=True)
    session.add(user)
    session.commit()
    log.warning("Created the first admin account: sign in as 'admin' and choose a new password")
    return user


def _locked(user: User) -> bool:
    until = user.locked_until
    return bool(until and (until if until.tzinfo else until.replace(tzinfo=UTC))
                > datetime.now(UTC))


def _failed(session: Session, user: User) -> None:
    s = cfg()
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= s.max_failed_logins:
        user.locked_until = datetime.now(UTC) + timedelta(minutes=s.lockout_minutes)
        user.failed_logins = 0
    session.commit()


WRONG = "Wrong username or password"
_unknown_failures: dict[str, list[float]] = {}


def _throttle_unknown(name: str) -> None:
    """Slow down guessing against names that don't exist (or live only in LDAP)."""
    now = time.monotonic()
    window = cfg().lockout_minutes * 60
    hits = [t for t in _unknown_failures.get(name, []) if now - t < window]
    if len(hits) >= cfg().max_failed_logins:
        raise AuthError(f"Too many attempts; try again in {cfg().lockout_minutes} "
                        "minutes")
    hits.append(now)
    _unknown_failures[name] = hits[-50:]
    if len(_unknown_failures) > 10_000:
        _unknown_failures.clear()


def password_login(session: Session, username: str, password: str) -> User:
    """Local account first (when one exists with that name), then the directory."""
    name = username.strip()
    if not name or not password:
        raise AuthError(WRONG)
    ms = methods()
    user = session.scalar(select(User).where(User.username == name.lower())) \
        or session.scalar(select(User).where(User.email == name.lower(), User.email != ""))
    if user is not None and user.auth_source == "local" and "local" in ms:
        if _locked(user):
            raise AuthError(f"Account locked after too many attempts; try again in "
                            f"{cfg().lockout_minutes} minutes or ask an admin")
        if not check_password(password, user.password_hash):
            _failed(session, user)
            raise AuthError(WRONG)
        if not user.active:
            raise AuthError("Your account is disabled; ask an admin")
        user.failed_logins, user.last_login_at = 0, datetime.now(UTC)
        session.commit()
        return user
    if "ldap" in ms and cfg().ldap_url:
        if user is not None and _locked(user):
            raise AuthError("Account locked after too many attempts; try again later")
        _throttle_unknown(name.lower())
        try:
            entry = ldap_authenticate(name, password)
        except AuthError:
            if user is not None:
                _failed(session, user)
            raise
        _unknown_failures.pop(name.lower(), None)
        return upsert_ldap_user(session, entry)
    check_password(password, _DUMMY)  # same timing whether or not the account exists
    _throttle_unknown(name.lower())
    raise AuthError(WRONG)


# -- LDAP -----------------------------------------------------------------------------------------
def _ldap_server():
    import ssl

    import ldap3

    s = cfg()
    tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=s.ldap_ca_file,
                    version=ssl.PROTOCOL_TLS_CLIENT)
    return ldap3.Server(s.ldap_url, use_ssl=s.ldap_url.startswith("ldaps://"), tls=tls,
                        get_info=ldap3.NONE, connect_timeout=s.ldap_timeout_s)


def _ldap_connect(server, user: str | None, password: str | None):
    """Bind and return the connection, or raise AuthError. Tests replace this."""
    import ldap3
    from ldap3.core.exceptions import LDAPException

    s = cfg()
    try:
        conn = ldap3.Connection(server, user=user, password=password,
                                receive_timeout=s.ldap_timeout_s, read_only=True)
        if s.ldap_start_tls and not s.ldap_url.startswith("ldaps://"):
            conn.open()
            conn.start_tls()
        if not conn.bind():
            return None
        return conn
    except LDAPException as exc:
        raise AuthError(f"The directory is unreachable: {exc}") from exc


def ldap_authenticate(username: str, password: str) -> dict:
    """Find the person with the service account, then check their password by binding as them."""
    from ldap3.utils.conv import escape_filter_chars

    s = cfg()
    if not password:  # an empty password is an anonymous bind on many servers: never "success"
        raise AuthError(WRONG)
    server = _ldap_server()
    svc = _ldap_connect(server, s.ldap_bind_dn, s.ldap_bind_password)
    if svc is None:
        raise AuthError("Ignite can't sign in to the directory (check IGNITE_LDAP_BIND_DN)")
    attrs = [a for a in {s.ldap_email_attr, s.ldap_name_attr, s.ldap_group_attr,
                         s.ldap_github_attr} if a]
    svc.search(s.ldap_base_dn or "", s.ldap_user_filter.replace(
        "{username}", escape_filter_chars(username)), attributes=attrs, size_limit=2)
    entries = list(svc.entries)
    svc.unbind()
    if len(entries) != 1:
        raise AuthError(WRONG)
    dn = entries[0].entry_dn
    who = _ldap_connect(server, dn, password)
    if who is None:
        raise AuthError(WRONG)
    who.unbind()
    data = entries[0].entry_attributes_as_dict

    def one(attr):
        v = data.get(attr) if attr else None
        return str(v[0]) if v else None

    groups = [str(g) for g in data.get(s.ldap_group_attr, [])]
    # Teams can name a group by its full DN or just its CN.
    cns = [g.split(",", 1)[0].split("=", 1)[1] for g in groups if g.lower().startswith("cn=")]
    email = (one(s.ldap_email_attr) or "").lower()
    return {"dn": dn, "username": username.lower(), "email": email,
            "name": one(s.ldap_name_attr) or username, "groups": groups + cns,
            "github": one(s.ldap_github_attr)}


def upsert_ldap_user(session: Session, entry: dict) -> User:
    s = cfg()
    user = session.scalar(select(User).where(User.sub == f"ldap:{entry['dn']}"))
    if user is None and entry["email"]:  # invited by email before their first sign-in?
        user = session.scalar(select(User).where(User.sub == f"pending:{entry['email']}"))
    if user is None:
        user = User(sub=f"ldap:{entry['dn']}", email=entry["email"], active=True, is_admin=False)
        session.add(user)
    if user.active is False:
        raise AuthError("Your account is disabled; ask an admin")
    clash = session.scalar(select(User).where(User.username == entry["username"],
                                              User.id != user.id)) if user.id else None
    user.sub, user.auth_source = f"ldap:{entry['dn']}", "ldap"
    user.username = None if clash else entry["username"]
    user.email, user.name, user.groups = entry["email"], entry["name"], entry["groups"]
    if entry.get("github"):
        user.github_login = entry["github"]
    if entry["email"] and entry["email"] in {e.lower() for e in s.admin_emails}:
        user.is_admin = True
    user.failed_logins, user.last_login_at = 0, datetime.now(UTC)
    session.flush()
    sync_groups(session, user)
    session.commit()
    return user


# -- users ----------------------------------------------------------------------------------------
def upsert_user(session: Session, claims: dict) -> User:
    s = cfg()
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    if not email:
        raise AuthError("Your SSO account has no email claim")
    if claims.get("email_verified") is False:
        raise AuthError("Your SSO email is not verified")
    user = session.scalar(select(User).where(User.sub == claims["sub"]))
    if user is None:  # invited by an admin before their first sign-in?
        user = session.scalar(select(User).where(User.sub == f"pending:{email}"))
        if user is not None:
            user.sub = claims["sub"]
    if user is None:
        user = User(sub=claims["sub"], email=email, active=True, is_admin=False)
        session.add(user)
    user.auth_source = "oidc"
    if user.active is False:
        raise AuthError("Your Ignite account is disabled; ask an admin")
    user.email = email
    user.name = claims.get("name") or claims.get("given_name") or email.split("@")[0]
    groups = claims.get(s.oidc_groups_claim) or []
    user.groups = [str(g) for g in (groups if isinstance(groups, list) else [groups])]
    if s.oidc_github_claim and claims.get(s.oidc_github_claim):
        user.github_login = str(claims[s.oidc_github_claim])
    if email in {e.lower() for e in s.admin_emails}:
        user.is_admin = True
    user.last_login_at = datetime.now(UTC)
    session.flush()
    sync_groups(session, user)
    return user


def sync_groups(session: Session, user: User) -> None:
    """Team memberships that came from SSO follow the user's current groups."""
    groups = set(user.groups or [])
    teams = session.scalars(select(Team).where(Team.sso_group.is_not(None))).all()
    want = {t.id: t.sso_role or "contributor" for t in teams if t.sso_group in groups}
    have = {m.scope_id: m for m in session.scalars(select(Membership).where(
        Membership.user_id == user.id, Membership.scope == "team", Membership.source == "sso"))}
    for team_id, m in have.items():
        if team_id not in want:
            session.delete(m)
        else:
            m.role = want[team_id]
    manual = {m.scope_id for m in session.scalars(select(Membership).where(
        Membership.user_id == user.id, Membership.scope == "team",
        Membership.source == "manual"))}
    for team_id, role in want.items():
        if team_id not in have and team_id not in manual:
            session.add(Membership(user_id=user.id, scope="team", scope_id=team_id, role=role,
                                   source="sso"))


# -- sessions -------------------------------------------------------------------------------------
def session_cookie(user: User) -> str:
    s = cfg()
    return jwt.encode({"uid": user.id, "iat": int(time.time()), "t": time.time(),
                       "exp": int(time.time()) + s.session_hours * 3600},
                      secret(), algorithm="HS256")


def user_from_cookie(session: Session, value: str | None) -> User | None:
    if not value:
        return None
    try:
        data = jwt.decode(value, secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    user = session.get(User, data.get("uid", ""))
    if not user or not user.active:
        return None
    changed = user.password_changed_at
    started = float(data.get("t", data.get("iat", 0)))
    if changed and (changed if changed.tzinfo else changed.replace(tzinfo=UTC)).timestamp() \
            > started:
        return None  # the password changed after this session began
    return user


# -- personal tokens ------------------------------------------------------------------------------
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(session: Session, user: User, name: str,
              days: int | None = 90) -> tuple[str, ApiToken]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user.id, name=name[:100] or "token", token_hash=_hash(raw),
                   prefix=raw[:10],
                   expires_at=datetime.now(UTC) + timedelta(days=days) if days else None)
    session.add(row)
    session.flush()
    return raw, row


def user_from_token(session: Session, raw: str) -> User | None:
    row = session.scalar(select(ApiToken).where(ApiToken.token_hash == _hash(raw)))
    if row is None or row.revoked:
        return None
    exp = row.expires_at
    if exp is not None and (exp if exp.tzinfo else exp.replace(tzinfo=UTC)) < datetime.now(UTC):
        return None
    user = session.get(User, row.user_id)
    if not user or not user.active:
        return None
    row.last_used_at = datetime.now(UTC)
    session.commit()
    return user


# -- request dependency ---------------------------------------------------------------------------
def principal_for(request: Request, session: Session) -> access.Principal | None:
    if not enabled():
        return access.LOCAL
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        raw = header[7:].strip()
        user = user_from_token(session, raw) if raw.startswith(TOKEN_PREFIX) else None
        return access.from_user(user, via="token") if user else None
    user = user_from_cookie(session, request.cookies.get(SESSION_COOKIE))
    return access.from_user(user) if user else None


def get_principal(request: Request, session: Session = Depends(get_session)) -> access.Principal:
    p = principal_for(request, session)
    if p is None:
        raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    return p
