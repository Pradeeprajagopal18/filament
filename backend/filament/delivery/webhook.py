"""GitHub webhook events for the delivery pipeline.

pull_request: a PR whose branch is `filament/<task id>-...` or whose description says
`Filament-Task: <task id>` is linked to that task; merges are recorded.
check_run / check_suite: CI results are recorded on the PR and its test plan, so the board
shows passing or failing checks without anyone polling.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.delivery import checks
from filament.delivery import service as svc
from filament.models import Artifact, Project


def handle(session: Session, event: str, payload: dict) -> dict:
    if event == "pull_request":
        return _pull_request(session, payload)
    return _checks(session, event, payload)


def _pull_request(session: Session, payload: dict) -> dict:
    pr = payload.get("pull_request") or {}
    info = {"url": pr.get("html_url", ""), "title": pr.get("title", ""),
            "body": pr.get("body") or "",
            "branch": (pr.get("head") or {}).get("ref", ""),
            "head_sha": (pr.get("head") or {}).get("sha", ""), "state": pr.get("state", ""),
            "merged": bool(pr.get("merged")), "number": pr.get("number"),
            "repo": (payload.get("repository") or {}).get("full_name", ""),
            "author": (pr.get("user") or {}).get("login", ""), "files": []}
    existing = next((a for a in session.scalars(select(Artifact).where(
        Artifact.type == "pull_request")) if (a.data or {}).get("url") == info["url"]), None)
    if existing is not None:
        existing.data = {**existing.data, **{k: info[k] for k in ("state", "merged", "head_sha")}}
        return {"updated": existing.id}
    for project in session.scalars(select(Project)).all():
        task = svc.task_for_pr(session, project, info)
        if task is not None:
            art = svc.attach_pull_request(session, project, task, info, author="webhook")
            return {"linked": art.id, "task": task.id}
    return {"ignored": "not a Filament task"}


def _checks(session: Session, event: str, payload: dict) -> dict:
    run = payload.get("check_run") or payload.get("check_suite") or {}
    sha = run.get("head_sha")
    if not sha or run.get("status") != "completed":
        return {"ignored": "not a completed check"}
    prs = [a for a in session.scalars(select(Artifact).where(Artifact.type == "pull_request"))
           if (a.data or {}).get("head_sha") == sha]
    if not prs:
        return {"ignored": "no Filament PR for this commit"}
    item = {"name": run.get("name") or (run.get("app") or {}).get("name", "checks"),
            "status": "completed", "conclusion": run.get("conclusion"),
            "summary": ((run.get("output") or {}).get("summary") or "")[:600],
            "url": run.get("html_url", "")}
    for pr in prs:
        seen = {c["name"]: c for c in (pr.data.get("check_runs") or [])}
        seen[item["name"]] = item
        result = checks.classify_checks(list(seen.values()))
        attempt = int(pr.data.get("attempt", 1))
        decision = checks.retry_decision(result["state"], attempt,
                                         get_settings().delivery_max_retries)
        pr.data = {**pr.data, "check_runs": list(seen.values()), "checks": result}
        plan = next((a for a in svc.related(session, pr) if a.type == "test_plan"), None)
        if plan is not None:
            state = "escalated" if decision["action"] == "escalate" else result["state"]
            plan.data = {**plan.data, "ci": {"state": state, "attempt": attempt,
                                             "failed": [f["name"] for f in result["failed"]]}}
    return {"recorded": [p.id for p in prs], "state": result["state"]}
