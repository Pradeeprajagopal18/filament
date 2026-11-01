"""Launchpad repos: onboard, analyze, browse API / UI / components, system map, references,
scheduled rescans, webhooks and the API simulator."""

import hashlib
import hmac
import json
import re

import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from filament import schemas as s
from filament.api.graph import get_or_404
from filament.config import get_settings
from filament.db import get_session
from filament.models import Artifact, Project, RepoRecord, RepoScan, Workspace
from filament.repos import context as repo_context
from filament.repos import generate as gen
from filament.repos import service
from filament.repos.analyze import analyze
from filament.repos.system import impact

router = APIRouter()


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
    trigger = "manual"
    if record is None:
        trigger = "onboard"
        project_id = body.project_id
        if project_id:
            get_or_404(session, Project, project_id)
        else:
            project_id = service.project_for(session, ws_id, analysis["name"], body.repo,
                                             analysis["profile"]["kind_names"])
        record = RepoRecord(workspace_id=ws_id, project_id=project_id, ref=body.repo,
                            name=analysis["name"], scan_interval_hours=body.scan_interval_hours)
        session.add(record)
        session.flush()
    service.store(session, record, analysis, trigger)
    session.commit()
    return record


@router.get("/workspaces/{ws_id}/repos", response_model=list[s.RepoSummary])
def list_repos(ws_id: str, session: Session = Depends(get_session)):
    return session.scalars(select(RepoRecord).where(RepoRecord.workspace_id == ws_id)
                           .order_by(RepoRecord.name)).all()


@router.get("/workspaces/{ws_id}/system-map")
def system_map(ws_id: str, session: Session = Depends(get_session)):
    """How the workspace's repos connect: calls (matched to endpoints), configuration,
    provisioning and shared external services."""
    get_or_404(session, Workspace, ws_id)
    m = service.system_for(session, ws_id)
    return {k: v for k, v in m.items() if k != "consumers"}


@router.get("/repos/{repo_id}", response_model=s.RepoDetail)
def get_repo(repo_id: str, session: Session = Depends(get_session)):
    return get_or_404(session, RepoRecord, repo_id)


