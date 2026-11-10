"""Agent runs, token usage and Launchpad readiness."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from filament import access
from filament import schemas as s
from filament.agents import REGISTRY, execute_run, list_agents
from filament.api.graph import get_or_404
from filament.db import get_session
from filament.models import Project, ReadinessReport, Run
from filament.readiness import scan
from filament.runtime import INLINE, resolve_config

router = APIRouter()


@router.get("/agents")
def agents():
    return list_agents()


@router.post("/projects/{project_id}/runs", response_model=s.RunOut, status_code=201)
def start_run(project_id: str, body: s.RunIn, session: Session = Depends(get_session)):
    project = get_or_404(session, Project, project_id)
    if body.agent not in REGISTRY:
        raise HTTPException(400, f"Unknown agent '{body.agent}'. Known: {sorted(REGISTRY)}")
    try:
        cfg = resolve_config(session, project, body.agent, {
            "runner": body.runner, "model": body.model, "max_turns": body.max_turns,
            "token_budget": body.token_budget})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    run = Run(project_id=project_id, agent=body.agent, goal=body.goal, input=body.input,
              autonomy=body.autonomy, requested_by=access.requester(body.requested_by),
              config=cfg,
              runner_id=cfg["runner_id"])
    if cfg["runner_kind"] != INLINE:
        # Executes wherever `fil runner start <name>` is running for this runner.
        run.status = "queued"
        session.add(run)
        session.commit()
        return run
    return execute_run(session, project, run)


@router.get("/projects/{project_id}/runs", response_model=list[s.RunOut])
def list_runs(project_id: str, session: Session = Depends(get_session)):
    stmt = select(Run).where(Run.project_id == project_id).order_by(Run.started_at.desc())
    return session.scalars(stmt).all()


@router.get("/runs/{run_id}", response_model=s.RunOut)
def get_run(run_id: str, session: Session = Depends(get_session)):
    return get_or_404(session, Run, run_id)


USAGE_KEYS = {"agent": Run.agent, "project": Run.project_id, "model": Run.model,
              "person": Run.requested_by, "status": Run.status}


@router.get("/workspaces/{ws_id}/usage", response_model=list[s.UsageRow])
def usage(ws_id: str, by: str = "agent", session: Session = Depends(get_session)):
    """Token and cost totals grouped by agent, project, model, person or status."""
    col = USAGE_KEYS.get(by)
    if col is None:
        raise HTTPException(400, f"by must be one of {sorted(USAGE_KEYS)}")
    stmt = (
        select(col, func.count(Run.id), func.sum(Run.input_tokens), func.sum(Run.output_tokens),
               func.sum(Run.cost_usd))
        .join(Project, Run.project_id == Project.id)
        .where(Project.workspace_id == ws_id)
        .group_by(col)
    )
    rows = session.execute(stmt).all()
    if by == "person":  # show people by email, not internal ids
        from filament.models import User

        def name(k):
            if isinstance(k, str) and k.startswith("user:"):
                u = session.get(User, k[5:])
                return u.email if u else k
            return k
        merged: dict = {}
        for k, n, i, o, c in rows:
            m = merged.setdefault(name(k), [0, 0, 0, 0.0])
            m[0] += n
            m[1] += i or 0
            m[2] += o or 0
            m[3] += c or 0
        rows = [(k, *v) for k, v in merged.items()]
    return sorted(
        ({"key": str(k), "runs": n, "input_tokens": i or 0, "output_tokens": o or 0,
          "cost_usd": c} for k, n, i, o, c in rows),
        key=lambda r: r["input_tokens"] + r["output_tokens"], reverse=True,
    )


@router.post("/readiness/scan", response_model=s.ReadinessOut, status_code=201)
def readiness_scan(body: s.ReadinessIn, session: Session = Depends(get_session)):
    try:
        result = scan(body.repo)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    report = ReadinessReport(repo=body.repo, project_id=body.project_id, score=result.score,
                             grade=result.grade, checks=[c.__dict__ for c in result.checks])
    session.add(report)
    session.commit()
    return report


@router.get("/readiness", response_model=list[s.ReadinessOut])
def readiness_reports(repo: str | None = None, session: Session = Depends(get_session)):
    stmt = select(ReadinessReport).order_by(ReadinessReport.created_at.desc())
    if repo:
        stmt = stmt.where(ReadinessReport.repo == repo)
    p = access.current()
    rows = session.scalars(stmt.limit(200)).all()
    if p.unrestricted or p.is_admin:
        return rows
    ok = {}
    out = []
    for r in rows:  # reports without a project were run by an admin; keep them to admins
        if r.project_id and r.project_id not in ok:
            proj = session.get(Project, r.project_id)
            ok[r.project_id] = bool(proj and access.can(session, p, proj))
        if r.project_id and ok[r.project_id]:
            out.append(r)
    return out
