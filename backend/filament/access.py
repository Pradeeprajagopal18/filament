"""Who can see and do what.

Everything hangs off a Principal (the person behind a request) and a few rules:

* Workspaces: every signed-in person is a member of every workspace (FILAMENT_WORKSPACE_DEFAULT_ROLE
  = member) unless that is set to "none", in which case only people added explicitly are.
  Admins (User.is_admin, or an admin membership on the workspace) can do everything there.
* Projects: open projects give every workspace member the contributor role. A restricted project
  is visible only to its members: people with a project membership, members of the project's
  team (synced from an SSO group), and admins. Roles: viewer < contributor < maintainer < owner.
* Repositories follow their project, and private repos additionally mirror the git host: you see
  one if GitHub says you can read it (your GitHub login, checked with the org token, cached),
  or, when GitHub can't tell, if you hold an explicit role on its project.
* Things you cannot see answer 404, never 403, so their existence doesn't leak.

With FILAMENT_AUTH_MODE=none (a laptop), the principal is a local admin and nothing is filtered.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from filament import gitaccess
from filament.config import get_settings
from filament.models import (
    Artifact,
    AuditEvent,
    CheckpointRole,
    McpServer,
    McpToolEntry,
    Membership,
    MemoryEntry,
    Project,
    RepoRecord,
    Run,
    Runner,
    User,
    Workspace,
)

log = logging.getLogger(__name__)

PROJECT_ROLES = ["viewer", "contributor", "maintainer", "owner"]
RANK = {r: i for i, r in enumerate(PROJECT_ROLES)}
WORKSPACE_ROLES = ["member", "admin"]
TEAM_ROLES = ["viewer", "contributor", "maintainer"]


@dataclass
class Principal:
    user_id: str | None
    email: str
    name: str = ""
    is_admin: bool = False
    github_login: str | None = None
    via: str = "session"  # session | token | local | system
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return self.email or self.name or "local"

    @property
    def unrestricted(self) -> bool:
        return self.is_admin and self.user_id is None  # local mode or system jobs


LOCAL = Principal(user_id=None, email="local", name="Local admin", is_admin=True, via="local")
SYSTEM = Principal(user_id=None, email="system", name="Filament", is_admin=True, via="system")

_current: contextvars.ContextVar[Principal] = contextvars.ContextVar("principal", default=SYSTEM)


def current() -> Principal:
    """The person the current request (or agent run) acts for."""
    return _current.get()


def set_current(p: Principal):
    return _current.set(p)


def reset_current(token) -> None:
    _current.reset(token)


def from_user(user: User, via: str = "session") -> Principal:
    return Principal(user_id=user.id, email=user.email, name=user.name, is_admin=user.is_admin,
                     github_login=user.github_login, via=via)


def for_requester(session: Session, requested_by: str | None) -> Principal:
    """The principal an agent run acts as: the user who asked for it, else the system."""
    if requested_by and requested_by.startswith("user:"):
        user = session.get(User, requested_by[5:])
        if user and user.active:
            return from_user(user, via="run")
    return SYSTEM


# -- roles ----------------------------------------------------------------------------------------
def _memberships(session: Session, p: Principal) -> list[Membership]:
    if "m" not in p._cache:
        p._cache["m"] = list(session.scalars(select(Membership).where(
            Membership.user_id == p.user_id))) if p.user_id else []
    return p._cache["m"]


def workspace_role(session: Session, p: Principal, ws_id: str) -> str | None:
    if p.is_admin:
        return "admin"
    explicit = [m.role for m in _memberships(session, p)
                if m.scope == "workspace" and m.scope_id == ws_id]
    if "admin" in explicit:
        return "admin"
    if explicit:
        return "member"
    return "member" if get_settings().workspace_default_role == "member" else None


def is_ws_admin(session: Session, p: Principal, ws_id: str) -> bool:
    return workspace_role(session, p, ws_id) == "admin"


def explicit_project_role(session: Session, p: Principal, project: Project) -> str | None:
    """A role given on purpose: on the project itself or through its team."""
    roles = [m.role for m in _memberships(session, p)
             if (m.scope == "project" and m.scope_id == project.id)
             or (m.scope == "team" and project.team_id and m.scope_id == project.team_id)]
    return max(roles, key=lambda r: RANK.get(r, -1)) if roles else None


def project_role(session: Session, p: Principal, project: Project) -> str | None:
    key = ("p", project.id)
    if key in p._cache:
        return p._cache[key]
    ws = workspace_role(session, p, project.workspace_id)
    if ws == "admin":
        role = "owner"
    elif ws is None:
        role = None
    else:
        role = explicit_project_role(session, p, project)
        if role is None and not project.restricted:
            role = "contributor"
    p._cache[key] = role
    return role


def can(session: Session, p: Principal, project: Project, need: str = "viewer") -> bool:
    role = project_role(session, p, project)
    return role is not None and RANK[role] >= RANK[need]


# -- repos ----------------------------------------------------------------------------------------
_gh_cache: dict[tuple[str, str], tuple[float, bool | None]] = {}
GH_TTL = 900.0


def _github_can_read(ref: str, login: str) -> bool | None:
    """True/False from GitHub's collaborator permission API; None when GitHub can't tell us."""
    from filament.delivery import github

    key = (ref, login.lower())
    hit = _gh_cache.get(key)
    if hit and time.monotonic() - hit[0] < GH_TTL:
        return hit[1]
    answer: bool | None = None
    try:
        host, owner, repo = github.parse_repo(ref)
        data = github._get(host, f"/repos/{owner}/{repo}/collaborators/{login}/permission")
        answer = (data.get("permission") or "none") != "none"
    except github.GitHubError as exc:
        # 404 here means the login isn't a collaborator (or the token can't see the repo).
        answer = False if "Not found" in str(exc) and gitaccess.token() else None
    except Exception:  # noqa: BLE001 - never let a lookup failure open access
        answer = None
    _gh_cache[key] = (time.monotonic(), answer)
    return answer


def repo_visibility(ref: str) -> bool | None:
    """Is a repo private on its host? None for local paths or when the host can't say."""
    from filament.delivery import github

    if "://" not in ref and not ref.startswith("git@"):
        return None
    try:
        host, owner, repo = github.parse_repo(ref)
        data = github._get(host, f"/repos/{owner}/{repo}")
        return bool(data.get("private")) or data.get("visibility") in {"private", "internal"}
    except Exception:  # noqa: BLE001
        # Unknown, but it's on our GitHub: if we needed the token to reach it, assume private.
        try:
            return True if gitaccess.host_of(ref) in gitaccess.hosts() and gitaccess.token() \
                else None
        except Exception:  # noqa: BLE001
            return None


