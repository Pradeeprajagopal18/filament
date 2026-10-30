"""Launchpad repos: onboard, analyze, browse API / UI / components, generate missing items."""

from datetime import UTC, datetime

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from filament import schemas as s
from filament.api.graph import get_or_404
from filament.db import get_session
from filament.models import Artifact, MemoryEntry, Project, ReadinessReport, RepoRecord, Workspace
from filament.repos import generate as gen
from filament.repos.analyze import analyze

router = APIRouter()
AUTHOR = "agent:repo-onboarding"


def _store(session: Session, record: RepoRecord, analysis: dict) -> None:
    record.analysis = analysis
    record.name = analysis["name"]
    record.kinds = analysis["profile"]["kind_names"]
    record.score = analysis["readiness"]["score"]
    record.grade = analysis["readiness"]["grade"]
    record.scanned_at = datetime.now(UTC)
    session.add(ReadinessReport(repo=record.ref, project_id=record.project_id,
                                score=record.score, grade=record.grade,
                                checks=analysis["readiness"]["checks"]))


def _run(ref: str) -> dict:
    try:
        return analyze(ref)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/workspaces/{ws_id}/repos", response_model=s.RepoDetail, status_code=201)
def onboard_repo(ws_id: str, body: s.RepoIn, session: Session = Depends(get_session)):
    """Analyze a repo and keep it in the workspace (re-onboarding the same ref rescans it)."""
    get_or_404(session, Workspace, ws_id)
    analysis = _run(body.repo)
    record = session.scalars(select(RepoRecord).where(RepoRecord.workspace_id == ws_id,
                                                      RepoRecord.ref == body.repo)).first()
    if record is None:
        project_id = body.project_id
        if project_id:
            get_or_404(session, Project, project_id)
        else:
            project = Project(workspace_id=ws_id, name=analysis["name"],
                              description=f"Repository {body.repo} "
                                          f"({', '.join(analysis['profile']['kind_names'])})")
            session.add(project)
            session.flush()
            project_id = project.id
        record = RepoRecord(workspace_id=ws_id, project_id=project_id, ref=body.repo,
                            name=analysis["name"])
        session.add(record)
    _store(session, record, analysis)
    session.commit()
    return record


@router.get("/workspaces/{ws_id}/repos", response_model=list[s.RepoSummary])
def list_repos(ws_id: str, session: Session = Depends(get_session)):
    return session.scalars(select(RepoRecord).where(RepoRecord.workspace_id == ws_id)
                           .order_by(RepoRecord.name)).all()


@router.get("/repos/{repo_id}", response_model=s.RepoDetail)
def get_repo(repo_id: str, session: Session = Depends(get_session)):
    return get_or_404(session, RepoRecord, repo_id)


@router.post("/repos/{repo_id}/rescan", response_model=s.RepoDetail)
def rescan_repo(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    _store(session, record, _run(record.ref))
    session.commit()
    return record


@router.delete("/repos/{repo_id}", status_code=204)
def remove_repo(repo_id: str, session: Session = Depends(get_session)):
    session.delete(get_or_404(session, RepoRecord, repo_id))
    session.commit()


@router.post("/repos/{repo_id}/generate", response_model=s.RepoDetail)
def generate_missing(repo_id: str, body: s.GenerateIn, session: Session = Depends(get_session)):
    """Create missing readiness items as draft artifacts in Filament (not in the repo).

    With no items, every generator behind a failing check runs. Running a generator again
    replaces its draft with a new version instead of creating a duplicate.
    """
    record = get_or_404(session, RepoRecord, repo_id)
    a = record.analysis
    failing = [c["generate"] for c in a["readiness"]["checks"]
               if c["status"] == "fail" and c.get("generate")]
    items = body.items or failing
    unknown = [i for i in items if i not in gen.GENERATORS]
    if unknown:
        raise HTTPException(422, f"Unknown generators {unknown}; known: {sorted(gen.GENERATORS)}")
    generated = dict(record.generated or {})
    created_ids = []
    for item in items:
        built = gen.build(item, a)
        if built is None:
            continue
        title, content, data = built
        data = {**data, "repo": record.ref, "generator": item, "in_repo": False}
        art_type = "api_contract" if item == "api_contract" else "doc"
        existing = session.get(Artifact, generated[item]) if item in generated else None
        if existing:
            existing.title, existing.content, existing.data = title, content, data
            existing.version += 1
            existing.status = "draft"
            created_ids.append(existing.id)
        else:
            art = Artifact(project_id=record.project_id, type=art_type, title=title,
                           content=content, data=data, status="draft", author=AUTHOR)
            session.add(art)
            session.flush()
            generated[item] = art.id
            created_ids.append(art.id)
    # Conventions become proposed project memory, citing the generated drafts.
    known = {m.content for m in session.scalars(select(MemoryEntry).where(
        MemoryEntry.scope == "project", MemoryEntry.scope_ref == record.project_id))}
    for fact in gen.conventions(a):
        if fact not in known:
            session.add(MemoryEntry(workspace_id=record.workspace_id, scope="project",
                                    scope_ref=record.project_id, kind="fact", content=fact,
                                    status="proposed", author=AUTHOR,
                                    source_artifact_ids=created_ids[:3], confidence=0.8))
    record.generated = generated
    session.commit()
    return record


@router.get("/repos/{repo_id}/openapi", response_class=PlainTextResponse)
def repo_openapi(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    spec = record.analysis.get("api", {}).get("openapi")
    if not spec:
        raise HTTPException(404, "No API endpoints found in this repo")
    return yaml.safe_dump(spec, sort_keys=False)


@router.get("/repos/{repo_id}/export")
def repo_export(repo_id: str, session: Session = Depends(get_session)):
    """The files generated drafts would become in the repo (for a pull request or download)."""
    record = get_or_404(session, RepoRecord, repo_id)
    arts = {g: session.get(Artifact, aid) for g, aid in (record.generated or {}).items()}
    return gen.export_files({g: {"content": a.content, "data": a.data}
                             for g, a in arts.items() if a})
