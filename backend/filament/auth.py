"""Sign-in with the company SSO (OpenID Connect), sessions and personal API tokens.

Flow: /api/auth/login redirects to the identity provider (Authorization Code + PKCE). The IdP
redirects back to /api/auth/callback; we exchange the code, verify the id_token against the
IdP's published keys (issuer, audience, expiry, nonce), create or update the User, sync SSO
groups into team memberships, and set a signed, HttpOnly session cookie.

CLI and MCP clients use personal tokens (Authorization: Bearer fil_...). Only a SHA-256 hash is
stored; the token is shown once.

With FILAMENT_AUTH_MODE=none every request is the local admin (laptop use, tests).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from filament import access
from filament.config import get_settings
from filament.db import get_session
from filament.models import ApiToken, Membership, Team, User

SESSION_COOKIE = "filament_session"
FLOW_COOKIE = "filament_oidc"
TOKEN_PREFIX = "fil_"


class AuthError(RuntimeError):
    pass


def enabled() -> bool:
    return get_settings().auth_mode == "oidc"


def redirect_url() -> str:
    s = get_settings()
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
    s = get_settings()
    if not (s.oidc_issuer and s.oidc_client_id):
        raise AuthError("SSO is not configured: set FILAMENT_OIDC_ISSUER and "
                        "FILAMENT_OIDC_CLIENT_ID")
    return _discovery(s.oidc_issuer, int(time.time() // 3600))


@lru_cache(maxsize=4)
def _jwks(uri: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(uri, cache_keys=True, lifespan=3600)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def start_login(next_path: str = "/") -> tuple[str, str]:
    """(authorize URL, flow cookie value)."""
    s, conf = get_settings(), discovery()
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
                      s.session_secret, algorithm="HS256")
    return url, flow


def _safe_next(path: str | None) -> str:
    # Only same-site paths, so the login can't be used as an open redirect.
    return path if path and path.startswith("/") and not path.startswith("//") else "/"


def read_flow(cookie: str | None, state: str) -> dict:
    try:
        flow = jwt.decode(cookie or "", get_settings().session_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("The sign-in took too long or was started elsewhere; try again") from exc
    if not secrets.compare_digest(flow.get("state", ""), state or ""):
        raise AuthError("Sign-in state mismatch; try again")
    return flow


def exchange(code: str, flow: dict) -> dict:
    """Swap the code for tokens and return the verified id_token claims."""
    import httpx

    s, conf = get_settings(), discovery()
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
    s = get_settings()
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


# -- users ----------------------------------------------------------------------------------------
def upsert_user(session: Session, claims: dict) -> User:
    s = get_settings()
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
    if user.active is False:
        raise AuthError("Your Filament account is disabled; ask an admin")
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
    s = get_settings()
    return jwt.encode({"uid": user.id, "iat": int(time.time()),
                       "exp": int(time.time()) + s.session_hours * 3600},
                      s.session_secret, algorithm="HS256")


def user_from_cookie(session: Session, value: str | None) -> User | None:
    if not value:
        return None
    try:
        data = jwt.decode(value, get_settings().session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    user = session.get(User, data.get("uid", ""))
    return user if user and user.active else None


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
