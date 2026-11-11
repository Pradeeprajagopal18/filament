"""Knowledge sources (admins) and knowledge search (anyone with the Knowledge feature)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from igniteai import access
from igniteai.api.graph import get_or_404
from igniteai.db import get_session
from igniteai.knowledge import embed as emb
from igniteai.knowledge import service as ks
from igniteai.knowledge.connectors import ConnectorError, connector
from igniteai.models import (
    KnowledgeDoc,
    KnowledgeIndex,
    KnowledgeSource,
    Project,
    RepoRecord,
    Team,
    User,
    Workspace,
)

router = APIRouter()
SECRET_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
MAX_UPLOAD = 25 * 1024 * 1024


class SourceIn(BaseModel):
    name: str
    kind: str
    config: dict = {}
    audience: str = "everyone"
    teams: list[str] = []
    groups: list[str] = []
    users: list[str] = []
    project_id: str | None = None
    agents: list[str] = []
    sync_hours: int = 24
    enabled: bool = True


class SourcePatch(BaseModel):
    name: str | None = None
    config: dict | None = None
    audience: str | None = None
    teams: list[str] | None = None
    groups: list[str] | None = None
    users: list[str] | None = None
    project_id: str | None = Field(default=None, description="'' clears it")
    agents: list[str] | None = None
    sync_hours: int | None = None
    enabled: bool | None = None


def _out(s: KnowledgeSource) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "config": s.config,
            "audience": s.audience, "teams": s.teams, "groups": s.groups, "users": s.users,
            "project_id": s.project_id, "agents": s.agents, "enabled": s.enabled,
            "sync_hours": s.sync_hours, "status": s.status, "last_sync_at": s.last_sync_at,
            "next_sync_at": s.next_sync_at, "last_error": s.last_error,
            "last_report": s.last_report, "doc_count": s.doc_count,
            "chunk_count": s.chunk_count, "hits": s.hits, "created_by": s.created_by}


def _validate(session: Session, ws_id: str, kind: str, config: dict, body) -> None:
    from igniteai.agents import REGISTRY

    if kind not in ks.KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(ks.KINDS)}")
    for key in ("token_ref",):
        val = config.get(key)
        if val and not SECRET_REF.match(str(val)):
            raise HTTPException(422, "Don't paste the token itself: set it on the server and "
                                     "enter a reference such as ${env:CONFLUENCE_TOKEN}")
    if kind == "repo":
        ids = config.get("repo_ids") or []
        found = {r.id for r in session.scalars(select(RepoRecord).where(
            RepoRecord.id.in_(ids), RepoRecord.workspace_id == ws_id))}
        if not ids or set(ids) - found:
            raise HTTPException(422, "Choose onboarded repos from this workspace")
    if getattr(body, "audience", None) not in (None, "everyone", "some"):
        raise HTTPException(422, "audience must be everyone or some")
    teams = {t.id for t in session.scalars(select(Team).where(Team.workspace_id == ws_id))}
    if set(getattr(body, "teams", None) or []) - teams:
        raise HTTPException(422, "Unknown team")
    if any(session.get(User, u) is None for u in getattr(body, "users", None) or []):
        raise HTTPException(422, "Unknown person")
    if set(getattr(body, "agents", None) or []) - set(REGISTRY):
        raise HTTPException(422, f"Unknown agent; choose from {sorted(REGISTRY)}")
    pid = getattr(body, "project_id", None)
    if pid:
        proj = session.get(Project, pid)
        if proj is None or proj.workspace_id != ws_id:
            raise HTTPException(422, "Unknown project")
    if (getattr(body, "sync_hours", None) or 0) < 0:
        raise HTTPException(422, "sync_hours can't be negative")


def _try_connector(session: Session, source: KnowledgeSource) -> None:
    if source.kind == "upload":
        return
    try:
        connector(source, session)
    except ConnectorError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/workspaces/{ws_id}/knowledge")
def overview(ws_id: str, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    sources = session.scalars(select(KnowledgeSource).where(
        KnowledgeSource.workspace_id == ws_id).order_by(KnowledgeSource.created_at))
    return {"sources": [_out(s) for s in sources], "index": _index_out(session, ws_id)}


@router.post("/workspaces/{ws_id}/knowledge/sources", status_code=201)
def create_source(ws_id: str, body: SourceIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    _validate(session, ws_id, body.kind, body.config, body)
    s = KnowledgeSource(workspace_id=ws_id, created_by=access.actor(), status="new",
                        **body.model_dump())
    if s.kind == "upload":
        s.sync_hours = 0
    _try_connector(session, s)
    session.add(s)
    session.flush()
    access.audit(session, "knowledge.add", workspace_id=ws_id, resource=s.id, kind=s.kind,
                 name=s.name)
    session.commit()
    return _out(s)


@router.get("/knowledge/{source_id}")
def get_source(source_id: str, session: Session = Depends(get_session)):
    s = get_or_404(session, KnowledgeSource, source_id)
    docs = session.scalars(select(KnowledgeDoc).where(KnowledgeDoc.source_id == s.id)
                           .order_by(KnowledgeDoc.title).limit(500))
    return {**_out(s), "docs": [{"id": d.id, "title": d.title, "url": d.url,
                                  "passages": d.chunk_count, "updated_at": d.updated_at}
                                 for d in docs]}


@router.patch("/knowledge/{source_id}")
def update_source(source_id: str, body: SourcePatch, session: Session = Depends(get_session)):
    s = get_or_404(session, KnowledgeSource, source_id)
    _validate(session, s.workspace_id, s.kind, body.config or s.config, body)
    changes = body.model_dump(exclude_none=True)
    if changes.get("project_id") == "":
        changes["project_id"] = None
    for k, v in changes.items():
        setattr(s, k, v)
    if body.config is not None:
        _try_connector(session, s)
    if body.sync_hours is not None:
        s.next_sync_at = None
    access.audit(session, "knowledge.update", workspace_id=s.workspace_id, resource=s.id,
                 change=list(changes))
    session.commit()
    ks.invalidate(s.workspace_id)
    return _out(s)


@router.delete("/knowledge/{source_id}", status_code=204)
def delete_source(source_id: str, session: Session = Depends(get_session)):
    s = get_or_404(session, KnowledgeSource, source_id)
    access.audit(session, "knowledge.delete", workspace_id=s.workspace_id, resource=s.id,
                 name=s.name)
    ks.delete_source(session, s)


@router.post("/knowledge/{source_id}/sync")
def sync_source(source_id: str, full: bool = False, wait: bool = False,
                session: Session = Depends(get_session)):
    """Sync now. By default it runs in the background (big spaces take a while); wait=true
    returns the report."""
    s = get_or_404(session, KnowledgeSource, source_id)
    if s.status == "syncing" and not wait:
        return {"status": "syncing"}
    access.audit(session, "knowledge.sync", workspace_id=s.workspace_id, resource=s.id)
    session.commit()
    if wait:
        return {"status": "done", "report": ks.sync(session, s, full)}
    s.status = "syncing"
    session.commit()
    ks.sync_in_background(s.id, session.get_bind(), full)
    return {"status": "syncing"}


@router.post("/knowledge/{source_id}/test")
def test_source(source_id: str, session: Session = Depends(get_session)):
    """Connect and list the first few documents, without saving anything."""
    s = get_or_404(session, KnowledgeSource, source_id)
    if s.kind == "upload":
        return {"ok": True, "message": "Uploaded files don't need a connection", "sample": []}
    try:
        conn = connector(s, session)
        sample = []
        for item in conn.list():
            sample.append({"title": item.title, "url": item.url})
            if len(sample) >= 5:
                break
    except ConnectorError as exc:
        return {"ok": False, "message": str(exc), "sample": []}
    if not sample:
        return {"ok": False, "message": "Connected, but found no documents (check the space "
                                        "keys, URLs or file patterns)", "sample": []}
    return {"ok": True, "message": f"Connected. First {len(sample)} documents:",
            "sample": sample}


@router.post("/knowledge/{source_id}/files")
async def upload(source_id: str, files: list[UploadFile] = File(...),
                 session: Session = Depends(get_session)):
    s = get_or_404(session, KnowledgeSource, source_id)
    if s.kind != "upload":
        raise HTTPException(422, "Files go into an 'Uploaded files' source")
    results = []
    for f in files:
        data = await f.read(MAX_UPLOAD + 1)
        name = (f.filename or "file").replace("\\", "/").rsplit("/", 1)[-1]
        if len(data) > MAX_UPLOAD:
            results.append({"file": name, "error": "Larger than 25 MB"})
            continue
        try:
            results.append(ks.add_file(session, s, name, data))
        except (ValueError, emb.EmbedError) as exc:
            session.rollback()
            results.append({"file": name, "error": str(exc)})
    access.audit(session, "knowledge.upload", workspace_id=s.workspace_id, resource=s.id,
                 files=[r["file"] for r in results])
    session.commit()
    return {"results": results, "source": _out(s)}


@router.delete("/knowledge/{source_id}/docs/{doc_id}", status_code=204)
def delete_doc(source_id: str, doc_id: str, session: Session = Depends(get_session)):
    doc = get_or_404(session, KnowledgeDoc, doc_id)
    if doc.source_id != source_id:
        raise HTTPException(404, "Not found")
    ks.remove_doc(session, doc)


# -- search ---------------------------------------------------------------------------------------
class SearchIn(BaseModel):
    query: str
    limit: int = 6
    source_ids: list[str] | None = None


@router.post("/workspaces/{ws_id}/knowledge/search")
def search(ws_id: str, body: SearchIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    return {"results": ks.search(session, ws_id, body.query, limit=min(body.limit, 20),
                                 source_ids=body.source_ids)}


# -- embedding settings ---------------------------------------------------------------------------
class IndexIn(BaseModel):
    provider: str
    model: str | None = None
    model_path: str | None = None
    endpoint_url: str | None = None
    api_key_ref: str | None = None


def _index_out(session: Session, ws_id: str) -> dict:
    i = session.get(KnowledgeIndex, ws_id)
    if i is None:
        return {"provider": "none", "model": "BAAI/bge-small-en-v1.5", "model_path": None,
                "endpoint_url": None, "api_key_ref": None, "fastembed": _has_fastembed()}
    return {"provider": i.provider, "model": i.model, "model_path": i.model_path,
            "endpoint_url": i.endpoint_url, "api_key_ref": i.api_key_ref,
            "updated_by": i.updated_by, "fastembed": _has_fastembed()}


def _has_fastembed() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def _check_index(body: IndexIn) -> None:
    if body.provider not in {"none", "local", "endpoint"}:
        raise HTTPException(422, "provider must be none, local or endpoint")
    if body.provider == "endpoint" and not (body.endpoint_url or "").startswith(
            ("http://", "https://")):
        raise HTTPException(422, "Give the embedding server's URL, e.g. "
                                 "http://ollama.internal:11434/v1")
    if body.api_key_ref and not SECRET_REF.match(body.api_key_ref):
        raise HTTPException(422, "The key must be a reference such as ${env:EMBEDDINGS_KEY}")


@router.post("/workspaces/{ws_id}/knowledge/index/test")
def test_index(ws_id: str, body: IndexIn):
    _check_index(body)
    probe = KnowledgeIndex(workspace_id=ws_id, provider=body.provider,
                           model=body.model or "BAAI/bge-small-en-v1.5",
                           model_path=body.model_path, endpoint_url=body.endpoint_url,
                           api_key_ref=body.api_key_ref)
    try:
        e = emb.for_index(probe)
        if e is None:
            return {"ok": True, "message": "Keyword search only; nothing to test"}
        vec = e.embed(["How do we roll back a release?"])[0]
    except emb.EmbedError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Working: {e.name} gives {len(vec)}-number vectors"}


@router.put("/workspaces/{ws_id}/knowledge/index")
def set_index(ws_id: str, body: IndexIn, reembed: bool = True,
              session: Session = Depends(get_session)):
    """Change how passages are turned into vectors. Existing passages get new vectors (in the
    background) so search keeps working."""
    get_or_404(session, Workspace, ws_id)
    _check_index(body)
    i = session.get(KnowledgeIndex, ws_id) or KnowledgeIndex(workspace_id=ws_id)
    session.add(i)
    before = (i.provider, i.model, i.endpoint_url, i.model_path)
    i.provider = body.provider
    i.model = body.model or i.model or "BAAI/bge-small-en-v1.5"
    i.model_path, i.endpoint_url, i.api_key_ref = body.model_path, body.endpoint_url, \
        body.api_key_ref
    i.updated_by = access.actor()
    access.audit(session, "knowledge.index", workspace_id=ws_id, resource=ws_id,
                 provider=i.provider, model=i.model)
    session.commit()
    changed = before != (i.provider, i.model, i.endpoint_url, i.model_path)
    if changed and reembed:
        bind = session.get_bind()

        def run():
            from sqlalchemy.orm import sessionmaker

            with sessionmaker(bind=bind, expire_on_commit=False)() as s:
                try:
                    ks.reembed_workspace(s, ws_id)
                except emb.EmbedError as exc:
                    ks.log.warning("re-embedding failed: %s", exc)

        import threading

        threading.Thread(target=run, daemon=True).start()
    return {**_index_out(session, ws_id), "reembedding": changed and reembed}
