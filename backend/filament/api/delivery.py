"""Delivery pipeline API: board, work items, stage agents, coding handoffs, SLOs, releases."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from filament import access
from filament import schemas as s
from filament.agents import execute_run
from filament.api.graph import get_or_404
from filament.db import get_session
from filament.delivery import github
from filament.delivery import service as svc
from filament.models import Approval, Artifact, Project, Run, SloSample
from filament.runtime import INLINE, resolve_config

router = APIRouter()


def _project(session: Session, pid: str) -> Project:
    return get_or_404(session, Project, pid)


def _typed(session: Session, aid: str, type_: str) -> Artifact:
    art = get_or_404(session, Artifact, aid)
    if art.type != type_:
        raise HTTPException(404, f"{aid} is a {art.type}, not a {type_}")
    return art


def _item(session: Session, issue: Artifact) -> dict:
    project = session.get(Project, issue.project_id)
    return next(i for i in svc.board(session, project)["items"] if i["id"] == issue.id)


@router.get("/projects/{project_id}/delivery")
def delivery_board(project_id: str, session: Session = Depends(get_session)):
    """Every work item with its seven stages: agent, state, artifacts and pending checkpoint."""
    return svc.board(session, _project(session, project_id))


class IssueIn(BaseModel):
    title: str
    body: str = ""
    labels: list[str] = []
    repo: str | None = None


@router.post("/projects/{project_id}/issues", status_code=201)
def add_issue(project_id: str, body: IssueIn, session: Session = Depends(get_session)):
    issue = svc.add_issue(session, _project(session, project_id), body.title, body.body,
                          {"labels": body.labels, "repo": body.repo, "source": "manual"})
    session.commit()
    return _item(session, issue)


class ImportIn(BaseModel):
    repo: str = Field(description="owner/repo or a GitHub URL")
    state: str = "open"
    labels: str = ""
    limit: int = 20


@router.post("/projects/{project_id}/issues/import", status_code=201)
def import_issues(project_id: str, body: ImportIn, session: Session = Depends(get_session)):
    try:
        arts = svc.import_issues(session, _project(session, project_id), body.repo, body.state,
                                 body.labels, body.limit)
    except github.GitHubError as exc:
        raise HTTPException(502, str(exc)) from exc
    session.commit()
    return {"imported": [{"id": a.id, "title": a.title} for a in arts]}


class AdvanceIn(BaseModel):
    stage: str | None = Field(None, description="plan|design|build|test|review|deploy|operate; "
                                                "default: the item's current stage")
    autonomy: str = "L2"
    requested_by: str = "human"
    target: str = Field("claude_code", description="claude_code | codex | cursor (build)")
    repo: str | None = None
    pr_url: str | None = None
    post: bool = Field(False, description="Review: post the review on the PR (needs L3+)")


def _gate(states: list[dict], key: str, message: str) -> None:
    st = next(s for s in states if s["key"] == key)
    if st["state"] not in svc.DONE and st["state"] != "todo":
        raise HTTPException(409, message)


@router.post("/work-items/{issue_id}/advance")
def advance(issue_id: str, body: AdvanceIn, session: Session = Depends(get_session)):
    """Run the agent for a stage of a work item. Checkpoints are enforced here: a stage cannot
    start until the person who owns the previous checkpoint has signed off."""
    issue = _typed(session, issue_id, "issue")
    project = session.get(Project, issue.project_id)
    states = svc.stage_states(session, issue)
    stage = body.stage or svc.current_stage(states)
    if stage not in svc.STAGE_BY_KEY:
        raise HTTPException(422, f"stage must be one of {[s['key'] for s in svc.STAGES]}")
    st = svc.STAGE_BY_KEY[stage]
    prs = sorted([a for a in svc.related(session, issue) if a.type == "pull_request"],
                 key=lambda a: a.created_at)
    inp: dict = {}
    if stage == "plan":
        inp, goal = {"issue_ids": [issue.id]}, "Score this work item's readiness"
    elif stage == "design":
        if issue.status != "approved":
            raise HTTPException(409, "Waiting for the product owner to approve the scope")
        inp = {"idea": f"{issue.title}\n\n{issue.content}", "artifact_ids": [issue.id]}
        goal = "Take this work item to a reviewed design"
    elif stage == "build":
        if issue.status != "approved":
            raise HTTPException(409, "Waiting for the product owner to approve the scope")
        _gate(states, "design", "Waiting for the tech lead to sign off on the design")
        inp = {"issue_id": issue.id, "target": body.target, "repo": body.repo,
               "pr_url": body.pr_url}
        goal = f"Hand this work item to {body.target.replace('_', ' ')}"
    else:
        if not prs:
            raise HTTPException(409, "No pull request yet: the coding agent's PR must be linked "
                                     "first (it links itself when the PR mentions the task)")
        pr = prs[-1]
        if stage == "test":
            inp, goal = {"pr_id": pr.id}, "Test this pull request until CI passes"
        elif stage == "review":
            inp, goal = {"pr_id": pr.id, "post": body.post}, "First-pass review of this PR"
        elif stage == "deploy":
            review = next(s for s in states if s["key"] == "review")
            if review["state"] != "approved":
                raise HTTPException(409, "Waiting for an engineer to approve the PR review")
            inp, goal = {"pr_ids": [pr.id], "title": issue.title}, "Plan the production rollout"
        else:
            inp, goal = {}, "Check SLOs after the release"
    inp = {k: v for k, v in inp.items() if v not in (None, "")}
    try:
        cfg = resolve_config(session, project, st["agent"], {})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    run = Run(project_id=project.id, agent=st["agent"], goal=goal, input=inp,
              autonomy=body.autonomy, requested_by=access.requester(body.requested_by),
              config=cfg,
              runner_id=cfg["runner_id"])
    if cfg["runner_kind"] != INLINE:
        run.status = "queued"
        session.add(run)
        session.commit()
        return {"run": s.RunOut.model_validate(run), "item": _item(session, issue)}
    run = execute_run(session, project, run)
    _attach_outputs(session, project, issue, run, stage)
    session.commit()
    return {"run": s.RunOut.model_validate(run), "item": _item(session, issue)}


def _attach_outputs(session: Session, project: Project, issue: Artifact, run: Run,
                    stage: str) -> None:
    """Whatever a stage produced hangs off its work item, so the board can find it."""
    run_ids = [run.id] + list(session.scalars(select(Run.id).where(Run.parent_run_id == run.id)))
    made = session.scalars(select(Artifact).where(Artifact.run_id.in_(run_ids))).all()
    reach = {a.id for a in svc.related(session, issue)} | {issue.id}
    for a in made:
        if a.id not in reach and a.type != "issue":
            svc.link(session, project, a, issue, svc.LINK_KIND.get(stage, "relates_to"))
            reach |= {a.id} | {x.id for x in svc.related(session, a)}


@router.get("/tasks/{task_id}/handoff")
def handoff(task_id: str, session: Session = Depends(get_session)):
    """The brief for an external coding agent (also available to it over the Omni MCP)."""
    task = _typed(session, task_id, "task")
    d = task.data or {}
    return {"id": task.id, "title": task.title, "branch": d.get("branch"),
            "target": d.get("target"), "brief": task.content, "prompt": d.get("prompt", ""),
            "commands": d.get("commands", {}), "attempt": d.get("attempt", 1)}


class PrIn(BaseModel):
    url: str


@router.post("/tasks/{task_id}/pull-request", status_code=201)
def link_pr(task_id: str, body: PrIn, session: Session = Depends(get_session)):
    task = _typed(session, task_id, "task")
    project = session.get(Project, task.project_id)
    try:
        pr = github.pull_request(body.url)
    except github.GitHubError as exc:
        raise HTTPException(502, str(exc)) from exc
    art = svc.attach_pull_request(session, project, task, pr)
    session.commit()
    issue = svc.issue_of(session, art)
    return {"id": art.id, "item": _item(session, issue) if issue else None}


class SloIn(BaseModel):
    title: str
    objective: float = Field(99.9, gt=0, lt=100)
    service: str | None = None
    description: str = ""


@router.post("/projects/{project_id}/slos", status_code=201)
def add_slo(project_id: str, body: SloIn, session: Session = Depends(get_session)):
    project = _project(session, project_id)
    art = Artifact(project_id=project.id, type="slo", title=body.title, content=body.description,
                   data={"objective": body.objective, "service": body.service}, status="approved")
    session.add(art)
    session.commit()
    return {"id": art.id, "title": art.title, "objective": body.objective}


@router.get("/projects/{project_id}/slos")
def list_slos(project_id: str, session: Session = Depends(get_session)):
    return svc.slo_overview(session, _project(session, project_id))


class SampleIn(BaseModel):
    good: int = Field(ge=0)
    total: int = Field(ge=0)
    at: datetime | None = None


@router.post("/slos/{slo_id}/samples", status_code=201)
def add_samples(slo_id: str, body: list[SampleIn], session: Session = Depends(get_session)):
    """Push measurements (e.g. from your metrics pipeline or a monitoring MCP)."""
    slo = _typed(session, slo_id, "slo")
    for b in body:
        if b.good > b.total:
            raise HTTPException(422, "good cannot exceed total")
        session.add(SloSample(project_id=slo.project_id, slo_id=slo.id, good=b.good,
                              total=b.total, at=b.at or datetime.now(UTC)))
    session.commit()
    return {"added": len(body)}


@router.post("/projects/{project_id}/slo-check")
def slo_check(project_id: str, session: Session = Depends(get_session)):
    """Evaluate SLOs now (the scheduler also does this): auto-rollback, incidents, follow-ups."""
    out = svc.check_slos(session, _project(session, project_id))
    session.commit()
    return out


class ActorIn(BaseModel):
    by: str = "human"
    reason: str = ""


@router.post("/releases/{release_id}/{action}")
def release_action(release_id: str, action: str, body: ActorIn,
                   session: Session = Depends(get_session)):
    rel = _typed(session, release_id, "release")
    project = session.get(Project, rel.project_id)
    by = access.actor(body.by)
    try:
        if action == "promote":
            access.check_sign_off(session, project, "release_manager")
            svc.promote(session, rel, by)
        elif action == "rollback":
            # Rolling back is the safe direction: on-call or the release manager may do it.
            if not (access.can_sign_off(session, access.current(), project, "on_call")
                    or access.can_sign_off(session, access.current(), project,
                                           "release_manager")):
                access.check_sign_off(session, project, "on_call")
            svc.rollback(session, rel, by, body.reason or "Manual rollback")
        else:
            raise HTTPException(404, "action must be promote or rollback")
        access.audit(session, f"release.{action}", workspace_id=project.workspace_id,
                     resource=rel.id, reason=body.reason)
    except svc.DeliveryError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {"id": rel.id, "stage": rel.data["stage"], "steps": rel.data.get("steps", [])}


@router.post("/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, body: ActorIn, session: Session = Depends(get_session)):
    inc = _typed(session, incident_id, "incident")
    project = session.get(Project, inc.project_id)
    access.check_sign_off(session, project, "on_call")
    by = access.actor(body.by)
    access.audit(session, "incident.resolve", workspace_id=project.workspace_id, resource=inc.id)
    inc.data = {**inc.data, "state": "resolved", "resolved_by": by,
                "resolution": body.reason, "resolved_at": datetime.now(UTC).isoformat()}
    inc.status = "approved"
    for ap in session.scalars(select(Approval).where(Approval.artifact_id == inc.id,
                                                     Approval.status == "pending")):
        ap.status, ap.decided_by, ap.decided_at = "approved", by, datetime.now(UTC)
    session.commit()
    return {"id": inc.id, "state": "resolved"}
