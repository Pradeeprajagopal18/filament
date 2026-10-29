"""Workspaces, teams, projects, artifacts, links, graph, impact and approvals."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from filament import schemas as s
from filament.context import neighbours
from filament.db import get_session
from filament.models import Approval, Artifact, Link, Project, Team, Workspace

router = APIRouter()


def get_or_404(session: Session, model, id_: str):
    obj = session.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {id_} not found")
    return obj


# -- workspaces & teams --------------------------------------------------------
@router.post("/workspaces", response_model=s.WorkspaceOut, status_code=201)
def create_workspace(body: s.WorkspaceIn, session: Session = Depends(get_session)):
    ws = Workspace(name=body.name)
    session.add(ws)
    session.commit()
    return ws


@router.get("/workspaces", response_model=list[s.WorkspaceOut])
def list_workspaces(session: Session = Depends(get_session)):
    return session.scalars(select(Workspace).order_by(Workspace.created_at)).all()


@router.post("/workspaces/{ws_id}/teams", response_model=s.TeamOut, status_code=201)
def create_team(ws_id: str, body: s.TeamIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    team = Team(workspace_id=ws_id, name=body.name)
    session.add(team)
    session.commit()
    return team


@router.get("/workspaces/{ws_id}/teams", response_model=list[s.TeamOut])
def list_teams(ws_id: str, session: Session = Depends(get_session)):
    return session.scalars(select(Team).where(Team.workspace_id == ws_id)).all()


# -- projects --------------------------------------------------------------------
@router.post("/workspaces/{ws_id}/projects", response_model=s.ProjectOut, status_code=201)
def create_project(ws_id: str, body: s.ProjectIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    project = Project(workspace_id=ws_id, **body.model_dump())
    session.add(project)
    session.commit()
    return project


@router.get("/workspaces/{ws_id}/projects", response_model=list[s.ProjectOut])
def list_projects(ws_id: str, session: Session = Depends(get_session)):
    stmt = select(Project).where(Project.workspace_id == ws_id).order_by(Project.created_at)
    return session.scalars(stmt).all()


@router.get("/projects/{project_id}", response_model=s.ProjectOut)
def get_project(project_id: str, session: Session = Depends(get_session)):
    return get_or_404(session, Project, project_id)


# -- artifacts ---------------------------------------------------------------------
@router.post("/projects/{project_id}/artifacts", response_model=s.ArtifactOut, status_code=201)
def create_artifact(project_id: str, body: s.ArtifactIn, session: Session = Depends(get_session)):
    get_or_404(session, Project, project_id)
    art = Artifact(project_id=project_id, **body.model_dump())
    session.add(art)
    session.commit()
    return art


@router.get("/projects/{project_id}/artifacts", response_model=list[s.ArtifactOut])
def list_artifacts(project_id: str, type: str | None = None,
                   session: Session = Depends(get_session)):
    stmt = select(Artifact).where(Artifact.project_id == project_id)
    if type:
        stmt = stmt.where(Artifact.type == type)
    return session.scalars(stmt.order_by(Artifact.created_at)).all()


@router.get("/artifacts/{artifact_id}", response_model=s.ArtifactOut)
def get_artifact(artifact_id: str, session: Session = Depends(get_session)):
    return get_or_404(session, Artifact, artifact_id)


@router.patch("/artifacts/{artifact_id}", response_model=s.ArtifactOut)
def update_artifact(artifact_id: str, body: s.ArtifactPatch,
                    session: Session = Depends(get_session)):
    art = get_or_404(session, Artifact, artifact_id)
    changes = body.model_dump(exclude_none=True)
    if {"title", "content", "data"} & changes.keys():
        art.version += 1
    for key, value in changes.items():
        setattr(art, key, value)
    session.commit()
    return art


# -- links, graph, impact ------------------------------------------------------------
@router.post("/projects/{project_id}/links", response_model=s.LinkOut, status_code=201)
def create_link(project_id: str, body: s.LinkIn, session: Session = Depends(get_session)):
    for aid in (body.source_id, body.target_id):
        get_or_404(session, Artifact, aid)
    link = Link(project_id=project_id, **body.model_dump())
    session.add(link)
    session.commit()
    return link


@router.get("/projects/{project_id}/graph", response_model=s.GraphOut)
def project_graph(project_id: str, session: Session = Depends(get_session)):
    nodes = session.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()
    edges = session.scalars(select(Link).where(Link.project_id == project_id)).all()
    return {"nodes": nodes, "edges": edges}


@router.get("/artifacts/{artifact_id}/impact", response_model=s.ImpactOut)
def impact(artifact_id: str, hops: int = 3, session: Session = Depends(get_session)):
    """Everything linked to this artifact within `hops` edges: what a change could affect."""
    get_or_404(session, Artifact, artifact_id)
    ids = neighbours(session, [artifact_id], hops=hops)
    affected = session.scalars(select(Artifact).where(Artifact.id.in_(ids))).all() if ids else []
    return {"artifact_id": artifact_id, "affected": affected}


# -- approvals -------------------------------------------------------------------------
@router.get("/projects/{project_id}/approvals", response_model=list[s.ApprovalOut])
def list_approvals(project_id: str, status: str = "pending",
                   session: Session = Depends(get_session)):
    stmt = (select(Approval).join(Artifact, Approval.artifact_id == Artifact.id)
            .where(Artifact.project_id == project_id, Approval.status == status))
    return session.scalars(stmt).all()


@router.post("/artifacts/{artifact_id}/approve", response_model=s.ArtifactOut)
def decide(artifact_id: str, body: s.ApprovalDecision, session: Session = Depends(get_session)):
    art = get_or_404(session, Artifact, artifact_id)
    pending = session.scalars(select(Approval).where(
        Approval.artifact_id == artifact_id, Approval.status == "pending")).all()
    if not pending:
        pending = [Approval(artifact_id=artifact_id)]
        session.add(pending[0])
    for ap in pending:
        ap.status = body.decision
        ap.decided_by = body.decided_by
        ap.comment = body.comment
        ap.decided_at = datetime.now(UTC)
    art.status = "approved" if body.decision == "approved" else "rejected"
    session.commit()
    return art


@router.get("/artifacts/{artifact_id}/links", response_model=list[s.LinkOut])
def artifact_links(artifact_id: str, session: Session = Depends(get_session)):
    stmt = select(Link).where(or_(Link.source_id == artifact_id, Link.target_id == artifact_id))
    return session.scalars(stmt).all()
