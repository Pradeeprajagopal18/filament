"""Sign-in, the current user, personal tokens, and people & access administration."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from igniteai import access, auth
from igniteai.api.graph import get_or_404
from igniteai.authconfig import cfg
from igniteai.db import get_session
from igniteai.models import (
    ApiToken,
    AuditEvent,
    CheckpointRole,
    Membership,
    Project,
    Team,
    User,
    Workspace,
)

router = APIRouter()

CHECKPOINT_ROLES = ["product_owner", "tech_lead", "engineer", "release_manager", "on_call"]


def _cookie_secure() -> bool:
    return cfg().ui_url.startswith("https://")


# -- sign-in --------------------------------------------------------------------------------------
@router.get("/auth/config")
def auth_config():
    s = cfg()
    ms = auth.methods() if auth.enabled() else []
    return {"mode": "none" if s.auth_mode == "none" else "login", "methods": ms,
            "password": bool({"local", "ldap"} & set(ms)),
            "sso_url": "/api/auth/oidc/login" if "oidc" in ms else None}


class LoginIn(BaseModel):
    username: str
    password: str


def _session_response(user: User, body: dict) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(auth.SESSION_COOKIE, auth.session_cookie(user),
                    max_age=cfg().session_hours * 3600, httponly=True, samesite="lax",
                    secure=_cookie_secure(), path="/")
    return resp


@router.post("/auth/login")
def password_login(body: LoginIn, session: Session = Depends(get_session)):
    """Username and password: a local account or the company directory (LDAP)."""
    if not auth.enabled():
        raise HTTPException(400, "Sign-in is off (IGNITE_AUTH_MODE=none)")
    if not {"local", "ldap"} & set(auth.methods()):
        raise HTTPException(400, "Password sign-in is off; use SSO")
    auth.bootstrap(session)
    try:
        user = auth.password_login(session, body.username, body.password)
    except auth.AuthError as exc:
        session.rollback()
        access.audit(session, "auth.failed", p=access.SYSTEM, username=body.username[:100],
                     reason=str(exc)[:200])
        session.commit()
        raise HTTPException(401, str(exc)) from exc
    access.audit(session, "auth.login", p=access.from_user(user), method=user.auth_source)
    session.commit()
    return _session_response(user, {"ok": True, "must_change_password":
                                    bool(user.must_change_password)})


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/auth/password")
def change_password(body: PasswordIn, session: Session = Depends(get_session)):
    user = _me(session)
    if user.auth_source != "local":
        raise HTTPException(400, "Your password is managed by the company directory or SSO")
    if not auth.check_password(body.current, user.password_hash):
        raise HTTPException(403, "The current password is wrong")
    if problem := auth.password_problem(body.new, user, body.current):
        raise HTTPException(422, problem)
    auth.set_password(user, body.new)
    access.audit(session, "auth.password_changed", resource=user.id)
    session.commit()
    # A fresh session: the old ones (on other devices too) end with the password change.
    return _session_response(user, {"ok": True})


@router.get("/auth/oidc/login")
def login(next: str = "/"):
    try:
        url, flow = auth.start_login(next)
    except auth.AuthError as exc:
        raise HTTPException(503, str(exc)) from exc
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(auth.FLOW_COOKIE, flow, max_age=600, httponly=True, samesite="lax",
                    secure=_cookie_secure(), path="/api/auth")
    return resp


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             session: Session = Depends(get_session)):
    ui = cfg().ui_url.rstrip("/")
    if error:
        return RedirectResponse(f"{ui}/?login_error={error}", status_code=302)
    try:
        flow = auth.read_flow(request.cookies.get(auth.FLOW_COOKIE), state)
        claims = auth.exchange(code, flow)
        user = auth.upsert_user(session, claims)
    except auth.AuthError as exc:
        session.rollback()
        access.audit(session, "auth.failed", p=access.SYSTEM, reason=str(exc)[:200])
        session.commit()
        from urllib.parse import quote

        return RedirectResponse(f"{ui}/?login_error={quote(str(exc)[:200])}", status_code=302)
    access.audit(session, "auth.login", p=access.from_user(user))
    session.commit()
    resp = RedirectResponse(ui + flow.get("next", "/"), status_code=302)
    resp.set_cookie(auth.SESSION_COOKIE, auth.session_cookie(user),
                    max_age=cfg().session_hours * 3600, httponly=True, samesite="lax",
                    secure=_cookie_secure(), path="/")
    resp.delete_cookie(auth.FLOW_COOKIE, path="/api/auth")
    return resp


@router.post("/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/me")
def me(session: Session = Depends(get_session)):
    p = access.current()
    workspaces = []
    for ws in session.scalars(select(Workspace).order_by(Workspace.created_at)):
        role = access.workspace_role(session, p, ws.id)
        if role:
            from igniteai import features

            workspaces.append({"id": ws.id, "name": ws.name, "role": role,
                               "features": features.allowed_set(session, p, ws.id)})
    roles = []
    if p.user_id:
        roles = [{"workspace_id": r.workspace_id, "project_id": r.project_id, "role": r.role}
                 for r in session.scalars(select(CheckpointRole).where(
                     CheckpointRole.user_id == p.user_id))]
    user = session.get(User, p.user_id) if p.user_id else None
    return {"mode": "none" if not auth.enabled() else "login", "id": p.user_id,
            "email": p.email, "name": p.name, "username": user.username if user else None,
            "auth_source": user.auth_source if user else "local",
            "must_change_password": bool(user and user.must_change_password),
            "is_admin": p.is_admin, "github_login": p.github_login, "via": p.via,
            "workspaces": workspaces, "checkpoint_roles": roles}


# -- personal tokens ------------------------------------------------------------------------------
class TokenIn(BaseModel):
    name: str
    days: int | None = 90


def _me(session: Session) -> User:
    p = access.current()
    user = session.get(User, p.user_id) if p.user_id else None
    if user is None:
        raise HTTPException(400, "This needs sign-in (IGNITE_AUTH_MODE=login)")
    return user


@router.get("/auth/tokens")
def list_tokens(session: Session = Depends(get_session)):
    user = _me(session)
    rows = session.scalars(select(ApiToken).where(ApiToken.user_id == user.id,
                                                  ApiToken.revoked.is_(False)))
    return [{"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": t.created_at,
             "expires_at": t.expires_at, "last_used_at": t.last_used_at} for t in rows]


@router.post("/auth/tokens", status_code=201)
def create_token(body: TokenIn, session: Session = Depends(get_session)):
    user = _me(session)
    if body.days is not None and not 1 <= body.days <= 366:
        raise HTTPException(422, "days must be between 1 and 366")
    raw, row = auth.new_token(session, user, body.name, body.days)
    access.audit(session, "token.create", resource=row.id, name=row.name)
    session.commit()
    return {"id": row.id, "name": row.name, "token": raw, "expires_at": row.expires_at,
            "note": "Copy it now; it is not shown again."}


@router.delete("/auth/tokens/{token_id}", status_code=204)
def revoke_token(token_id: str, session: Session = Depends(get_session)):
    user = _me(session)
    row = session.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Token not found")
    row.revoked = True
    access.audit(session, "token.revoke", resource=row.id)
    session.commit()


# -- sign-in settings (admins) -------------------------------------------------------------------
@router.get("/auth/settings")
def get_auth_settings(session: Session = Depends(get_session)):
    from igniteai import authconfig
    from igniteai.models import AuthSetting

    row = session.get(AuthSetting, "auth")
    return {"fields": authconfig.view(), "redirect_uri": auth.redirect_url(),
            "updated_by": row.updated_by if row else None,
            "updated_at": row.updated_at if row else None,
            "ldap_available": _ldap_available()}


def _ldap_available() -> bool:
    try:
        import ldap3  # noqa: F401
    except ImportError:
        return False
    return True


@router.put("/auth/settings")
def put_auth_settings(body: dict, session: Session = Depends(get_session)):
    """Change sign-in settings. Send only what changes; null puts a value back to the server's
    environment default."""
    from igniteai import authconfig

    p = access.current()
    me_user = session.get(User, p.user_id) if p.user_id else None
    try:
        data = authconfig.validate(session, body, me_user.auth_source if me_user else None)
    except authconfig.ConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    before = authconfig.view()
    authconfig.save(session, data, access.actor())
    after = authconfig.view()
    changed = {k: after[k]["value"] for k in after if after[k]["value"] != before[k]["value"]}
    access.audit(session, "auth.settings", resource="sign-in", changed=changed)
    session.commit()
    auth.bootstrap(session)  # local accounts just turned on with no admin yet
    return get_auth_settings(session)


class LdapTestIn(BaseModel):
    settings: dict = {}           # unsaved form values to try
    username: str | None = None   # optionally also try signing someone in
    password: str | None = None


def _trial(overrides: dict) -> dict:
    """Apply unsaved form values for the length of a test; returns what to restore."""
    from igniteai import authconfig

    saved = dict(authconfig._override)
    authconfig._override = {**saved, **{k: v for k, v in overrides.items()
                                        if k in authconfig.FIELDS and v not in (None, "")}}
    return saved


@router.post("/auth/settings/test-ldap")
def test_ldap(body: LdapTestIn, session: Session = Depends(get_session)):
    from igniteai import authconfig

    if not _ldap_available():
        return {"ok": False, "message": "The ldap3 package isn't installed on the server"}
    saved = _trial(body.settings)
    try:
        c = cfg()
        if not c.ldap_url:
            return {"ok": False, "message": "Enter the server URL first"}
        if c.ldap_bind_dn and not c.ldap_bind_password:
            return {"ok": False, "message": "The service account password reference doesn't "
                                            "resolve: is that variable set on the server?"}
        conn = auth._ldap_connect(auth._ldap_server(), c.ldap_bind_dn, c.ldap_bind_password)
        if conn is None:
            return {"ok": False, "message": "Connected, but the service account couldn't sign "
                                            "in (check the bind DN and password)"}
        conn.unbind()
        if body.username:
            entry = auth.ldap_authenticate(body.username, body.password or "")
            groups = [g for g in entry["groups"] if not g.lower().startswith("cn=")]
            email = entry["email"] or "no email"
            return {"ok": True, "message": f"Signed in {entry['name']} <{email}>, "
                                           f"{len(groups)} groups"}
        return {"ok": True, "message": "Connected and the service account signed in"}
    except auth.AuthError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - show what went wrong, never a 500
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        authconfig._override = saved


@router.post("/auth/settings/test-oidc")
def test_oidc(body: LdapTestIn, session: Session = Depends(get_session)):
    from igniteai import authconfig

    saved = _trial(body.settings)
    try:
        c = cfg()
        if not (c.oidc_issuer and c.oidc_client_id):
            return {"ok": False, "message": "Enter the issuer and client ID first"}
        auth._discovery.cache_clear()
        conf = auth.discovery()
        missing = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")
                   if k not in conf]
        if missing:
            return {"ok": False, "message": f"The issuer's configuration lacks {missing}"}
        found = conf.get("issuer", c.oidc_issuer)
        return {"ok": True, "message": f"Found the provider at {found}. Register this "
                                       f"redirect URI with it: {auth.redirect_url()}"}
    except auth.AuthError as exc:
        return {"ok": False, "message": str(exc)}
    finally:
        authconfig._override = saved


# -- features (admins) ---------------------------------------------------------------------------
@router.get("/workspaces/{ws_id}/features")
def list_features(ws_id: str, session: Session = Depends(get_session)):
    from igniteai import features

    out = features.describe(session, ws_id)
    for f in out:
        f["people"] = features.who_has(session, ws_id, f["key"])
    return out


class FeatureIn(BaseModel):
    mode: str                       # everyone | some | off
    teams: list[str] = []
    groups: list[str] = []
    users: list[str] = []


@router.put("/workspaces/{ws_id}/features/{feature}")
def set_feature(ws_id: str, feature: str, body: FeatureIn,
                session: Session = Depends(get_session)):
    from igniteai import features
    from igniteai.models import FeatureAccess

    if feature not in features.CATALOG:
        raise HTTPException(404, f"Unknown feature {feature}")
    if body.mode not in {"everyone", "some", "off"}:
        raise HTTPException(422, "mode must be everyone, some or off")
    team_ids = {t.id for t in session.scalars(select(Team).where(Team.workspace_id == ws_id))}
    if set(body.teams) - team_ids:
        raise HTTPException(422, "Unknown team")
    if any(session.get(User, u) is None for u in body.users):
        raise HTTPException(422, "Unknown person")
    row = session.scalar(select(FeatureAccess).where(FeatureAccess.workspace_id == ws_id,
                                                     FeatureAccess.feature == feature))
    if body.mode == "everyone":
        if row is not None:
            session.delete(row)
    else:
        if row is None:
            row = FeatureAccess(workspace_id=ws_id, feature=feature)
            session.add(row)
        row.mode = body.mode
        some = body.mode == "some"
        row.teams = sorted(set(body.teams)) if some else []
        row.groups = sorted({g.strip() for g in body.groups if g.strip()}) if some else []
        row.users = sorted(set(body.users)) if some else []
        row.updated_by = access.actor()
    access.audit(session, "feature.access", workspace_id=ws_id, resource=feature,
                 **body.model_dump())
    session.commit()
    return next(f for f in list_features(ws_id, session) if f["key"] == feature)


# -- people ---------------------------------------------------------------------------------------
def _person(session: Session, ws_id: str, user: User) -> dict:
    ms = session.scalars(select(Membership).where(Membership.user_id == user.id)).all()
    ws_role = next((m.role for m in ms if m.scope == "workspace" and m.scope_id == ws_id), None)
    return {"id": user.id, "email": user.email, "name": user.name, "username": user.username,
            "auth_source": user.auth_source, "locked": auth._locked(user),
            "must_change_password": bool(user.must_change_password),
            "github_login": user.github_login, "is_admin": user.is_admin, "active": user.active,
            "groups": user.groups, "last_login_at": user.last_login_at,
            "workspace_role": "admin" if user.is_admin else (ws_role or (
                "member" if cfg().workspace_default_role == "member" else None)),
            "teams": [{"team_id": m.scope_id, "role": m.role, "source": m.source}
                      for m in ms if m.scope == "team"],
            "projects": [{"project_id": m.scope_id, "role": m.role}
                         for m in ms if m.scope == "project"]}


@router.get("/workspaces/{ws_id}/people")
def people(ws_id: str, session: Session = Depends(get_session)):
    users = session.scalars(select(User).order_by(User.email)).all()
    return [_person(session, ws_id, u) for u in users]


class PersonIn(BaseModel):
    email: str = ""
    name: str = ""
    workspace_role: str = "member"
    github_login: str | None = None
    # Set to create a local account (sign-in with username and password, kept in Ignite)
    # instead of inviting someone who will come through LDAP or SSO.
    local_username: str | None = None


def _temp_password() -> str:
    return secrets.token_urlsafe(12)


@router.post("/workspaces/{ws_id}/people", status_code=201)
def invite(ws_id: str, body: PersonIn, session: Session = Depends(get_session)):
    """Add someone: a local account (with a one-time password) or an invite by email that's
    matched when they first sign in through LDAP or SSO."""
    email = body.email.strip().lower()
    temp = None
    if body.local_username:
        if "local" not in auth.methods():
            raise HTTPException(422, "Local accounts are off (add 'local' to "
                                     "IGNITE_AUTH_METHODS)")
        username = body.local_username.strip().lower()
        if not username or not all(c.isalnum() or c in "._-@" for c in username):
            raise HTTPException(422, "Usernames use letters, numbers and . _ - @")
        if session.scalar(select(User).where(User.username == username)):
            raise HTTPException(409, "That username is taken")
        user = User(sub=f"local:{username}", username=username, email=email,
                    name=body.name or username, auth_source="local", active=True,
                    is_admin=False, groups=[])
        temp = _temp_password()
        auth.set_password(user, temp, must_change=True)
        session.add(user)
        session.flush()
    else:
        if "@" not in email:
            raise HTTPException(422, "A valid email is required")
        user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(sub=f"pending:{email}", email=email, name=body.name, active=True,
                    is_admin=False, groups=[])
        session.add(user)
        session.flush()
    _set_ws_role(session, ws_id, user, body.workspace_role)
    if body.github_login is not None:
        user.github_login = body.github_login.strip() or None
    access.audit(session, "people.add", workspace_id=ws_id, resource=user.id, email=email,
                 role=body.workspace_role, local=bool(temp))
    session.commit()
    out = _person(session, ws_id, user)
    if temp:
        out["temporary_password"] = temp  # shown once; they must change it at first sign-in
    return out


@router.post("/workspaces/{ws_id}/people/{user_id}/reset-password")
def reset_password(ws_id: str, user_id: str, session: Session = Depends(get_session)):
    user = get_or_404(session, User, user_id)
    if user.auth_source != "local":
        raise HTTPException(422, "This person signs in through the directory or SSO")
    temp = _temp_password()
    auth.set_password(user, temp, must_change=True)
    access.audit(session, "people.reset_password", workspace_id=ws_id, resource=user.id)
    session.commit()
    return {"temporary_password": temp, "note": "Share it privately; they must change it at "
            "first sign-in. Their other sessions have ended."}


class PersonPatch(BaseModel):
    workspace_role: str | None = None   # admin | member | none
    github_login: str | None = None
    active: bool | None = None
    teams: list[dict] | None = None     # [{team_id, role}] (manual memberships)
    projects: list[dict] | None = None  # [{project_id, role}]


def _set_ws_role(session: Session, ws_id: str, user: User, role: str) -> None:
    if role not in {"admin", "member", "none"}:
        raise HTTPException(422, "workspace_role must be admin, member or none")
    m = session.scalar(select(Membership).where(Membership.user_id == user.id,
                                                Membership.scope == "workspace",
                                                Membership.scope_id == ws_id))
    if role == "none" or (role == "member"
                          and cfg().workspace_default_role == "member"):
        if m:
            session.delete(m)
        if role == "none" and cfg().workspace_default_role == "member":
            raise HTTPException(422, "Everyone who signs in is a member "
                                "(IGNITE_WORKSPACE_DEFAULT_ROLE=member); deactivate the "
                                "person instead")
        return
    if m:
        m.role = role
    else:
        session.add(Membership(user_id=user.id, scope="workspace", scope_id=ws_id, role=role))


@router.patch("/workspaces/{ws_id}/people/{user_id}")
def update_person(ws_id: str, user_id: str, body: PersonPatch,
                  session: Session = Depends(get_session)):
    user = get_or_404(session, User, user_id)
    if body.workspace_role is not None:
        _set_ws_role(session, ws_id, user, body.workspace_role)
    if body.github_login is not None:
        user.github_login = body.github_login.strip() or None
    if body.active is not None:
        if not body.active and user.id == access.current().user_id:
            raise HTTPException(422, "You can't deactivate yourself")
        user.active = body.active
    if body.teams is not None:
        team_ids = {t.id for t in session.scalars(select(Team).where(Team.workspace_id == ws_id))}
        _replace(session, user, "team", team_ids, body.teams, "team_id", access.TEAM_ROLES)
    if body.projects is not None:
        pids = {x.id for x in session.scalars(select(Project).where(
            Project.workspace_id == ws_id))}
        _replace(session, user, "project", pids, body.projects, "project_id",
                 access.PROJECT_ROLES)
    access.audit(session, "people.update", workspace_id=ws_id, resource=user.id,
                 change=body.model_dump(exclude_none=True))
    session.commit()
    return _person(session, ws_id, user)


def _replace(session, user, scope, allowed, items, key, roles) -> None:
    for item in items:
        if item.get(key) not in allowed or item.get("role") not in roles:
            raise HTTPException(422, f"Unknown {scope} or role: {item}")
    for m in session.scalars(select(Membership).where(
            Membership.user_id == user.id, Membership.scope == scope,
            Membership.scope_id.in_(allowed), Membership.source == "manual")):
        session.delete(m)
    session.flush()
    for item in items:
        session.add(Membership(user_id=user.id, scope=scope, scope_id=item[key],
                               role=item["role"]))


# -- teams & SSO groups ---------------------------------------------------------------------------
class TeamAccessIn(BaseModel):
    sso_group: str | None = None
    sso_role: str = "contributor"


@router.put("/workspaces/{ws_id}/teams/{team_id}")
def set_team_group(ws_id: str, team_id: str, body: TeamAccessIn,
                   session: Session = Depends(get_session)):
    team = get_or_404(session, Team, team_id)
    if team.workspace_id != ws_id:
        raise HTTPException(404, "Team not found")
    if body.sso_role not in access.TEAM_ROLES:
        raise HTTPException(422, f"sso_role must be one of {access.TEAM_ROLES}")
    team.sso_group, team.sso_role = (body.sso_group or "").strip() or None, body.sso_role
    for user in session.scalars(select(User)):
        auth.sync_groups(session, user)
    access.audit(session, "team.sso", workspace_id=ws_id, resource=team.id,
                 group=team.sso_group, role=team.sso_role)
    session.commit()
    return {"id": team.id, "name": team.name, "sso_group": team.sso_group,
            "sso_role": team.sso_role}


# -- projects -------------------------------------------------------------------------------------
class ProjectAccessIn(BaseModel):
    restricted: bool


@router.patch("/projects/{project_id}/access")
def set_restricted(project_id: str, body: ProjectAccessIn,
                   session: Session = Depends(get_session)):
    project = get_or_404(session, Project, project_id)
    project.restricted = body.restricted
    access.audit(session, "project.restricted", workspace_id=project.workspace_id,
                 resource=project.id, restricted=body.restricted)
    session.commit()
    return project_access(project_id, session)


@router.get("/projects/{project_id}/access")
def project_access(project_id: str, session: Session = Depends(get_session)):
    project = get_or_404(session, Project, project_id)
    members = []
    for m in session.scalars(select(Membership).where(Membership.scope == "project",
                                                      Membership.scope_id == project.id)):
        u = session.get(User, m.user_id)
        if u:
            members.append({"user_id": u.id, "email": u.email, "name": u.name, "role": m.role})
    team = session.get(Team, project.team_id) if project.team_id else None
    p = access.current()
    return {"project_id": project.id, "restricted": project.restricted,
            "team": {"id": team.id, "name": team.name, "sso_group": team.sso_group}
            if team else None,
            "members": members, "my_role": access.project_role(session, p, project),
            "checkpoints": {r: [_who(session, uid) for uid in access.holders(session, project, r)]
                            for r in CHECKPOINT_ROLES}}


def _who(session: Session, user_id: str) -> dict:
    u = session.get(User, user_id)
    return {"user_id": user_id, "email": u.email if u else "", "name": u.name if u else ""}


class MemberIn(BaseModel):
    user_id: str
    role: str


@router.put("/projects/{project_id}/members")
def set_project_members(project_id: str, body: list[MemberIn],
                        session: Session = Depends(get_session)):
    project = get_or_404(session, Project, project_id)
    for b in body:
        if b.role not in access.PROJECT_ROLES or session.get(User, b.user_id) is None:
            raise HTTPException(422, f"Unknown user or role: {b.user_id} {b.role}")
    p = access.current()
    if any(b.role == "owner" for b in body) and not access.can(session, p, project, "owner"):
        raise HTTPException(403, "Only an owner can make someone an owner")
    for m in session.scalars(select(Membership).where(Membership.scope == "project",
                                                      Membership.scope_id == project.id)):
        session.delete(m)
    session.flush()
    for b in body:
        session.add(Membership(user_id=b.user_id, scope="project", scope_id=project.id,
                               role=b.role))
    access.audit(session, "project.members", workspace_id=project.workspace_id,
                 resource=project.id, members=[b.model_dump() for b in body])
    session.commit()
    return project_access(project_id, session)


# -- checkpoint roles -----------------------------------------------------------------------------
class CheckpointIn(BaseModel):
    user_id: str
    role: str
    project_id: str | None = None


@router.get("/workspaces/{ws_id}/checkpoint-roles")
def checkpoint_roles(ws_id: str, session: Session = Depends(get_session)):
    rows = session.scalars(select(CheckpointRole).where(CheckpointRole.workspace_id == ws_id))
    out = []
    for r in rows:
        u = session.get(User, r.user_id)
        out.append({"id": r.id, "user_id": r.user_id, "email": u.email if u else "",
                    "role": r.role, "project_id": r.project_id})
    return out


@router.put("/workspaces/{ws_id}/checkpoint-roles", status_code=201)
def add_checkpoint_role(ws_id: str, body: CheckpointIn, session: Session = Depends(get_session)):
    if body.role not in CHECKPOINT_ROLES:
        raise HTTPException(422, f"role must be one of {CHECKPOINT_ROLES}")
    get_or_404(session, User, body.user_id)
    if body.project_id:
        proj = get_or_404(session, Project, body.project_id)
        if proj.workspace_id != ws_id:
            raise HTTPException(404, "Project not found")
    existing = session.scalar(select(CheckpointRole).where(
        CheckpointRole.user_id == body.user_id, CheckpointRole.workspace_id == ws_id,
        CheckpointRole.project_id == body.project_id, CheckpointRole.role == body.role))
    if existing is None:
        existing = CheckpointRole(user_id=body.user_id, workspace_id=ws_id,
                                  project_id=body.project_id, role=body.role)
        session.add(existing)
        access.audit(session, "checkpoint.assign", workspace_id=ws_id, resource=body.user_id,
                     role=body.role, project_id=body.project_id)
        session.commit()
    return {"id": existing.id, **body.model_dump()}


@router.delete("/workspaces/{ws_id}/checkpoint-roles/{role_id}", status_code=204)
def remove_checkpoint_role(ws_id: str, role_id: str, session: Session = Depends(get_session)):
    row = session.get(CheckpointRole, role_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(404, "Not found")
    session.delete(row)
    access.audit(session, "checkpoint.remove", workspace_id=ws_id, resource=row.user_id,
                 role=row.role)
    session.commit()


# -- audit ----------------------------------------------------------------------------------------
@router.get("/workspaces/{ws_id}/audit")
def audit_log(ws_id: str, limit: int = 200, action: str | None = None,
              session: Session = Depends(get_session)):
    stmt = select(AuditEvent).where((AuditEvent.workspace_id == ws_id)
                                    | AuditEvent.workspace_id.is_(None))
    if action:
        stmt = stmt.where(AuditEvent.action.like(f"{action}%"))
    rows = session.scalars(stmt.order_by(AuditEvent.at.desc()).limit(min(limit, 1000)))
    return [{"at": e.at, "actor": e.actor, "action": e.action, "resource": e.resource,
             "detail": e.detail} for e in rows]
