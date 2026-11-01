"""Scanning onboarded repos and keeping Filament's references to them current.

Filament doesn't own these repos, so it never writes to them. Instead every scan:
1. re-analyzes the repo and records what changed (RepoScan),
2. refreshes the references Filament holds about it (drafts update in place; approved ones get
   a pending update a person reviews),
3. raises signals for breaking API changes, naming the repos that consume those endpoints.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.models import (
    Artifact,
    MemoryEntry,
    Project,
    ReadinessReport,
    RepoRecord,
    RepoScan,
    Run,
    Signal,
)
from filament.repos import diff as diffmod
from filament.repos import generate as gen
from filament.repos import system as sysmap
from filament.repos.analyze import analyze

log = logging.getLogger("filament.repos")
AUTHOR = "agent:repo-onboarding"


def interval_hours(record: RepoRecord) -> int:
    return get_settings().repo_scan_interval_hours if record.scan_interval_hours is None \
        else record.scan_interval_hours


def schedule_next(record: RepoRecord) -> None:
    hours = interval_hours(record)
    record.next_scan_at = datetime.now(UTC) + timedelta(hours=hours) if hours > 0 else None


def system_for(session: Session, workspace_id: str) -> dict:
    records = session.scalars(select(RepoRecord).where(RepoRecord.workspace_id == workspace_id)
                              .order_by(RepoRecord.name)).all()
    return sysmap.build([sysmap.RepoView(r.id, r.name, r.kinds or [], r.grade, r.analysis or {})
                         for r in records])


def store(session: Session, record: RepoRecord, analysis: dict, trigger: str) -> RepoScan:
    """Save a fresh analysis on the record, with history, reference refresh and signals."""
    previous = record.analysis or None
    changes = diffmod.compare(previous, analysis)
    # Consumers are looked up on the map as it was, before this change removed anything.
    before = system_for(session, record.workspace_id) if changes.get("breaking") else None
    record.analysis = analysis
    record.name = analysis["name"]
    record.kinds = analysis["profile"]["kind_names"]
    record.score = analysis["readiness"]["score"]
    record.grade = analysis["readiness"]["grade"]
    record.commit = analysis.get("commit")
    record.scanned_at = datetime.now(UTC)
    record.last_error = None
    schedule_next(record)
    session.add(ReadinessReport(repo=record.ref, project_id=record.project_id, score=record.score,
                                grade=record.grade, checks=analysis["readiness"]["checks"]))
    changes["references"] = refresh_references(session, record) if record.generated else {}
    if before is not None:
        _breaking_signals(session, record, changes, before)
    scan = RepoScan(repo_id=record.id, trigger=trigger, grade=record.grade, score=record.score,
                    commit=record.commit, changes=changes)
    session.add(scan)
    return scan


def scan_record(session: Session, record: RepoRecord, trigger: str = "manual") -> RepoScan:
    try:
        analysis = analyze(record.ref)
    except Exception as exc:  # clone failures, unreadable repos: keep the last good analysis
        record.last_error = str(exc)[:500]
        schedule_next(record)
        scan = RepoScan(repo_id=record.id, trigger=trigger, status="failed", grade=record.grade,
                        score=record.score, error=record.last_error, changes={})
        session.add(scan)
        return scan
    return store(session, record, analysis, trigger)


def _breaking_signals(session: Session, record: RepoRecord, changes: dict, system: dict) -> None:
    consumers = system["consumers"].get(record.id, {})
    for b in changes["breaking"][:20]:
        key = b["endpoint"]
        users = consumers.get(key) or []
        b["consumers"] = sorted({u["repo"] for u in users})
        who = f" Used by {', '.join(b['consumers'])}." if b["consumers"] else ""
        session.add(Signal(workspace_id=record.workspace_id, project_id=record.project_id,
                           kind="breaking_change",
                           text=f"{record.name}: {key} — {b['change']}.{who}",
                           asker="agent:repo-scanner"))


def refresh_references(session: Session, record: RepoRecord) -> dict:
    """Regenerate each reference Filament holds for this repo from the new analysis."""
    result = {"refreshed": [], "pending_review": [], "unchanged": []}
    for item, art_id in (record.generated or {}).items():
        art = session.get(Artifact, art_id)
        built = gen.build(item, record.analysis)
        if art is None or built is None:
            continue
        title, content, data = built
        data = {**data, "repo": record.ref, "generator": item, "in_repo": False,
                "source_commit": record.commit}
        if content.strip() == (art.content or "").strip():
            result["unchanged"].append(item)
            continue
        if art.status in {"draft", "in_review"}:
            art.title, art.content = title, content
            art.data = {**data, "refreshed_at": datetime.now(UTC).isoformat()}
            art.version += 1
            result["refreshed"].append(item)
        else:  # approved: a person reviews the update before it replaces the reference
            art.data = {**(art.data or {}), "stale": True,
                        "pending_update": {"title": title, "content": content, "data": data,
                                           "at": datetime.now(UTC).isoformat()}}
            result["pending_review"].append(item)
    return result


def accept_update(session: Session, record: RepoRecord, item: str) -> Artifact:
    art = session.get(Artifact, (record.generated or {}).get(item, ""))
    if art is None:
        raise KeyError(item)
    pending = (art.data or {}).get("pending_update")
    if not pending:
        raise ValueError("No pending update for this reference")
    art.title, art.content = pending["title"], pending["content"]
    art.data = {**pending["data"], "accepted_at": datetime.now(UTC).isoformat()}
    art.version += 1
    art.status = "in_review"  # the refreshed reference is re-reviewed, not silently approved
    return art


def generate(session: Session, record: RepoRecord, items: list[str]) -> list[str]:
    """Create (or refresh) references for the given generators; propose conventions."""
    a = record.analysis
    generated = dict(record.generated or {})
    touched = []
    for item in items:
        built = gen.build(item, a)
        if built is None:
            continue
        title, content, data = built
        data = {**data, "repo": record.ref, "generator": item, "in_repo": False,
                "source_commit": record.commit}
        art_type = "api_contract" if item == "api_contract" else "doc"
        existing = session.get(Artifact, generated[item]) if item in generated else None
        if existing:
            existing.title, existing.content, existing.data = title, content, data
            existing.version += 1
            existing.status = "draft"
            touched.append(existing.id)
        else:
            art = Artifact(project_id=record.project_id, type=art_type, title=title,
                           content=content, data=data, status="draft", author=AUTHOR)
            session.add(art)
            session.flush()
            generated[item] = art.id
            touched.append(art.id)
    known = {m.content for m in session.scalars(select(MemoryEntry).where(
        MemoryEntry.scope == "project", MemoryEntry.scope_ref == record.project_id))}
    for fact in gen.conventions(a):
        if fact not in known:
            session.add(MemoryEntry(workspace_id=record.workspace_id, scope="project",
                                    scope_ref=record.project_id, kind="fact", content=fact,
                                    status="proposed", author=AUTHOR,
                                    source_artifact_ids=touched[:3], confidence=0.8))
    record.generated = generated
    return touched


def enrich_api(session: Session, record: RepoRecord) -> dict:
    """Optional model pass over the repo's endpoints (summaries, descriptions, extra errors).

    Only endpoints that exist are updated; anything else in the reply is dropped. Token use is
    recorded as a run, so it shows up in usage tracking like any agent.
    """
    from filament import llm
    from filament.readiness.scanner import Repo, checkout
    from filament.repos import api_enrich, api_extract

    analysis = copy.deepcopy(record.analysis)
    api = analysis.get("api") or {}
    endpoints = api.get("endpoints") or []
    if not endpoints:
        raise ValueError("No API endpoints to enrich")
    with checkout(record.ref) as root:
        prompt, picked = api_enrich.model_prompt(Repo(root), endpoints)
    result = llm.complete(api_enrich.MODEL_SYSTEM, prompt, model=get_settings().model_small,
                          max_tokens=4000,
                          offline_response=lambda: api_enrich.offline_summaries(picked))
    applied = api_enrich.apply_model_output(result.text, endpoints)
    if api.get("source") == "inferred":
        api["openapi"] = api_extract.to_openapi(record.name, endpoints, True, api.get("schemas"))
    api["enrichment"] = {"model": result.model, "applied": applied,
                         "at": datetime.now(UTC).isoformat()}
    record.analysis = analysis
    session.add(Run(project_id=record.project_id, agent="api-enricher",
                    goal=f"Document the API of {record.name}", status="succeeded",
                    model=result.model, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens, cost_usd=result.cost_usd(),
                    output={"applied": applied, "endpoints": len(picked)},
                    requested_by="launchpad", finished_at=datetime.now(UTC)))
    return api["enrichment"]


def due(session: Session, limit: int = 5) -> list[RepoRecord]:
    now = datetime.now(UTC)
    rows = session.scalars(select(RepoRecord).where(
        or_(RepoRecord.next_scan_at.is_(None), RepoRecord.next_scan_at <= now))
        .order_by(RepoRecord.next_scan_at).limit(limit * 4)).all()
    return [r for r in rows if interval_hours(r) > 0 and _is_due(r, now)][:limit]


def _is_due(r: RepoRecord, now: datetime) -> bool:
    if r.next_scan_at is not None:
        nxt = r.next_scan_at if r.next_scan_at.tzinfo else r.next_scan_at.replace(tzinfo=UTC)
        return nxt <= now
    last = r.scanned_at if r.scanned_at.tzinfo else r.scanned_at.replace(tzinfo=UTC)
    return last + timedelta(hours=interval_hours(r)) <= now


def scan_due(session: Session, limit: int = 5) -> list[RepoScan]:
    scans = []
    for record in due(session, limit):
        scans.append(scan_record(session, record, "schedule"))
        session.commit()
    return scans


def project_for(session: Session, workspace_id: str, name: str, ref: str, kinds: list[str]) -> str:
    project = Project(workspace_id=workspace_id, name=name,
                      description=f"Repository {ref} ({', '.join(kinds)})")
    session.add(project)
    session.flush()
    return project.id


async def scheduler_loop() -> None:
    """Background loop started with the app: rescans repos whose interval has elapsed."""
    import asyncio

    from filament.db import SessionLocal

    tick = max(30, get_settings().repo_scheduler_tick_seconds)
    while True:
        await asyncio.sleep(tick)
        try:
            def run() -> int:
                with SessionLocal() as s:
                    return len(scan_due(s))
            n = await asyncio.to_thread(run)
            if n:
                log.info("rescanned %d repos", n)
        except Exception:  # never let the loop die
            log.exception("repo scheduler tick failed")