def can_see_repo(session: Session, p: Principal, repo: RepoRecord,
                 project: Project | None = None) -> bool:
    key = ("r", repo.id)
    if key in p._cache:
        return p._cache[key]
    project = project or session.get(Project, repo.project_id)
    ok = project is not None and can(session, p, project)
    if ok and repo.private and not is_ws_admin(session, p, repo.workspace_id):
        gh = _github_can_read(repo.ref, p.github_login) if p.github_login else None
        ok = gh if gh is not None else explicit_project_role(session, p, project) is not None
    p._cache[key] = ok
    return ok


# -- resolving request paths ----------------------------------------------------------------------
@dataclass
class Target:
    workspace_id: str | None = None
    project: Project | None = None
    repo: RepoRecord | None = None
    scope_ok: bool = True  # e.g. a memory entry scoped to a team/project you can't see


def _nf(what: str, id_: str) -> HTTPException:
    return HTTPException(404, f"{what} {id_} not found")


def resolve(session: Session, params: dict) -> Target:
    """Find the workspace/project/repo a request path points at (404 for unknown ids)."""
    t = Target()

    def project(pid: str | None):
        if pid and t.project is None:
            t.project = session.get(Project, pid)
            if t.project is None:
                raise _nf("Project", pid)
            t.workspace_id = t.project.workspace_id

    if ws := params.get("ws_id"):
        if session.get(Workspace, ws) is None:
            raise _nf("Workspace", ws)
        t.workspace_id = ws
    if pid := params.get("project_id"):
        project(pid)
    for key in ("artifact_id", "issue_id", "task_id", "incident_id", "release_id", "slo_id"):
        if aid := params.get(key):
            art = session.get(Artifact, aid)
            if art is None:
                raise _nf("Artifact", aid)
            project(art.project_id)
    if rid := params.get("repo_id"):
        t.repo = session.get(RepoRecord, rid)
        if t.repo is None:
            raise _nf("Repo", rid)
        project(t.repo.project_id)
    if run_id := params.get("run_id"):
        run = session.get(Run, run_id)
        if run is None:
            raise _nf("Run", run_id)
        project(run.project_id)
    if eid := params.get("entry_id"):
        entry = session.get(MemoryEntry, eid)
        if entry is None:
            raise _nf("MemoryEntry", eid)
        t.workspace_id = entry.workspace_id
        if entry.scope == "project" and entry.scope_ref:
            project(entry.scope_ref)
    if sid := params.get("server_id"):
        srv = session.get(McpServer, sid)
        if srv is None:
            raise _nf("McpServer", sid)
        t.workspace_id = srv.workspace_id
    if tid := params.get("tool_id"):
        tool = session.get(McpToolEntry, tid)
        srv = session.get(McpServer, tool.server_id) if tool else None
        if srv is None:
            raise _nf("McpTool", tid)
        t.workspace_id = srv.workspace_id
    if rn := params.get("runner_id"):
        runner = session.get(Runner, rn)
        if runner is None:
            raise _nf("Runner", rn)
        t.workspace_id = runner.workspace_id
    return t


