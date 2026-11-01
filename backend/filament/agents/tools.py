"""Tools agents can call in their tool-use loop.

The model decides *what* to do; these handlers decide *what it is allowed to do*. Write tools
go through Agent.save_artifact / remember, so autonomy levels, approval gates and provenance
apply no matter what the model asks for.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from filament.context import keywords
from filament.models import Artifact, Link, Signal

if TYPE_CHECKING:
    from filament.agents.base import Agent

ARTIFACT_TYPES = ["idea", "opportunity", "prd", "story", "flow", "adr", "api_contract", "task",
                  "test_plan", "release", "incident", "doc", "feature_request", "ui_mock",
                  "component_view"]
LINK_KINDS = ["implements", "derives_from", "visualizes", "depends_on", "verifies", "relates_to",
              "supersedes"]
EDITABLE = {"draft", "in_review"}


CITATION = re.compile(r"\b(artifact|memory|run):([0-9a-f]{12})\b")


def check_grounding(agent: Agent, text: str, sources: list[str] | None) -> list[str]:
    """Reject citations or sources the agent has not actually seen (anti-hallucination)."""
    cited = {m.group(2) for m in CITATION.finditer(text or "")}
    unknown = sorted((cited | set(sources or [])) - agent.seen_ids)
    if unknown:
        raise ToolError(
            f"Unknown or unread references: {', '.join(unknown)}. Only cite ids returned by "
            "your tools or present in the context; read an artifact before citing it.")
    return sorted(cited | set(sources or []))


class ToolError(Exception):
    """Raised by a handler; reported back to the model as an error result it can recover from."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Agent, dict], dict]
    writes: bool = False

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _summary(a: Artifact) -> dict:
    return {"id": a.id, "type": a.type, "title": a.title, "status": a.status,
            "snippet": a.content[:240]}


# -- read tools -------------------------------------------------------------------------
def search_graph(agent: Agent, inp: dict) -> dict:
    stmt = select(Artifact).where(Artifact.project_id == agent.project.id,
                                  Artifact.status != "rejected")
    if inp.get("types"):
        stmt = stmt.where(Artifact.type.in_(inp["types"]))
    arts = agent.session.scalars(stmt.order_by(Artifact.created_at)).all()
    q = keywords(inp.get("query", ""))
    if q:
        scored = [(len(q & keywords(a.title + " " + a.content)), a) for a in arts]
        arts = [a for s, a in sorted(scored, key=lambda x: -x[0]) if s > 0]
    limit = min(int(inp.get("limit", 10)), 25)
    return {"results": [_summary(a) for a in arts[:limit]], "total": len(arts)}


def read_artifact(agent: Agent, inp: dict) -> dict:
    a = agent.session.get(Artifact, inp["id"])
    if a is None or a.project_id != agent.project.id:
        raise ToolError(f"No artifact {inp['id']} in this project")
    links = agent.session.scalars(
        select(Link).where(or_(Link.source_id == a.id, Link.target_id == a.id))).all()
    return {**_summary(a), "content": a.content[:12_000], "version": a.version,
            "links": [{"source": lk.source_id, "target": lk.target_id, "kind": lk.kind}
                      for lk in links]}


def search_memory(agent: Agent, inp: dict) -> dict:
    from filament.context import relevant_memory

    entries = relevant_memory(agent.session, agent.project, inp.get("query", ""), limit=15)
    return {"results": [{"id": m.id, "scope": m.scope, "kind": m.kind, "status": m.status,
                         "content": m.content} for m in entries]}


def _repos(agent: Agent):
    from filament.models import RepoRecord

    return agent.session.scalars(select(RepoRecord).where(
        RepoRecord.workspace_id == agent.project.workspace_id)).all()


def search_repos(agent: Agent, inp: dict) -> dict:
    """Repos onboarded in this workspace, most relevant first."""
    from filament.repos import context as rc

    query = inp.get("query", "")
    repos = _repos(agent)
    ranked = sorted(repos, key=lambda r: -rc.relevance(r, query)) if query else repos
    if query:
        ranked = [r for r in ranked if rc.relevance(r, query) > 0] or ranked[:5]
    return {"results": [{"id": r.id, "name": r.name, "kinds": r.kinds, "grade": r.grade,
                         "endpoints": len((r.analysis.get("api") or {}).get("endpoints") or []),
                         "pages": len((r.analysis.get("ui") or {}).get("pages") or [])}
                        for r in ranked[:10]],
            "total": len(repos)}


