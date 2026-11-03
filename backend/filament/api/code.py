"""Code graph API: summary and graph for the UI, explain/path/query/affected, change and PR
impact, conflicts between change sets."""

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from filament.api.graph import get_or_404
from filament.db import get_session
from filament.models import CodeGraph, RepoRecord
from filament.repos import codequery as cq
from filament.repos import service
from filament.repos.codegraph import STRUCTURAL, Index

router = APIRouter()


def _graph(session: Session, repo_id: str) -> tuple[RepoRecord, dict]:
    record = get_or_404(session, RepoRecord, repo_id)
    row = session.get(CodeGraph, repo_id)
    if row is None or not row.graph.get("nodes"):
        reason = ((record.analysis or {}).get("code") or {})
        raise HTTPException(404, reason.get("error") or reason.get("reason")
                            or "No code graph yet: rescan the repo")
    return record, row.graph


def _resolve_errors(fn):
    try:
        return fn()
    except cq.Ambiguous as exc:
        return JSONResponse({"detail": str(exc), "ambiguous": True,
                             "candidates": exc.candidates}, status_code=409)
    except cq.NotFound as exc:
        return JSONResponse({"detail": str(exc), "suggestions": exc.suggestions},
                            status_code=404)


@router.get("/repos/{repo_id}/code")
def code_summary(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    row = session.get(CodeGraph, repo_id)
    return {**((record.analysis or {}).get("code") or {}),
            "built_at": row.built_at if row else None, "commit": row.commit if row else None}


@router.get("/repos/{repo_id}/code/graph")
def code_graph(repo_id: str, community: int | None = None, limit: int = 400,
               session: Session = Depends(get_session)):
    """Aggregated subsystem view by default; pass `community` for its symbols and neighbours."""
    record, g = _graph(session, repo_id)
    names = {c["id"]: c["name"] for c in (record.analysis.get("code") or {}).get("communities", [])}
    idx = Index(g)
    if community is None:
        size: dict = {}
        for n in g["nodes"]:
            if n["kind"] == "code" and n.get("community") is not None:
                size[n["community"]] = size.get(n["community"], 0) + 1
        links: dict = {}
        for e in g["edges"]:
            if e["relation"] in STRUCTURAL:
                continue
            a = idx.nodes[e["source"]].get("community")
            b = idx.nodes[e["target"]].get("community")
            if a is None or b is None or a == b:
                continue
            k = tuple(sorted((a, b)))
            links[k] = links.get(k, 0) + 1
        ranked = sorted(size.items(), key=lambda x: -x[1])[:limit]
        return {"mode": "subsystems",
                "nodes": [{"id": f"c{c}", "community": c, "label": names.get(c, f"group {c}"),
                           "size": s} for c, s in ranked],
                "edges": [{"source": f"c{a}", "target": f"c{b}", "weight": w}
                          for (a, b), w in links.items()]}
    inside = [n for n in g["nodes"] if n.get("community") == community and n["kind"] == "code"]
    ids = {n["id"] for n in inside}
    edges = [e for e in g["edges"] if (e["source"] in ids or e["target"] in ids)
             and e["relation"] not in {"rationale_for"}]
    border = {x for e in edges for x in (e["source"], e["target"])} - ids
    shown = {"code", "external", "concept"}
    keep = list(ids) + [b for b in border if idx.nodes[b]["kind"] in shown]
    keep = keep[:limit]
    ks = set(keep)
    return {"mode": "symbols", "community": community, "name": names.get(community),
            "nodes": [{**{k: idx.nodes[i][k] for k in ("id", "label", "kind", "file", "line",
                                                       "callable")},
                       "inside": i in ids} for i in keep],
            "edges": [{"source": e["source"], "target": e["target"], "relation": e["relation"],
                       "confidence": e["confidence"]} for e in edges
                      if e["source"] in ks and e["target"] in ks]}


@router.get("/repos/{repo_id}/code/explain")
def code_explain(repo_id: str, name: str, session: Session = Depends(get_session)):
    record, g = _graph(session, repo_id)

    def run():
        code = record.analysis.get("code") or {}
        node = cq.resolve(Index(g), name)
        eps = [k for k, v in (code.get("endpoints") or {}).items() if v["node"] == node["id"]]
        services = next((v["services"] for v in (code.get("endpoints") or {}).values()
                         if v["node"] == node["id"]), [])
        return cq.explain(g, node["id"], {"serves_endpoints": eps, "uses_services": services})
    return _resolve_errors(run)


@router.get("/repos/{repo_id}/code/path")
def code_path(repo_id: str, a: str, b: str, session: Session = Depends(get_session)):
    _, g = _graph(session, repo_id)
    return _resolve_errors(lambda: cq.path(g, a, b))


@router.get("/repos/{repo_id}/code/query")
def code_query(repo_id: str, q: str, session: Session = Depends(get_session)):
    _, g = _graph(session, repo_id)
    return cq.query(g, q)


class ChangesIn(BaseModel):
    files: list[str] = Field(default_factory=list)
    diff: str | None = Field(default=None, description="Unified diff for line-level impact")
    pr: str | None = Field(default=None, description="GitHub pull request URL")
    depth: int = Field(default=3, ge=1, le=6)


def _pr_changes(url: str) -> dict:
    """Changed files and hunks of a GitHub PR (uses FILAMENT_GIT_TOKEN/GITHUB_TOKEN if set)."""
    import os

    import httpx

    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise HTTPException(422, "pr must look like https://github.com/<owner>/<repo>/pull/<n>")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("FILAMENT_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    out: dict = {}
    for page in range(1, 11):
        api = f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/pulls/{m.group(3)}/files"
        r = httpx.get(api, params={"per_page": 100, "page": page}, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise HTTPException(502, f"GitHub said {r.status_code}: {r.text[:200]}")
        items = r.json()
        for f in items:
            patch = f.get("patch")
            fake = f"diff --git a/x b/x\n+++ b/{f['filename']}\n{patch}"
            out[f["filename"]] = cq.parse_diff(fake).get(f["filename"]) if patch else None
        if len(items) < 100:
            break
    return out


@router.post("/repos/{repo_id}/code/changes")
def code_changes(repo_id: str, body: ChangesIn, session: Session = Depends(get_session)):
    """Impact of a change: changed symbols, what they affect, endpoints, consuming repos."""
    record, g = _graph(session, repo_id)
    changes: dict = {f: None for f in body.files}
    if body.diff:
        changes.update(cq.parse_diff(body.diff))
    if body.pr:
        changes.update(_pr_changes(body.pr))
    if not changes:
        raise HTTPException(422, "Pass files, a diff or a pr URL")
    code = record.analysis.get("code") or {}
    system = service.system_for(session, record.workspace_id)
    return cq.change_impact(g, code.get("endpoints") or {}, changes,
                            system["consumers"].get(record.id, {}),
                            {h["id"] for h in code.get("hotspots") or []}, body.depth)


class ConflictsIn(BaseModel):
    changes: list[dict] = Field(description="[{name, files: [...]}] e.g. open pull requests")


@router.post("/repos/{repo_id}/code/conflicts")
def code_conflicts(repo_id: str, body: ConflictsIn, session: Session = Depends(get_session)):
    _, g = _graph(session, repo_id)
    return cq.conflicts(g, body.changes)


@router.get("/repos/{repo_id}/data-model")
def data_model(repo_id: str, session: Session = Depends(get_session)):
    record = get_or_404(session, RepoRecord, repo_id)
    return (record.analysis or {}).get("data_model") or {"entities": [], "relations": [],
                                                         "mermaid": ""}
