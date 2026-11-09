"""Omni MCP tools for external coding agents (Claude Code, Codex, Cursor): read the task they
were handed, and report the pull request they opened so the pipeline picks it up."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from filament.delivery import github
from filament.delivery import service as svc
from filament.models import Artifact, Project

SPECS = [
    {"name": "filament_list_tasks", "description": "Coding tasks Filament has handed off in this "
     "workspace that do not have a pull request yet.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "filament_get_task", "description": "The brief for a Filament coding task: goal, "
     "acceptance criteria, code to start from, endpoints, conventions, tests, branch name.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "filament_report_pr", "description": "Tell Filament the pull request you opened for "
     "a task so QA and review can start.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"},
                                                       "url": {"type": "string"}},
                      "required": ["task_id", "url"]}},
]
NAMES = {s["name"] for s in SPECS}


class TaskError(Exception):
    pass


def _task(session: Session, ws_id: str, task_id: str) -> tuple[Artifact, Project]:
    task = session.get(Artifact, task_id or "")
    project = session.get(Project, task.project_id) if task else None
    if task is None or task.type != "task" or project is None or project.workspace_id != ws_id:
        raise TaskError(f"No coding task {task_id} in this workspace")
    return task, project


def run(session: Session, ws_id: str, name: str, args: dict) -> dict:
    if name == "filament_list_tasks":
        projects = {p.id for p in session.scalars(select(Project).where(
            Project.workspace_id == ws_id))}
        tasks = session.scalars(select(Artifact).where(Artifact.type == "task",
                                                       Artifact.project_id.in_(projects))).all()
        open_ = [t for t in tasks
                 if not any(a.type == "pull_request" for a in svc.related(session, t))]
        return {"tasks": [{"task_id": t.id, "title": t.title, "branch": t.data.get("branch"),
                           "attempt": t.data.get("attempt", 1)} for t in open_[-20:]]}
    task, project = _task(session, ws_id, args.get("task_id", ""))
    if name == "filament_get_task":
        return {"task_id": task.id, "title": task.title, "branch": task.data.get("branch"),
                "repo": task.data.get("repo"), "brief": task.content}
    try:
        pr = github.pull_request(args.get("url", ""))
    except github.GitHubError as exc:
        raise TaskError(str(exc)) from exc
    art = svc.attach_pull_request(session, project, task, pr, author="omni")
    session.commit()
    return {"linked": True, "pull_request_id": art.id,
            "next": "QA checks CI and the Review agent does a first pass."}
