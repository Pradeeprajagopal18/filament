"""Knowledge: sync sources into passages, and find the right passages for a question.

Search is hybrid: keyword ranking (BM25, always on) plus meaning (cosine over embeddings, when a
local model or embedding server is set up), merged by reciprocal rank. Results are filtered to
what the person asking may read, and, for agents, to sources that agent may use.

Everything is stored in Ignite's own database. Vectors are kept per passage; search loads a
workspace's index into memory once and refreshes it when a sync changes something.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from igniteai.knowledge import embed as emb
from igniteai.knowledge.connectors import ConnectorError, Item, connector
from igniteai.knowledge.text import Section, chunk, file_sections, terms
from igniteai.models import KnowledgeChunk, KnowledgeDoc, KnowledgeIndex, KnowledgeSource

log = logging.getLogger(__name__)
KINDS = {"confluence", "web", "upload", "repo"}


# -- access ---------------------------------------------------------------------------------------
def can_read(session: Session, p, source: KnowledgeSource, agent: str | None = None,
             doc: KnowledgeDoc | None = None) -> bool:
    from igniteai import access
    from igniteai.models import Membership, Project, RepoRecord, User

    if not source.enabled:
        return False
    if agent and source.agents and agent not in source.agents:
        return False
    if not p.unrestricted and not access.is_ws_admin(session, p, source.workspace_id):
        if access.workspace_role(session, p, source.workspace_id) is None:
            return False
        if source.project_id:
            proj = session.get(Project, source.project_id)
            if proj is None or not access.can(session, p, proj):
                return False
        if source.audience == "some":
            user = session.get(User, p.user_id) if p.user_id else None
            if user is None:
                return False
            teams = {m.scope_id for m in session.scalars(select(Membership).where(
                Membership.user_id == user.id, Membership.scope == "team"))}
            groups = {g.lower() for g in (user.groups or [])}
            if not (user.id in source.users or teams & set(source.teams)
                    or groups & {g.lower() for g in source.groups}):
                return False
    if doc is not None and doc.meta.get("repo_id"):  # repo docs follow the repo's visibility
        repo = session.get(RepoRecord, doc.meta["repo_id"])
        return repo is not None and (p.unrestricted or access.can_see_repo(session, p, repo))
    return True


# -- indexing -------------------------------------------------------------------------------------
def index_settings(session: Session, ws_id: str) -> KnowledgeIndex | None:
    return session.get(KnowledgeIndex, ws_id)


def _embedder(session: Session, ws_id: str):
    return emb.for_index(index_settings(session, ws_id))


def _write_doc(session: Session, source: KnowledgeSource, item: Item, title: str,
               sections: list[Section], embedder) -> int:
    doc = session.scalar(select(KnowledgeDoc).where(KnowledgeDoc.source_id == source.id,
                                                    KnowledgeDoc.external_id == item.external_id))
    if doc is None:
        doc = KnowledgeDoc(source_id=source.id, external_id=item.external_id)
        session.add(doc)
        session.flush()
    doc.title, doc.url, doc.version, doc.meta = title[:500], item.url, item.version, item.meta
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
    passages = chunk(title, sections)
    vectors = embedder.embed([f"{h}\n{t}" for h, t in passages]) if embedder and passages \
        else [None] * len(passages)
    for i, ((heading, text), vec) in enumerate(zip(passages, vectors, strict=True)):
        tf = terms(f"{heading} {text}")
        session.add(KnowledgeChunk(workspace_id=source.workspace_id, source_id=source.id,
                                   doc_id=doc.id, ord=i, heading=heading[:500], text=text,
                                   terms=tf, length=sum(tf.values()), embedding=vec,
                                   model=embedder.name if embedder and vec else None))
    doc.chunk_count = len(passages)
    return len(passages)


def _counts(session: Session, source: KnowledgeSource) -> None:
    source.doc_count = session.scalar(select(func.count()).select_from(KnowledgeDoc).where(
        KnowledgeDoc.source_id == source.id)) or 0
    source.chunk_count = session.scalar(select(func.count()).select_from(KnowledgeChunk).where(
        KnowledgeChunk.source_id == source.id)) or 0


def sync(session: Session, source: KnowledgeSource, full: bool = False) -> dict:
    """Bring a source up to date: add new documents, re-read changed ones, drop deleted ones."""
    report = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "failed": [],
              "passages": 0}
    source.status, source.last_error = "syncing", None
    session.commit()
    try:
        if source.kind == "upload":
            if full:
                embedder = _embedder(session, source.workspace_id)
                for doc in session.scalars(select(KnowledgeDoc).where(
                        KnowledgeDoc.source_id == source.id)):
                    _reembed(session, doc, embedder)
            report["unchanged"] = source.doc_count
        else:
            conn = connector(source, session)
            embedder = _embedder(session, source.workspace_id)
            known = {d.external_id: d for d in session.scalars(select(KnowledgeDoc).where(
                KnowledgeDoc.source_id == source.id))}
            seen: set[str] = set()
            for item in conn.list():
                seen.add(item.external_id)
                old = known.get(item.external_id)
                if old is not None and old.version == item.version and not full:
                    report["unchanged"] += 1
                    continue
                try:
                    title, sections = conn.read(item)
                    report["passages"] += _write_doc(session, source, item, title, sections,
                                                     embedder)
                    report["updated" if old else "added"] += 1
                except (ConnectorError, ValueError, emb.EmbedError) as exc:
                    report["failed"].append({"title": item.title, "error": str(exc)[:300]})
                session.commit()
            for ext_id, doc in known.items():
                if ext_id not in seen:
                    session.execute(delete(KnowledgeChunk).where(
                        KnowledgeChunk.doc_id == doc.id))
                    session.delete(doc)
                    report["removed"] += 1
        source.status = "error" if report["failed"] and not (
            report["added"] or report["updated"] or report["unchanged"]) else "ok"
        if report["failed"]:
            source.last_error = f"{len(report['failed'])} documents failed: " \
                                f"{report['failed'][0]['error']}"
    except (ConnectorError, emb.EmbedError) as exc:
        session.rollback()
        source = session.get(KnowledgeSource, source.id)
        source.status, source.last_error = "error", str(exc)[:1000]
    now = datetime.now(UTC)
    source.last_sync_at = now
    source.next_sync_at = now + timedelta(hours=source.sync_hours) if source.sync_hours else None
    source.last_report = report
    session.flush()  # deletions above must count
    _counts(session, source)
    session.commit()
    invalidate(source.workspace_id)
    return report


def _reembed(session: Session, doc: KnowledgeDoc, embedder) -> None:
    rows = list(session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id)))
    vecs = embedder.embed([f"{c.heading}\n{c.text}" for c in rows]) if embedder and rows \
        else [None] * len(rows)
    for c, v in zip(rows, vecs, strict=True):
        c.embedding, c.model = v, (embedder.name if embedder and v else None)


def add_file(session: Session, source: KnowledgeSource, name: str, data: bytes) -> dict:
    title, sections = file_sections(name, data)
    if not sections:
        raise ValueError(f"No text found in {name}")
    import hashlib

    item = Item(external_id=name, title=title, url=None,
                version=hashlib.sha1(data).hexdigest()[:16], meta={"file": name})
    n = _write_doc(session, source, item, title, sections, _embedder(session, source.workspace_id))
    source.status, source.last_sync_at = "ok", datetime.now(UTC)
    _counts(session, source)
    session.commit()
    invalidate(source.workspace_id)
    return {"file": name, "title": title, "passages": n}


def remove_doc(session: Session, doc: KnowledgeDoc) -> None:
    source = session.get(KnowledgeSource, doc.source_id)
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
    session.delete(doc)
    session.flush()
    _counts(session, source)
    session.commit()
    invalidate(source.workspace_id)


def delete_source(session: Session, source: KnowledgeSource) -> None:
    ws = source.workspace_id
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id))
    session.execute(delete(KnowledgeDoc).where(KnowledgeDoc.source_id == source.id))
    session.delete(source)
    session.commit()
    invalidate(ws)


def reembed_workspace(session: Session, ws_id: str) -> int:
    """After the embedding model changes: new vectors for every passage."""
    embedder = _embedder(session, ws_id)
    n = 0
    for doc in session.scalars(select(KnowledgeDoc).join(KnowledgeSource).where(
            KnowledgeSource.workspace_id == ws_id)):
        _reembed(session, doc, embedder)
        n += doc.chunk_count
        session.commit()
    invalidate(ws_id)
    return n


def sync_due(session: Session) -> int:
    now = datetime.now(UTC)
    due = [s for s in session.scalars(select(KnowledgeSource).where(
        KnowledgeSource.enabled.is_(True), KnowledgeSource.kind != "upload",
        KnowledgeSource.sync_hours > 0, KnowledgeSource.status != "syncing"))
        if s.next_sync_at is None or (s.next_sync_at if s.next_sync_at.tzinfo
                                      else s.next_sync_at.replace(tzinfo=UTC)) <= now]
    for s in due:
        try:
            sync(session, s)
        except Exception:  # noqa: BLE001 - one bad source must not stop the others
            log.exception("knowledge sync failed for %s", s.id)
    return len(due)


def sync_in_background(source_id: str, bind, full: bool = False) -> None:
    from sqlalchemy.orm import sessionmaker

    def run():
        with sessionmaker(bind=bind, expire_on_commit=False)() as s:
            src = s.get(KnowledgeSource, source_id)
            if src is not None:
                sync(s, src, full)

    threading.Thread(target=run, daemon=True, name=f"knowledge-sync-{source_id}").start()


# -- search ---------------------------------------------------------------------------------------
@dataclass
class _Index:
    built: float
    ids: list[str]
    source_of: dict[str, str]
    doc_of: dict[str, str]
    postings: dict[str, list[tuple[int, int]]]
    lengths: list[int]
    avg: float
    vectors: object  # numpy array or None
    model: str | None


_cache: dict[str, _Index] = {}
_lock = threading.Lock()


def invalidate(ws_id: str) -> None:
    with _lock:
        _cache.pop(ws_id, None)


def _index(session: Session, ws_id: str) -> _Index:
    hit = _cache.get(ws_id)
    if hit is not None and time.monotonic() - hit.built < 300:
        return hit
    rows = session.execute(select(KnowledgeChunk.id, KnowledgeChunk.source_id,
                                  KnowledgeChunk.doc_id, KnowledgeChunk.terms,
                                  KnowledgeChunk.length, KnowledgeChunk.embedding,
                                  KnowledgeChunk.model).where(
        KnowledgeChunk.workspace_id == ws_id)).all()
    postings: dict[str, list[tuple[int, int]]] = {}
    lengths, vecs, models = [], [], set()
    for i, r in enumerate(rows):
        for t, c in (r.terms or {}).items():
            postings.setdefault(t, []).append((i, c))
        lengths.append(r.length or 1)
        vecs.append(r.embedding)
        if r.model:
            models.add(r.model)
    vectors, model = None, None
    if rows and len(models) == 1 and all(v is not None for v in vecs):
        import numpy as np

        arr = np.asarray(vecs, dtype="float32")
        arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
        vectors, model = arr, models.pop()
    idx = _Index(time.monotonic(), [r.id for r in rows], {r.id: r.source_id for r in rows},
                 {r.id: r.doc_id for r in rows}, postings, lengths,
                 sum(lengths) / max(1, len(lengths)), vectors, model)
    with _lock:
        _cache[ws_id] = idx
    return idx


def _bm25(idx: _Index, q: dict[str, int], k1: float = 1.2, b: float = 0.75) -> dict[int, float]:
    n = len(idx.ids)
    scores: dict[int, float] = {}
    for t in q:
        plist = idx.postings.get(t)
        if not plist:
            continue
        idf = math.log(1 + (n - len(plist) + 0.5) / (len(plist) + 0.5))
        for i, tf in plist:
            dl = idx.lengths[i]
            scores[i] = scores.get(i, 0.0) + idf * tf * (k1 + 1) / (
                tf + k1 * (1 - b + b * dl / idx.avg))
    return scores


def search(session: Session, ws_id: str, query: str, p=None, agent: str | None = None,
           limit: int = 6, source_ids: list[str] | None = None) -> list[dict]:
    from igniteai import access

    p = p or access.current()
    idx = _index(session, ws_id)
    if not idx.ids or not query.strip():
        return []
    sources = {s.id: s for s in session.scalars(select(KnowledgeSource).where(
        KnowledgeSource.workspace_id == ws_id))}
    readable = {sid for sid, s in sources.items() if can_read(session, p, s, agent)
                and (not source_ids or sid in source_ids)}
    if not readable:
        return []
    ranked: dict[int, float] = {}
    kw = _bm25(idx, terms(query))
    for rank, (i, _) in enumerate(sorted(kw.items(), key=lambda x: -x[1])[:60]):
        ranked[i] = ranked.get(i, 0) + 1 / (60 + rank)
    meaning = False
    if idx.vectors is not None:
        try:
            embedder = _embedder(session, ws_id)
            if embedder is not None and embedder.name == idx.model:
                import numpy as np

                qv = np.asarray(embedder.embed([query])[0], dtype="float32")
                qv /= np.linalg.norm(qv) + 1e-9
                sims = idx.vectors @ qv
                for rank, i in enumerate(np.argsort(-sims)[:60]):
                    if sims[i] > 0.2:
                        ranked[int(i)] = ranked.get(int(i), 0) + 1 / (60 + rank)
                meaning = True
        except emb.EmbedError as exc:
            log.warning("meaning search skipped: %s", exc)
    out: list[dict] = []
    doc_ok: dict[str, bool] = {}
    for i, score in sorted(ranked.items(), key=lambda x: -x[1]):
        cid = idx.ids[i]
        sid = idx.source_of[cid]
        if sid not in readable:
            continue
        did = idx.doc_of[cid]
        if did not in doc_ok:
            doc = session.get(KnowledgeDoc, did)
            doc_ok[did] = doc is not None and (not doc.meta.get("repo_id") or can_read(
                session, p, sources[sid], agent, doc))
        if not doc_ok[did]:
            continue
        c = session.get(KnowledgeChunk, cid)
        doc = session.get(KnowledgeDoc, did)
        if c is None or doc is None:
            continue
        out.append({"id": c.id, "source": sources[sid].name, "source_id": sid,
                    "title": doc.title, "heading": c.heading, "url": doc.url,
                    "text": c.text, "score": round(score * 1000, 2),
                    "match": "keyword+meaning" if meaning and i in kw else
                    ("meaning" if meaning and i not in kw else "keyword")})
        if len(out) >= limit:
            break
    return out


def record_hits(session: Session, results: list[dict]) -> None:
    for sid in {r["source_id"] for r in results}:
        s = session.get(KnowledgeSource, sid)
        if s is not None:
            s.hits = (s.hits or 0) + 1
