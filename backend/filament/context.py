"""Context engine: builds a budgeted context pack for each agent run.

The pack combines the target artifacts, their graph neighbours (1-2 hops) and relevant
shared-memory entries. It is stored on the run so every output can be explained.
"""

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.llm import approx_tokens
from filament.models import Artifact, Link, MemoryEntry, Project

_WORD = re.compile(r"[a-z0-9]{3,}")


def keywords(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def neighbours(session: Session, artifact_ids: list[str], hops: int = 2) -> list[str]:
    """Artifact ids reachable from the given ids within `hops` edges (either direction)."""
    seen = set(artifact_ids)
    frontier = set(artifact_ids)
    for _ in range(hops):
        if not frontier:
            break
        links = session.scalars(
            select(Link).where(or_(Link.source_id.in_(frontier), Link.target_id.in_(frontier)))
        ).all()
        nxt = {lk.target_id for lk in links} | {lk.source_id for lk in links}
        frontier = nxt - seen
        seen |= nxt
    return [i for i in seen if i not in artifact_ids]


def relevant_memory(session: Session, project: Project, query: str, limit: int = 20):
    """Memory visible to this project (org, its team, the project), ranked by keyword overlap.

    Retired entries are excluded; proposed entries are included but marked as such.
    """
    scopes = [MemoryEntry.scope == "org"]
    if project.team_id:
        scopes.append((MemoryEntry.scope == "team") & (MemoryEntry.scope_ref == project.team_id))
    scopes.append((MemoryEntry.scope == "project") & (MemoryEntry.scope_ref == project.id))
    entries = session.scalars(
        select(MemoryEntry).where(
            MemoryEntry.workspace_id == project.workspace_id,
            MemoryEntry.status != "retired",
            or_(*scopes),
        )
    ).all()
    q = keywords(query)

    def score(e: MemoryEntry) -> float:
        overlap = len(q & keywords(e.content))
        boost = 1.0 if e.status == "confirmed" else 0.5
        standard = 2 if e.kind == "standard" else 0  # standards always matter to agents
        return (overlap + standard) * boost

    ranked = sorted(entries, key=score, reverse=True)
    return [e for e in ranked if score(e) > 0][:limit]


def build_context_pack(
    session: Session, project: Project, goal: str, artifact_ids: list[str] | None = None,
    budget: int | None = None,
) -> dict:
    budget = budget or get_settings().context_budget_tokens
    artifact_ids = artifact_ids or []
    items: list[dict] = []
    used = 0

    def add(item: dict) -> bool:
        nonlocal used
        cost = approx_tokens(item["text"])
        if used + cost > budget:
            return False
        items.append(item)
        used += cost
        return True

    targets = session.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids))).all()
    for a in targets:
        add({"kind": "target", "id": a.id, "type": a.type, "title": a.title, "text": a.content})

    for nid in neighbours(session, artifact_ids):
        a = session.get(Artifact, nid)
        if a:
            add({"kind": "linked", "id": a.id, "type": a.type, "title": a.title,
                 "text": a.content[:2000]})

    query = goal + " " + " ".join(a.title + " " + a.content[:500] for a in targets)
    for m in relevant_memory(session, project, query):
        add({"kind": "memory", "id": m.id, "type": m.kind, "title": f"{m.scope} {m.kind}",
             "text": m.content + (" (proposed)" if m.status == "proposed" else "")})

    return {"items": items, "approx_tokens": used, "budget": budget}


def render_context(pack: dict) -> str:
    if not pack["items"]:
        return "(no prior context)"
    lines = []
    for it in pack["items"]:
        lines.append(f"[{it['kind']}:{it['type']}:{it['id']}] {it['title']}\n{it['text']}".strip())
    return "\n\n".join(lines)
