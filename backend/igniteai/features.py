"""Which areas of the app each person may use, set by workspace admins.

Each feature covers a set of API routes (so turning it off is enforced by the API, not just
hidden in the menu) and the matching screens. A feature is on for everyone (the default), on for
some people (chosen teams, directory or SSO groups, and individuals), or off. Workspace admins
always have every feature. What a feature shows still follows project and repo access: turning
a feature on never reveals a restricted project.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from igniteai.models import FeatureAccess, Membership, User

# key -> (name, what it covers, [(methods or "*", route template)])
CATALOG: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "launchpad": ("Launchpad", "Onboard repos, scan them, readiness checks, references", [
        ("*", "/api/workspaces/{ws_id}/repos"), ("*", "/api/repos/{repo_id}"),
        ("*", "/api/repos/{repo_id}/rescan"), ("*", "/api/repos/{repo_id}/scans"),
        ("*", "/api/repos/{repo_id}/generate"), ("*", "/api/repos/{repo_id}/files"),
        ("*", "/api/repos/{repo_id}/context"), ("*", "/api/repos/{repo_id}/enrich-api"),
        ("*", "/api/repos/{repo_id}/references/{item}/accept"),
        ("*", "/api/readiness"), ("*", "/api/readiness/scan")]),
    "system_map": ("System map", "How repos connect, and who depends on a repo", [
        ("*", "/api/workspaces/{ws_id}/system-map"), ("*", "/api/repos/{repo_id}/impact")]),
    "api_docs": ("API docs and mocks", "API contracts and the mock server", [
        ("*", "/api/repos/{repo_id}/openapi"), ("*", "/api/repos/{repo_id}/mock/{path:path}")]),
    "code_intel": ("Code graph", "Explain, path, query, change impact, data model", [
        ("*", "/api/repos/{repo_id}/code"), ("*", "/api/repos/{repo_id}/code/graph"),
        ("*", "/api/repos/{repo_id}/code/explain"), ("*", "/api/repos/{repo_id}/code/path"),
        ("*", "/api/repos/{repo_id}/code/query"), ("*", "/api/repos/{repo_id}/code/changes"),
        ("*", "/api/repos/{repo_id}/code/conflicts"), ("*", "/api/repos/{repo_id}/data-model")]),
    "delivery": ("Delivery pipeline", "Work items, hand-offs, PRs, releases, SLOs, incidents", [
        ("*", "/api/projects/{project_id}/delivery"), ("*", "/api/projects/{project_id}/issues"),
        ("*", "/api/projects/{project_id}/issues/import"),
        ("*", "/api/work-items/{issue_id}/advance"),
        ("*", "/api/tasks/{task_id}/handoff"), ("*", "/api/tasks/{task_id}/pull-request"),
        ("*", "/api/projects/{project_id}/slos"), ("*", "/api/slos/{slo_id}/samples"),
        ("*", "/api/projects/{project_id}/slo-check"), ("*", "/api/releases/{release_id}/{action}"),
        ("*", "/api/incidents/{incident_id}/resolve")]),
    "agents": ("Run agents", "Start agent runs (everyone can still read their results)", [
        ("POST", "/api/projects/{project_id}/runs")]),
    "ask": ("Ask", "Questions answered from the project graph and memory; knowledge gaps", [
        ("*", "/api/workspaces/{ws_id}/ask"), ("*", "/api/workspaces/{ws_id}/gaps")]),
    "memory": ("Team memory", "Read, add, confirm and retire shared memory", [
        ("*", "/api/workspaces/{ws_id}/memory"), ("*", "/api/memory/{entry_id}/{action}")]),
    "mcp_tools": ("MCP tools", "Browse and call gateway tools, and the Omni MCP endpoint", [
        ("*", "/api/workspaces/{ws_id}/mcp/registry"),
        ("*", "/api/workspaces/{ws_id}/mcp/discover"),
        ("*", "/api/workspaces/{ws_id}/mcp/schema"), ("*", "/api/workspaces/{ws_id}/mcp/invoke"),
        ("*", "/api/workspaces/{ws_id}/mcp/omni"), ("*", "/api/projects/{project_id}/mcp-tools")]),
}

_ROUTES: dict[str, list[tuple[str, str]]] = {}
for _key, (_n, _d, _routes) in CATALOG.items():
    for _m, _p in _routes:
        _ROUTES.setdefault(_p, []).append((_m, _key))


def feature_for(method: str, path: str) -> str | None:
    for m, key in _ROUTES.get(path, []):
        if m == "*" or m == method:
            return key
    return None


def _policy(session: Session, ws_id: str, feature: str) -> FeatureAccess | None:
    return session.scalar(select(FeatureAccess).where(FeatureAccess.workspace_id == ws_id,
                                                      FeatureAccess.feature == feature))


def _allowed_by(row: FeatureAccess | None, user: User | None, team_ids: set[str]) -> bool:
    if row is None or row.mode == "everyone":
        return True
    if row.mode == "off" or user is None:
        return False
    groups = {g.lower() for g in (user.groups or [])}
    return (user.id in (row.users or [])
            or bool(team_ids & set(row.teams or []))
            or bool(groups & {g.lower() for g in (row.groups or [])}))


def allowed(session: Session, p, ws_id: str, feature: str) -> bool:
    from igniteai import access

    if p.unrestricted or access.is_ws_admin(session, p, ws_id):
        return True
    key = ("f", ws_id, feature)
    if key not in p._cache:
        user = session.get(User, p.user_id) if p.user_id else None
        teams = {m.scope_id for m in access._memberships(session, p) if m.scope == "team"}
        p._cache[key] = _allowed_by(_policy(session, ws_id, feature), user, teams)
    return p._cache[key]


def allowed_set(session: Session, p, ws_id: str) -> list[str]:
    return [k for k in CATALOG if allowed(session, p, ws_id, k)]


def describe(session: Session, ws_id: str) -> list[dict]:
    rows = {r.feature: r for r in session.scalars(select(FeatureAccess).where(
        FeatureAccess.workspace_id == ws_id))}
    out = []
    for key, (name, desc, _) in CATALOG.items():
        r = rows.get(key)
        out.append({"key": key, "name": name, "description": desc,
                    "mode": r.mode if r else "everyone", "teams": r.teams if r else [],
                    "groups": r.groups if r else [], "users": r.users if r else [],
                    "updated_by": r.updated_by if r else None})
    return out


def who_has(session: Session, ws_id: str, feature: str) -> int:
    """How many active non-admin people the policy lets in (for the admin page)."""
    row = _policy(session, ws_id, feature)
    n = 0
    for u in session.scalars(select(User).where(User.active.is_(True), User.is_admin.is_(False))):
        teams = {m.scope_id for m in session.scalars(select(Membership).where(
            Membership.user_id == u.id, Membership.scope == "team"))}
        n += _allowed_by(row, u, teams)
    return n