def get_repo(agent: Agent, inp: dict) -> dict:
    """What Filament read from a repo's code: endpoints, screens, connections, dependents."""
    from filament.repos import context as rc
    from filament.repos.service import system_for

    key = (inp.get("repo") or "").lower()
    repos = _repos(agent)
    record = next((r for r in repos if r.id == key or r.name.lower() == key), None) or next(
        (r for r in repos if key and key in r.name.lower()), None)
    if record is None:
        raise ToolError(f"No onboarded repo '{inp.get('repo')}'. Known: "
                        f"{', '.join(r.name for r in repos) or 'none'}. Use search_repos.")
    detail = inp.get("detail", "summary")
    out = rc.summary(record, system_for(agent.session, record.workspace_id),
                     endpoints=200 if detail == "endpoints" else 25, query=inp.get("query", ""))
    if detail == "endpoints":
        eps = (record.analysis.get("api") or {}).get("endpoints") or []
        q = keywords(inp.get("query", ""))
        if q:
            eps = [e for e in eps if q & keywords(f"{e['path']} {e.get('summary', '')}")] or eps
        out["endpoint_details"] = [
            {"method": e["method"], "path": e["path"], "summary": e.get("summary", ""),
             "auth": e.get("auth"), "params": [p["name"] for p in e.get("params") or []],
             "request": ((e.get("request") or {}).get("schema") or {}).get("$ref", "")
             .rsplit("/", 1)[-1] or None,
             "responses": sorted((e.get("responses") or {}).keys())} for e in eps[:40]]
    return out


# -- write tools ------------------------------------------------------------------------
def _not_saved() -> dict:
    return {"saved": False,
            "reason": "Autonomy L0 (suggest only): nothing is saved. Describe the result instead."}


def create_artifact(agent: Agent, inp: dict) -> dict:
    if inp["type"] not in ARTIFACT_TYPES:
        raise ToolError(f"type must be one of {ARTIFACT_TYPES}")
    sources = check_grounding(agent, inp.get("content", ""), inp.get("sources"))
    targets = []  # validate links before writing anything
    for link in inp.get("link_to", []) or []:
        target = agent.session.get(Artifact, link.get("target_id", ""))
        if target is None or target.project_id != agent.project.id:
            raise ToolError(f"link target {link.get('target_id')} not found")
        targets.append((target, link.get("kind", "relates_to")))
    data = {**(inp.get("data") or {}), **({"sources": sources} if sources else {})}
    art = agent.save_artifact(inp["type"], inp["title"], inp.get("content", ""),
                              data=data, gate=True)
    if art is None:
        return _not_saved()
    for target, kind in targets:
        agent.link(art, target, kind)
    return {"saved": True, "id": art.id, "status": art.status}


def update_artifact(agent: Agent, inp: dict) -> dict:
    a = agent.session.get(Artifact, inp["id"])
    if a is None or a.project_id != agent.project.id:
        raise ToolError(f"No artifact {inp['id']} in this project")
    if a.status not in EDITABLE:
        raise ToolError(f"Artifact is {a.status}; only draft or in-review artifacts can be "
                        "edited. Create a new artifact that supersedes it instead.")
    check_grounding(agent, inp.get("content", ""), inp.get("sources"))
    if not agent.persists:
        return _not_saved()
    for field in ("title", "content"):
        if inp.get(field):
            setattr(a, field, inp[field])
    a.version += 1
    agent.touched.append(a)
    return {"saved": True, "id": a.id, "version": a.version}


def link_artifacts(agent: Agent, inp: dict) -> dict:
    src = agent.session.get(Artifact, inp["source_id"])
    dst = agent.session.get(Artifact, inp["target_id"])
    if not src or not dst or {src.project_id, dst.project_id} != {agent.project.id}:
        raise ToolError("Both artifacts must exist in this project")
    if not agent.persists:
        return _not_saved()
    agent.link(src, dst, inp["kind"])
    return {"saved": True}


def propose_memory(agent: Agent, inp: dict) -> dict:
    if not inp.get("source_artifact_ids"):
        raise ToolError("Memory must be grounded: pass source_artifact_ids for the artifacts "
                        "this fact comes from.")
    check_grounding(agent, inp["content"], inp["source_artifact_ids"])
    if not agent.persists:
        return _not_saved()
    agent.remember(inp["content"], kind=inp.get("kind", "fact"),
                   sources=inp.get("source_artifact_ids") or [])
    return {"saved": True, "status": "proposed",
            "note": "A human must confirm this before it becomes shared memory."}


def ask_human(agent: Agent, inp: dict) -> dict:
    agent.session.add(Signal(workspace_id=agent.project.workspace_id, project_id=agent.project.id,
                             kind="agent_question", text=inp["question"],
                             asker=f"agent:{agent.name}"))
    agent.questions.append(inp["question"])
    return {"logged": True,
            "note": "The question is queued for a human. Do not wait: proceed with a clearly "
                    "stated assumption and record it as an open question."}


def finish(agent: Agent, inp: dict) -> dict:
    agent.finished_summary = inp.get("summary", "")
    return {"done": True}


