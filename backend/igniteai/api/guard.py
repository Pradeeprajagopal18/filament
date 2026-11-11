"""One gate in front of every API route: who is asking, and may they?

The rules are by route template so a new route can't silently skip them: a route that is not
listed as admin-only or open falls back to "reads need viewer, writes need contributor" on
whatever workspace/project/repo its path points at. Handlers then filter lists and check the
finer rules (checkpoint roles, the project a new repo goes into) themselves.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from igniteai import access, auth, authconfig, features
from igniteai.db import get_session

OPEN = {
    "/api/health", "/api/auth/config", "/api/auth/login", "/api/auth/oidc/login",
    "/api/auth/callback", "/api/auth/logout",
    "/api/webhooks/github",  # authenticated by the webhook signature
}

# Only workspace admins (or global admins where there is no workspace in the path).
ADMIN = {
    ("POST", "/api/workspaces"),
    ("GET", "/api/auth/settings"),
    ("GET", "/api/workspaces/{ws_id}/features"),
    ("GET", "/api/workspaces/{ws_id}/knowledge"),
    ("POST", "/api/workspaces/{ws_id}/knowledge/sources"),
    ("GET", "/api/knowledge/{source_id}"),
    ("PATCH", "/api/knowledge/{source_id}"),
    ("DELETE", "/api/knowledge/{source_id}"),
    ("POST", "/api/knowledge/{source_id}/sync"),
    ("POST", "/api/knowledge/{source_id}/test"),
    ("POST", "/api/knowledge/{source_id}/files"),
    ("DELETE", "/api/knowledge/{source_id}/docs/{doc_id}"),
    ("PUT", "/api/workspaces/{ws_id}/knowledge/index"),
    ("POST", "/api/workspaces/{ws_id}/knowledge/index/test"),
    ("PUT", "/api/workspaces/{ws_id}/features/{feature}"),
    ("PUT", "/api/auth/settings"),
    ("POST", "/api/auth/settings/test-ldap"),
    ("POST", "/api/auth/settings/test-oidc"),
    ("POST", "/api/repos/scan-due"),
    ("POST", "/api/readiness/scan"),
    ("GET", "/api/workspaces/{ws_id}/config"),
    ("POST", "/api/workspaces/{ws_id}/config"),
    ("PUT", "/api/workspaces/{ws_id}/agent-settings/{agent}"),
    ("POST", "/api/workspaces/{ws_id}/teams"),
    ("GET", "/api/workspaces/{ws_id}/usage"),
    ("POST", "/api/workspaces/{ws_id}/mcp"),
    ("GET", "/api/workspaces/{ws_id}/mcp/calls"),
    ("GET", "/api/workspaces/{ws_id}/mcp/metrics"),
    ("PATCH", "/api/mcp/{server_id}"),
    ("DELETE", "/api/mcp/{server_id}"),
    ("POST", "/api/mcp/{server_id}/sync"),
    ("POST", "/api/mcp/{server_id}/test"),
    ("PATCH", "/api/mcp-tools/{tool_id}"),
    ("POST", "/api/mcp-tools/{tool_id}/approve"),
    ("POST", "/api/mcp-tools/{tool_id}/reject"),
    ("POST", "/api/mcp-tools/{tool_id}/rollback"),
    ("POST", "/api/workspaces/{ws_id}/runners"),
    ("PATCH", "/api/runners/{runner_id}"),
    ("DELETE", "/api/runners/{runner_id}"),
    ("GET", "/api/workspaces/{ws_id}/audit"),
    ("GET", "/api/workspaces/{ws_id}/people"),
    ("POST", "/api/workspaces/{ws_id}/people"),
    ("PATCH", "/api/workspaces/{ws_id}/people/{user_id}"),
    ("PUT", "/api/workspaces/{ws_id}/teams/{team_id}"),
    ("PUT", "/api/workspaces/{ws_id}/checkpoint-roles"),
    ("POST", "/api/workspaces/{ws_id}/people/{user_id}/reset-password"),
    ("DELETE", "/api/workspaces/{ws_id}/checkpoint-roles/{role_id}"),
}

# Writes that change a project's or repo's setup need more than contributor.
MAINTAINER = {
    ("PATCH", "/api/repos/{repo_id}"),
    ("DELETE", "/api/repos/{repo_id}"),
    ("PATCH", "/api/projects/{project_id}/access"),
    ("PUT", "/api/projects/{project_id}/members"),
}

# Workspace-level writes any member may make (handlers check the project inside the body).
MEMBER_WRITES = {
    ("POST", "/api/workspaces/{ws_id}/projects"),
    ("POST", "/api/workspaces/{ws_id}/repos"),
    ("POST", "/api/workspaces/{ws_id}/memory"),
    ("POST", "/api/workspaces/{ws_id}/ask"),
    ("POST", "/api/workspaces/{ws_id}/mcp/invoke"),
    ("POST", "/api/workspaces/{ws_id}/mcp/omni"),
    ("POST", "/api/workspaces/{ws_id}/knowledge/search"),
}

# Signed in is enough (the handler filters what comes back).
SIGNED_IN = {
    ("GET", "/api/workspaces"),
    ("GET", "/api/agents"),
    ("GET", "/api/readiness"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/password"),
    ("GET", "/api/auth/tokens"),
    ("POST", "/api/auth/tokens"),
    ("DELETE", "/api/auth/tokens/{token_id}"),
}

READ = {"GET", "HEAD", "OPTIONS"}

# All someone with a temporary password (like the first admin / admin) can do until they change it.
BEFORE_PASSWORD_CHANGE = {"/api/auth/me", "/api/auth/password"}


def rule(method: str, path: str) -> str:
    key = (method, path)
    if path in OPEN:
        return "open"
    if key in ADMIN:
        return "admin"
    if key in SIGNED_IN:
        return "signed_in"
    if key in MAINTAINER:
        return "maintainer"
    if key in MEMBER_WRITES or method in READ:
        return "viewer"
    return "contributor"


async def guard(request: Request, session: Session = Depends(get_session)):
    """Async on purpose: the principal it sets is then visible to sync handlers (FastAPI copies
    the context into the worker thread) and to agent runs they start."""
    authconfig.refresh(session)  # sign-in settings an admin may have changed in the web app
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    if not path.startswith("/api/"):  # the matched route can be the router's un-prefixed copy
        path = "/api" + path
    need = rule(request.method, path)
    if need == "open":
        token = access.set_current(access.SYSTEM)
        try:
            yield access.SYSTEM
        finally:
            access.reset_current(token)
        return
    p = auth.principal_for(request, session)
    if p is None:
        raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    if p.must_change_password and path not in BEFORE_PASSWORD_CHANGE:
        raise HTTPException(403, "password_change_required")
    if need != "signed_in" and not p.unrestricted:
        target = access.resolve(session, dict(request.path_params))
        if need == "admin" and target.workspace_id is None and not p.is_admin:
            raise HTTPException(403, "Only an admin can do this")
        if target.workspace_id is not None or target.project is not None:
            try:
                access.require(session, p, target, need)
            except HTTPException as exc:
                if exc.status_code == 403:
                    access.audit(session, "access.denied", workspace_id=target.workspace_id,
                                 resource=f"{request.method} {path}", p=p,
                                 params=dict(request.path_params))
                    session.commit()
                raise
        feature = features.feature_for(request.method, path)
        if feature and target.workspace_id \
                and not features.allowed(session, p, target.workspace_id, feature):
            access.audit(session, "access.denied", workspace_id=target.workspace_id,
                         resource=f"{request.method} {path}", p=p, feature=feature)
            session.commit()
            name = features.CATALOG[feature][0]
            raise HTTPException(403, f"{name} isn't turned on for you in this workspace; "
                                     "ask a workspace admin")
    token = access.set_current(p)
    try:
        yield p
    finally:
        access.reset_current(token)
