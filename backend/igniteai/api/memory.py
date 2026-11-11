"""Shared memory, Ask Ignite and demand/gap detection."""

import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from igniteai import access
from igniteai import schemas as s
from igniteai.api.graph import get_or_404
from igniteai.context import keywords
from igniteai.db import get_session
from igniteai.llm import complete
from igniteai.models import Artifact, MemoryEntry, Project, Signal, Workspace

router = APIRouter()

STOPWORDS = {"the", "and", "for", "with", "how", "what", "why", "who", "does", "our", "are",
             "this", "that", "from", "use", "can", "did", "was", "which", "when", "where", "have"}


@router.post("/workspaces/{ws_id}/memory", response_model=s.MemoryOut, status_code=201)
def add_memory(ws_id: str, body: s.MemoryIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    p = access.current()
    if body.scope == "project" and body.scope_ref:
        proj = get_or_404(session, Project, body.scope_ref)
        access.require(session, p, access.Target(ws_id, proj), "contributor")
    elif body.scope == "team" and not access.can_see_memory(session, p, MemoryEntry(
            workspace_id=ws_id, scope="team", scope_ref=body.scope_ref)):
        raise HTTPException(403, "Only members of that team can add to its memory")
    elif body.scope == "org" and not p.unrestricted and not access.is_ws_admin(session, p, ws_id) \
            and not body.author.startswith("agent:"):
        # Org-wide memory is what every agent reads first; people add it through review.
        body.author = body.author if body.author != "human" else access.actor()
        entry = MemoryEntry(workspace_id=ws_id, status="proposed", **body.model_dump())
        session.add(entry)
        session.commit()
        return entry
    if p.user_id and not body.author.startswith("agent:"):
        body.author = access.actor()
    # Human-written entries are confirmed; agent-written ones start as proposed.
    status = "proposed" if body.author.startswith("agent:") else "confirmed"
    entry = MemoryEntry(workspace_id=ws_id, status=status, **body.model_dump())
    session.add(entry)
    session.commit()
    return entry


@router.get("/workspaces/{ws_id}/memory", response_model=list[s.MemoryOut])
def list_memory(ws_id: str, scope: str | None = None, status: str | None = None,
                session: Session = Depends(get_session)):
    stmt = select(MemoryEntry).where(MemoryEntry.workspace_id == ws_id)
    if scope:
        stmt = stmt.where(MemoryEntry.scope == scope)
    if status:
        stmt = stmt.where(MemoryEntry.status == status)
    return access.filter_memory(session, session.scalars(
        stmt.order_by(MemoryEntry.created_at.desc())).all())


@router.post("/memory/{entry_id}/{action}", response_model=s.MemoryOut)
def change_memory_status(entry_id: str, action: str, session: Session = Depends(get_session)):
    target = {"confirm": "confirmed", "retire": "retired"}.get(action)
    if target is None:
        raise HTTPException(400, "action must be 'confirm' or 'retire'")
    entry = get_or_404(session, MemoryEntry, entry_id)
    p = access.current()
    if not access.can_see_memory(session, p, entry):
        raise HTTPException(404, "Not found")
    if entry.scope == "org" and not p.unrestricted \
            and not access.is_ws_admin(session, p, entry.workspace_id):
        raise HTTPException(403, "Only a workspace admin can confirm or retire org-wide memory")
    if entry.scope == "project" and entry.scope_ref:
        proj = session.get(Project, entry.scope_ref)
        if proj and not access.can(session, p, proj, "maintainer"):
            raise HTTPException(403, "A project maintainer confirms or retires its memory")
    entry.status = target
    access.audit(session, f"memory.{action}", workspace_id=entry.workspace_id,
                 resource=entry.id)
    session.commit()
    return entry


def _search(session: Session, ws_id: str, project_id: str | None, question: str, limit: int = 8):
    q = keywords(question) - STOPWORDS
    if not q:
        return []
    art_stmt = select(Artifact).join(Project).where(Project.workspace_id == ws_id,
                                                     Artifact.status != "rejected")
    if project_id:
        art_stmt = art_stmt.where(Artifact.project_id == project_id)
    candidates: list[tuple[int, str, str, str]] = []
    p = access.current()
    ok_projects = {x.id for x in access.visible_projects(session, p, session.scalars(
        select(Project).where(Project.workspace_id == ws_id)))}
    for a in session.scalars(art_stmt):
        if a.project_id not in ok_projects:
            continue
        score = len(q & keywords(a.title + " " + a.content))
        if score:
            candidates.append((score, f"artifact:{a.id}", a.title, a.content[:1500]))
    mem_stmt = select(MemoryEntry).where(MemoryEntry.workspace_id == ws_id,
                                         MemoryEntry.status != "retired")
    for m in access.filter_memory(session, session.scalars(mem_stmt).all()):
        score = len(q & keywords(m.content))
        if score:
            candidates.append((score + 1, f"memory:{m.id}", f"{m.scope} {m.kind}", m.content))
    candidates.sort(key=lambda c: c[0], reverse=True)
    # Require at least two matching keywords for multi-word questions to count as an answer.
    min_score = 2 if len(q) > 2 else 1
    return [c for c in candidates if c[0] >= min_score][:limit]


@router.post("/workspaces/{ws_id}/ask", response_model=s.AskOut)
def ask(ws_id: str, body: s.AskIn, project_id: str | None = None,
        session: Session = Depends(get_session)):
    """Answer from memory and the graph with citations; unanswered questions feed gap detection."""
    get_or_404(session, Workspace, ws_id)
    hits = _search(session, ws_id, project_id, body.question)
    sources: dict[str, dict] = {}
    from igniteai import features
    from igniteai.knowledge import service as ks

    p = access.current()
    if features.allowed(session, p, ws_id, "knowledge"):
        found = ks.search(session, ws_id, body.question, p=p, limit=5)
        ks.record_hits(session, found)
        for k in found:
            ref = f"knowledge:{k['id']}"
            sources[ref] = {"title": k["title"], "heading": k["heading"], "url": k["url"],
                            "source": k["source"]}
            hits.append((k["score"], ref, k["heading"] or k["title"], k["text"]))
    for _, ref, title, _ in hits:
        sources.setdefault(ref, {"title": title[:200], "heading": "", "url": None,
                                 "source": "Shared memory" if ref.startswith("memory:")
                                 else "Project graph"})
    answered = bool(hits)
    session.add(Signal(workspace_id=ws_id, project_id=project_id,
                       kind="question" if answered else "no_answer",
                       text=body.question, asker=body.asker, answered=int(answered)))
    session.commit()
    if not hits:
        return {"answer": "Nothing in Ignite answers this yet. It has been logged as a gap.",
                "citations": [], "answered": False}

    context = "\n\n".join(f"[{ref}] {title}\n{text}" for _, ref, title, text in hits)
    result = complete(
        "You answer questions for a software team using only the provided context. Cite the "
        "bracketed references you used. If the context is insufficient, say so. Reply as JSON: "
        '{"answer": str, "citations": [str]}',
        f"## Context\n{context}\n\n## Question\n{body.question}",
        offline_response=lambda: json.dumps({
            "answer": "No AI model is connected (offline mode), so here is what Ignite found "
                      "rather than a written answer:\n\n" + "\n\n".join(
                          f"[{ref}] {title}: {_excerpt(text, body.question)}"
                          for _, ref, title, text in sorted(hits, key=lambda h: -h[0])[:3]),
            "citations": [ref for _, ref, _, _ in sorted(hits, key=lambda h: -h[0])[:3]],
        }),
    )
    try:
        parsed = json.loads(result.text[result.text.find("{"): result.text.rfind("}") + 1])
        cites = parsed.get("citations", [])
        return {"answer": parsed.get("answer", ""), "citations": cites, "answered": True,
                "sources": {c: sources[c] for c in cites if c in sources}}
    except (json.JSONDecodeError, ValueError):
        return {"answer": result.text, "citations": [r for _, r, _, _ in hits], "answered": True,
                "sources": sources}


def _excerpt(text: str, question: str, size: int = 400) -> str:
    """The part of a text that best matches the question (for offline answers)."""
    q = keywords(question) - STOPWORDS
    sentences = [s.strip() for s in text.replace("\n", " ").split(". ") if s.strip()]
    if not sentences:
        return ""
    best = max(range(len(sentences)), key=lambda i: len(q & keywords(sentences[i])))
    out = ". ".join(sentences[best:best + 3])
    return out[:size] + ("…" if len(out) > size else "")


@router.get("/workspaces/{ws_id}/gaps", response_model=list[s.GapOut])
def gaps(ws_id: str, min_count: int = 1, session: Session = Depends(get_session)):
    """Cluster unanswered questions and context misses by their most common keyword."""
    signals = session.scalars(select(Signal).where(
        Signal.workspace_id == ws_id,
        or_(Signal.kind == "no_answer", Signal.kind == "context_miss"))).all()
    freq = Counter(k for sig in signals for k in keywords(sig.text) - STOPWORDS)
    clusters: dict[str, list[str]] = {}
    for sig in signals:
        kws = keywords(sig.text) - STOPWORDS
        topic = max(kws, key=lambda k: (freq[k], k)) if kws else "other"
        clusters.setdefault(topic, []).append(sig.text)
    out = [{"topic": t, "count": len(v), "examples": v[:5]}
           for t, v in clusters.items() if len(v) >= min_count]
    return sorted(out, key=lambda g: g["count"], reverse=True)