@router.patch("/repos/{repo_id}", response_model=s.RepoDetail)
def update_repo(repo_id: str, body: s.RepoPatch, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    if body.scan_interval_hours is not None:
        record.scan_interval_hours = max(0, body.scan_interval_hours)
        service.schedule_next(record)
    session.commit()
    return record


@router.post("/repos/{repo_id}/rescan", response_model=s.RepoDetail)
def rescan_repo(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    scan = service.scan_record(session, record, "manual")
    session.commit()
    if scan.status == "failed":
        raise HTTPException(400, scan.error or "Scan failed")
    return record


@router.get("/repos/{repo_id}/scans", response_model=list[s.RepoScanOut])
def repo_scans(repo_id: str, limit: int = 30, session: Session = Depends(get_session)):
    get_or_404(session, RepoRecord, repo_id)
    return session.scalars(select(RepoScan).where(RepoScan.repo_id == repo_id)
                           .order_by(RepoScan.scanned_at.desc()).limit(min(limit, 200))).all()


@router.post("/repos/scan-due")
def scan_due(limit: int = 5, session: Session = Depends(get_session)):
    """Rescan repos whose interval has elapsed (the scheduler does this; also for cron)."""
    scans = service.scan_due(session, limit)
    return {"scanned": [{"repo_id": x.repo_id, "status": x.status,
                         "summary": (x.changes or {}).get("summary")} for x in scans]}


@router.delete("/repos/{repo_id}", status_code=204)
def remove_repo(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    for scan in session.scalars(select(RepoScan).where(RepoScan.repo_id == repo_id)):
        session.delete(scan)
    session.delete(record)
    session.commit()


@router.get("/repos/{repo_id}/impact")
def repo_impact(repo_id: str, session: Session = Depends(get_session)):
    """Who depends on this repo (and through which endpoints), and what it depends on."""
    record = get_or_404(session, RepoRecord, repo_id)
    return impact(service.system_for(session, record.workspace_id), repo_id)


@router.get("/repos/{repo_id}/context")
def repo_context_view(repo_id: str, query: str = "", session: Session = Depends(get_session)):
    """The compact summary agents receive for this repo."""
    record = get_or_404(session, RepoRecord, repo_id)
    summary = repo_context.summary(record, service.system_for(session, record.workspace_id),
                                   query=query)
    return {"summary": summary, "text": repo_context.render(summary)}


# -- references Filament keeps about the repo ----------------------------------------------------
@router.post("/repos/{repo_id}/generate", response_model=s.RepoDetail)
def generate_missing(repo_id: str, body: s.GenerateIn, session: Session = Depends(get_session)):
    """Create missing readiness items as references in Filament (never written to the repo).

    With no items, every generator behind a failing check runs. Running a generator again
    replaces its draft with a new version instead of creating a duplicate.
    """
    record = get_or_404(session, RepoRecord, repo_id)
    failing = [c["generate"] for c in record.analysis["readiness"]["checks"]
               if c["status"] == "fail" and c.get("generate")]
    items = body.items or list(dict.fromkeys(failing))
    unknown = [i for i in items if i not in gen.GENERATORS]
    if unknown:
        raise HTTPException(422, f"Unknown generators {unknown}; known: {sorted(gen.GENERATORS)}")
    service.generate(session, record, items)
    session.commit()
    return record


@router.post("/repos/{repo_id}/references/{item}/accept", response_model=s.ArtifactOut)
def accept_reference_update(repo_id: str, item: str, session: Session = Depends(get_session)):
    """Apply the update a rescan prepared for an approved reference (it goes back to review)."""
    record = get_or_404(session, RepoRecord, repo_id)
    try:
        art = service.accept_update(session, record, item)
    except KeyError as exc:
        raise HTTPException(404, f"No reference {item} for this repo") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return art


@router.post("/repos/{repo_id}/enrich-api", response_model=s.RepoDetail)
def enrich_api(repo_id: str, session: Session = Depends(get_session)):
    """Optional model pass: summaries, descriptions and extra error codes for each endpoint."""
    record = get_or_404(session, RepoRecord, repo_id)
    try:
        service.enrich_api(session, record)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return record


@router.get("/repos/{repo_id}/openapi", response_class=PlainTextResponse)
def repo_openapi(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    spec = (record.analysis.get("api") or {}).get("openapi")
    if not spec:
        raise HTTPException(404, "No API endpoints found in this repo")
    return yaml.safe_dump(spec, sort_keys=False)


@router.get("/repos/{repo_id}/files")
def repo_reference_files(repo_id: str, session: Session = Depends(get_session)):
    """The references as files (for download or to hand to the repo's owners)."""
    record = get_or_404(session, RepoRecord, repo_id)
    arts = {g: session.get(Artifact, aid) for g, aid in (record.generated or {}).items()}
    return gen.export_files({g: {"content": a.content, "data": a.data}
                             for g, a in arts.items() if a})


# -- API simulator --------------------------------------------------------------------------------
@router.api_route("/repos/{repo_id}/mock/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def mock_endpoint(repo_id: str, path: str, request: Request,
                  session: Session = Depends(get_session)):
    """Answer like the repo's API would, using the contract's example responses."""
    record = get_or_404(session, RepoRecord, repo_id)
    target = "/" + path.strip("/")
    for ep in (record.analysis.get("api") or {}).get("endpoints") or []:
        if ep["method"] not in {request.method, "ANY"}:
            continue
        escaped = re.escape(ep["path"].rstrip("/") or "/")
        pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", escaped) + "/?$"
        if re.match(pattern, target):
            ok = next((c for c in sorted(ep.get("responses") or {}) if c.startswith("2")), "200")
            body = ep.get("example_response")
            if ok == "204":
                return JSONResponse(None, status_code=204)
            return JSONResponse(body if body is not None else {"ok": True}, status_code=int(ok),
                                headers={"X-Filament-Mock": f"{ep['method']} {ep['path']}"})
    raise HTTPException(404, f"No {request.method} {target} in {record.name}'s contract")


# -- webhooks -------------------------------------------------------------------------------------
@router.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request, background: BackgroundTasks,
                         session: Session = Depends(get_session)):
    """Rescan onboarded repos when GitHub reports a push to their default branch."""
    raw = await request.body()
    secret = get_settings().github_webhook_secret
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(401, "Bad signature")
    event = request.headers.get("X-GitHub-Event", "push")
    if event == "ping":
        return {"ok": True}
    if event != "push":
        return {"ignored": event}
    payload = json.loads(raw or b"{}")
    repo = payload.get("repository") or {}
    full = (repo.get("full_name") or "").lower()
    default = repo.get("default_branch")
    if default and payload.get("ref") and payload["ref"] != f"refs/heads/{default}":
        return {"ignored": "not the default branch"}
    ids = [r.id for r in session.scalars(select(RepoRecord)).all()
           if full and full in r.ref.lower().removesuffix(".git")]
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    session.close()  # release the connection before background scans open their own
    for rid in ids:
        background.add_task(_scan_in_background, factory, rid)
    return {"queued": ids}


def _scan_in_background(factory, repo_id: str) -> None:
    with factory() as sess:
        record = sess.get(RepoRecord, repo_id)
        if record:
            service.scan_record(sess, record, "webhook")
            sess.commit()
