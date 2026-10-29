"""Shared memory, Ask Filament and demand/gap detection."""

import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from filament import schemas as s
from filament.api.graph import get_or_404
from filament.context import keywords
from filament.db import get_session
from filament.llm import complete
from filament.models import Artifact, MemoryEntry, Project, Signal, Workspace

router = APIRouter()

STOPWORDS = {"the", "and", "for", "with", "how", "what", "why", "who", "does", "our", "are",
             "this", "that", "from", "use", "can", "did", "was", "which", "when", "where", "have"}


@router.post("/workspaces/{ws_id}/memory", response_model=s.MemoryOut, status_code=201)
def add_memory(ws_id: str, body: s.MemoryIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
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
    return session.scalars(stmt.order_by(MemoryEntry.created_at.desc())).all()


@router.post("/memory/{entry_id}/{action}", response_model=s.MemoryOut)
def change_memory_status(entry_id: str, action: str, session: Session = Depends(get_session)):
    target = {"confirm": "confirmed", "retire": "retired"}.get(action)
    if target is None:
        raise HTTPException(400, "action must be 'confirm' or 'retire'")
    entry = get_or_404(session, MemoryEntry, entry_id)
    entry.status = target
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
    for a in session.scalars(art_stmt):
        score = len(q & keywords(a.title + " " + a.content))
        if score:
            candidates.append((score, f"artifact:{a.id}", a.title, a.content[:1500]))
    mem_stmt = select(MemoryEntry).where(MemoryEntry.workspace_id == ws_id,
                                         MemoryEntry.status != "retired")
    for m in session.scalars(mem_stmt):
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
    answered = bool(hits)
    session.add(Signal(workspace_id=ws_id, project_id=project_id,
                       kind="question" if answered else "no_answer",
                       text=body.question, asker=body.asker, answered=int(answered)))
    session.commit()
    if not hits:
        return {"answer": "Nothing in Filament answers this yet. It has been logged as a gap.",
                "citations": [], "answered": False}

    context = "\n\n".join(f"[{ref}] {title}\n{text}" for _, ref, title, text in hits)
    result = complete(
        "You answer questions for a software team using only the provided context. Cite the "
        "bracketed references you used. If the context is insufficient, say so. Reply as JSON: "
        '{"answer": str, "citations": [str]}',
        f"## Context\n{context}\n\n## Question\n{body.question}",
        offline_response=lambda: json.dumps({
            "answer": "Most relevant: " + "; ".join(t for _, _, t, _ in hits[:3]),
            "citations": [ref for _, ref, _, _ in hits[:3]],
        }),
    )
    try:
        parsed = json.loads(result.text[result.text.find("{"): result.text.rfind("}") + 1])
        return {"answer": parsed.get("answer", ""), "citations": parsed.get("citations", []),
                "answered": True}
    except (json.JSONDecodeError, ValueError):
        return {"answer": result.text, "citations": [r for _, r, _, _ in hits], "answered": True}


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