def delegate(agent: Agent, inp: dict) -> dict:
    """Run a specialist agent as a child run (orchestrator only)."""
    from filament.agents import REGISTRY, execute_run
    from filament.models import Run

    name = inp["agent"]
    if name not in REGISTRY or name == agent.name:
        raise ToolError(f"agent must be one of {sorted(set(REGISTRY) - {agent.name})}")
    if agent.run_record.parent_run_id:
        raise ToolError("Delegation depth limit reached: specialists cannot delegate further")
    child = Run(project_id=agent.project.id, agent=name, goal=inp.get("goal", name),
                input={k: v for k, v in (("idea", inp.get("idea")),
                                         ("artifact_ids", inp.get("artifact_ids"))) if v},
                autonomy=agent.autonomy, requested_by=f"agent:{agent.name}",
                parent_run_id=agent.run_record.id)
    from filament.runtime import resolve_config

    child.config = {**resolve_config(agent.session, agent.project, name), "runner": "inline",
                    "runner_kind": "inline", "runner_id": None}  # children run with the parent
    agent.session.flush()
    child = execute_run(agent.session, agent.project, child, commit=False)
    agent.children.append(child.id)
    out = {k: v for k, v in child.output.items() if k not in {"trace", "spec"}}
    return {"run_id": child.id, "status": child.status, "error": child.error, "output": out}


_ARTIFACT_INPUT = {
    "type": {"type": "string", "enum": ARTIFACT_TYPES},
    "title": {"type": "string"},
    "content": {"type": "string",
                "description": "Markdown body. Diagrams go in ```mermaid blocks."},
    "data": {"type": "object", "description": "Optional structured fields"},
    "sources": {"type": "array", "items": {"type": "string"},
                "description": "Ids of the artifacts and memory entries this is based on"},
    "link_to": {"type": "array", "items": _obj(
        {"target_id": {"type": "string"}, "kind": {"type": "string", "enum": LINK_KINDS}},
        ["target_id", "kind"])},
}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_graph", "Search this project's artifacts (PRDs, stories, flows, ADRs, ...). "
         "Empty query lists them. Use before creating anything to avoid duplicates.",
         _obj({"query": {"type": "string"},
               "types": {"type": "array", "items": {"type": "string", "enum": ARTIFACT_TYPES}},
               "limit": {"type": "integer"}}), search_graph),
    Tool("read_artifact", "Read an artifact's full content and its links.",
         _obj({"id": {"type": "string"}}, ["id"]), read_artifact),
    Tool("search_memory", "Search shared memory: org/team/project standards, decisions, facts.",
         _obj({"query": {"type": "string"}}, ["query"]), search_memory),
    Tool("search_repos", "Find onboarded code repos (APIs, frontends, infrastructure, config) "
         "by name, endpoint path, screen or dependency.",
         _obj({"query": {"type": "string"}}), search_repos),
    Tool("get_repo", "What Filament read from a repo's code: kinds, API endpoints, screens, "
         "connections, who depends on it. Use detail='endpoints' (with an optional query) for "
         "parameters, request models and responses. Base designs on these facts, not guesses.",
         _obj({"repo": {"type": "string", "description": "Repo name or id"},
               "detail": {"type": "string", "enum": ["summary", "endpoints"]},
               "query": {"type": "string"}}, ["repo"]), get_repo),
    Tool("create_artifact", "Create an artifact in the project graph, optionally linked to "
         "existing artifacts. Saved per the run's autonomy level.",
         _obj(_ARTIFACT_INPUT, ["type", "title", "content"]), create_artifact, writes=True),
    Tool("update_artifact", "Revise a draft or in-review artifact (bumps its version).",
         _obj({"id": {"type": "string"}, "title": {"type": "string"},
               "content": {"type": "string"}, "reason": {"type": "string"},
               "sources": {"type": "array", "items": {"type": "string"}}}, ["id", "reason"]),
         update_artifact, writes=True),
    Tool("link_artifacts", "Add a typed link between two artifacts.",
         _obj({"source_id": {"type": "string"}, "target_id": {"type": "string"},
               "kind": {"type": "string", "enum": LINK_KINDS}},
              ["source_id", "target_id", "kind"]), link_artifacts, writes=True),
    Tool("propose_memory", "Propose a durable decision or fact for shared memory (a human "
         "confirms it).",
         _obj({"content": {"type": "string"},
               "kind": {"type": "string", "enum": ["fact", "decision", "procedure"]},
               "source_artifact_ids": {"type": "array", "items": {"type": "string"}}},
              ["content", "source_artifact_ids"]), propose_memory, writes=True),
    Tool("ask_human", "Queue a question for a human when something important is ambiguous.",
         _obj({"question": {"type": "string"}}, ["question"]), ask_human),
    Tool("finish", "Call when the goal is complete, with a short summary of what you did.",
         _obj({"summary": {"type": "string"}}, ["summary"]), finish),
    Tool("delegate", "Hand a sub-goal to a specialist agent and get its result.",
         _obj({"agent": {"type": "string", "enum": ["requirements", "flow"]},
               "goal": {"type": "string"},
               "idea": {"type": "string", "description": "Idea text, for the requirements agent"},
               "artifact_ids": {"type": "array", "items": {"type": "string"}}},
              ["agent", "goal"]), delegate),
]}

READ_TOOLS = ["search_graph", "read_artifact", "search_memory", "search_repos", "get_repo"]
SPECIALIST_TOOLS = READ_TOOLS + ["create_artifact", "update_artifact", "link_artifacts",
                                 "propose_memory", "ask_human", "finish"]
ORCHESTRATOR_TOOLS = READ_TOOLS + ["delegate", "ask_human", "finish"]
