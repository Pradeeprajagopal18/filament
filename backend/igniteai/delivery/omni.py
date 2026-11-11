"""Omni MCP tools for external coding agents (Claude Code, Codex, Cursor): read the task they
were handed, and report the pull request they opened so the pipeline picks it up."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from igniteai import access
from igniteai.delivery import github
from igniteai.delivery import service as svc
from igniteai.models import Artifact, Project

SPECS = [
    {"name": "ignite_list_tasks", "description": "Coding tasks Ignite has handed off in this "
     "workspace that do not have a pull request yet.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "ignite_get_task", "description": "The brief for a Ignite coding task: goal, "
     "acceptance criteria, code to start from, endpoints, conventions, tests, branch name.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "ignite_report_pr", "description": "Tell Ignite the pull request you opened for "
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
    if task is None or task.type != "task" or project is None or project.workspace_id != ws_id \
            or not access.can(session, access.current(), project) \
            and not access.current().unrestricted:
        raise TaskError(f"No coding task {task_id} in this workspace")
    return task, project


def run(session: Session, ws_id: str, name: str, args: dict) -> dict:
    if name == "ignite_list_tasks":
        projects = {p.id for p in access.visible_projects(session, access.current(),
                                                          session.scalars(select(Project).where(
                                                              Project.workspace_id == ws_id)))}
        tasks = session.scalars(select(Artifact).where(Artifact.type == "task",
                                                       Artifact.project_id.in_(projects))).all()
        open_ = [t for t in tasks
                 if not any(a.type == "pull_request" for a in svc.related(session, t))]
        return {"tasks": [{"task_id": t.id, "title": t.title, "branch": t.data.get("branch"),
                           "attempt": t.data.get("attempt", 1)} for t in open_[-20:]]}
    task, project = _task(session, ws_id, args.get("task_id", ""))
    if name == "ignite_get_task":
        return {"task_id": task.id, "title": task.title, "branch": task.data.get("branch"),
                "repo": task.data.get("repo"), "brief": task.content}
    try:
        pr = github.pull_request(args.get("url", ""))
    except github.GitHubError as exc:
        raise TaskError(str(exc)) from exc
    if not access.current().unrestricted and not access.can(session, access.current(), project,
                                                             "contributor"):
        raise TaskError("You need the contributor role on this project to link a PR")
    art = svc.attach_pull_request(session, project, task, pr, author=access.actor("omni"))
    session.commit()
    return {"linked": True, "pull_request_id": art.id,
            "next": "QA checks CI and the Review agent does a first pass."}