def require(session: Session, p: Principal, t: Target, need: str) -> None:
    """need: viewer | contributor | maintainer | owner | member | admin."""
    if p.unrestricted:
        return
    hidden = HTTPException(404, "Not found")
    if t.workspace_id and workspace_role(session, p, t.workspace_id) is None:
        raise hidden
    if need == "admin":
        if not t.workspace_id or not is_ws_admin(session, p, t.workspace_id):
            raise HTTPException(403, "Only a workspace admin can do this")
        return
    if t.project is not None:
        if not can(session, p, t.project, "viewer"):
            raise hidden
        if t.repo is not None and not can_see_repo(session, p, t.repo, t.project):
            raise hidden
        if need in RANK and not can(session, p, t.project, need):
            raise HTTPException(403, f"You need the {need} role on this project")


# -- list filters ---------------------------------------------------------------------------------
def visible_projects(session: Session, p: Principal, projects):
    return [x for x in projects if p.unrestricted or can(session, p, x)]


def visible_repos(session: Session, p: Principal, repos):
    return [r for r in repos if p.unrestricted or can_see_repo(session, p, r)]


def visible_repo_ids(session: Session, p: Principal, ws_id: str) -> set[str] | None:
    """None means "everything"."""
    if p.unrestricted:
        return None
    repos = session.scalars(select(RepoRecord).where(RepoRecord.workspace_id == ws_id))
    return {r.id for r in repos if can_see_repo(session, p, r)}


def can_see_memory(session: Session, p: Principal, entry: MemoryEntry) -> bool:
    if p.unrestricted or is_ws_admin(session, p, entry.workspace_id):
        return True
    if entry.scope == "project" and entry.scope_ref:
        proj = session.get(Project, entry.scope_ref)
        return proj is not None and can(session, p, proj)
    if entry.scope == "team" and entry.scope_ref:
        return any(m.scope == "team" and m.scope_id == entry.scope_ref
                   for m in _memberships(session, p))
    if entry.scope == "run" and entry.scope_ref:
        run = session.get(Run, entry.scope_ref)
        proj = session.get(Project, run.project_id) if run else None
        return proj is not None and can(session, p, proj)
    return True  # org-wide


def actor(fallback: str | None = None) -> str:
    """Who to record as having done something: the signed-in person, else what the client said."""
    p = current()
    return p.email if p.user_id else (fallback or p.label)


def requester(fallback: str | None = None) -> str:
    p = current()
    return f"user:{p.user_id}" if p.user_id else (fallback or "human")


def filter_repos(session: Session, repos):
    """Repos the current principal may see (agents act as the person who started them)."""
    return visible_repos(session, current(), list(repos))


def filter_memory(session: Session, entries):
    p = current()
    return [e for e in entries if p.unrestricted or can_see_memory(session, p, e)]


def check_sign_off(session: Session, project: Project, role: str | None) -> None:
    if not can_sign_off(session, current(), project, role):
        label = (role or "maintainer").replace("_", " ")
        raise HTTPException(403, f"This checkpoint needs the {label} (assigned under People & "
                                 f"access), or a project maintainer when nobody holds that role")


# -- checkpoints ----------------------------------------------------------------------------------
def holders(session: Session, project: Project, role: str) -> list[str]:
    rows = session.scalars(select(CheckpointRole).where(
        CheckpointRole.workspace_id == project.workspace_id, CheckpointRole.role == role))
    return [r.user_id for r in rows if r.project_id in (None, project.id)]


def can_sign_off(session: Session, p: Principal, project: Project, role: str | None) -> bool:
    """A checkpoint needs its role (product owner, tech lead, ...). When nobody holds that role
    on the project, a project owner or maintainer may sign off instead."""
    if p.unrestricted or is_ws_admin(session, p, project.workspace_id):
        return True
    if not can(session, p, project, "contributor"):
        return False
    if not role:
        return can(session, p, project, "maintainer")
    who = holders(session, project, role)
    if who:
        return p.user_id in who
    return can(session, p, project, "maintainer")


# -- audit ----------------------------------------------------------------------------------------
def audit(session: Session, action: str, *, workspace_id: str | None = None,
          resource: str = "", p: Principal | None = None, **detail) -> None:
    p = p or current()
    session.add(AuditEvent(user_id=p.user_id, actor=p.label, action=action,
                           workspace_id=workspace_id, resource=resource, detail=detail))
