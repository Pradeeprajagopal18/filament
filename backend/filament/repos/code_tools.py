"""Code graph tools shared by Filament's agents and the Omni MCP endpoint."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from filament.models import CodeGraph, RepoRecord
from filament.repos import codequery as cq
from filament.repos.codegraph import Index

SPECS = [
    {"name": "code_query", "description": "Search a repo's code graph (functions, classes, modules "
     "and how they connect) for a question; returns the matching symbols and their neighbours. "
     "Use instead of guessing where something is implemented.",
     "input_schema": {"type": "object", "properties": {"repo": {"type": "string"},
                                                       "question": {"type": "string"}},
                      "required": ["repo", "question"]}},
    {"name": "code_explain", "description": "Explain one symbol: what it calls, who calls it, "
     "the endpoints it serves, the services it reaches and design notes about it. Ambiguous "
     "names return candidates; ask again with the id.",
     "input_schema": {"type": "object", "properties": {"repo": {"type": "string"},
                                                       "name": {"type": "string"}},
                      "required": ["repo", "name"]}},
    {"name": "code_path", "description": "Shortest chain of calls/imports/references between "
     "two symbols, with the confidence of each hop.",
     "input_schema": {"type": "object", "properties": {"repo": {"type": "string"},
                                                       "from": {"type": "string"},
                                                       "to": {"type": "string"}},
                      "required": ["repo", "from", "to"]}},
    {"name": "code_impact", "description": "What a change affects: give symbols or changed files; "
     "returns affected code, endpoints and the repos that consume them.",
     "input_schema": {"type": "object", "properties": {
         "repo": {"type": "string"},
         "symbols": {"type": "array", "items": {"type": "string"}},
         "files": {"type": "array", "items": {"type": "string"}}},
         "required": ["repo"]}},
]
NAMES = {s["name"] for s in SPECS}


class CodeToolError(Exception):
    pass


def _repo(session: Session, workspace_id: str, key: str) -> tuple[RepoRecord, dict]:
    key = (key or "").lower()
    repos = session.scalars(select(RepoRecord).where(RepoRecord.workspace_id == workspace_id)).all()
    record = next((r for r in repos if r.id == key or r.name.lower() == key), None) or next(
        (r for r in repos if key and key in r.name.lower()), None)
    if record is None:
        raise CodeToolError(f"No onboarded repo '{key}'. Known: "
                            f"{', '.join(r.name for r in repos) or 'none'}")
    row = session.get(CodeGraph, record.id)
    if row is None or not row.graph.get("nodes"):
        raise CodeToolError(f"{record.name} has no code graph yet (rescan it)")
    return record, row.graph


def run(session: Session, workspace_id: str, name: str, args: dict) -> dict:
    record, g = _repo(session, workspace_id, args.get("repo", ""))
    code = (record.analysis or {}).get("code") or {}
    try:
        if name == "code_query":
            return {"repo": record.name, **cq.query(g, args.get("question", ""))}
        if name == "code_explain":
            node = cq.resolve(Index(g), args.get("name", ""))
            eps = [k for k, v in (code.get("endpoints") or {}).items() if v["node"] == node["id"]]
            svcs = next((v["services"] for v in (code.get("endpoints") or {}).values()
                         if v["node"] == node["id"]), [])
            return {"repo": record.name, **cq.explain(g, node["id"], {"serves_endpoints": eps,
                                                                        "uses_services": svcs})}
        if name == "code_path":
            return {"repo": record.name, **cq.path(g, args.get("from", ""), args.get("to", ""))}
        if name == "code_impact":
            from filament.repos.service import system_for

            idx = Index(g)
            changes: dict = {f: None for f in args.get("files") or []}
            for s in args.get("symbols") or []:
                n = cq.resolve(idx, s)
                if n.get("file"):
                    from filament.repos.codegraph import _ranges

                    r = _ranges(idx).get(n["id"])
                    changes.setdefault(n["file"], [])
                    if changes[n["file"]] is not None:
                        changes[n["file"]].append((r[1], r[1]) if r else (n["line"] or 1,) * 2)
            if not changes:
                raise CodeToolError("Pass symbols or files")
            system = system_for(session, record.workspace_id)
            return {"repo": record.name, **cq.change_impact(
                g, code.get("endpoints") or {}, changes, system["consumers"].get(record.id, {}),
                {h["id"] for h in code.get("hotspots") or []})}
    except cq.Ambiguous as exc:
        return {"ambiguous": True, "message": str(exc), "candidates": exc.candidates}
    except cq.NotFound as exc:
        raise CodeToolError(f"{exc}. Close matches: "
                            f"{', '.join(s['label'] + ' (' + s['id'] + ')' for s in exc.suggestions[:6]) or 'none'}") from exc
    raise CodeToolError(f"Unknown code tool {name}")
