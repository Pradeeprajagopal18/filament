"""Delivery pipeline: work items moving through Plan → Design → Build → Test → Review → Deploy
→ Operate, each with an agent and a human checkpoint.

A work item is an `issue` artifact. Its progress is not stored separately: it is read from the
artifacts linked to it (PRD and flows, coding task, pull request, test plan, review, release,
incidents) and their approvals, so the board can never drift from the graph.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from igniteai import access
from igniteai.delivery import checks
from igniteai.models import Approval, Artifact, Link, Project, RepoRecord, SloSample

ROLE_FOR = {"issue": "product_owner", "prd": "tech_lead", "story": "tech_lead",
            "flow": "tech_lead", "adr": "tech_lead", "api_contract": "tech_lead",
            "review": "engineer", "pull_request": "engineer", "release": "release_manager",
            "incident": "on_call"}
ROLE_LABEL = {"product_owner": "Product owner", "tech_lead": "Tech lead", "engineer": "Engineer",
              "release_manager": "Release manager", "on_call": "On-call engineer"}
ALWAYS_GATED = {"release"}  # production releases need a person at every autonomy level
TARGET_LABEL = {"claude_code": "Claude Code", "codex": "Codex", "cursor": "Cursor"}

STAGES = [
    {"key": "plan", "label": "Plan", "work": "Import issues · Prioritize", "agent": "triage",
     "agent_label": "Triage Agent", "agent_does": "Scores issue readiness",
     "role": "product_owner", "checkpoint": "Approves scope", "types": ["issue"]},
    {"key": "design", "label": "Design", "work": "Write spec · Map deps", "agent": "orchestrator",
     "agent_label": "Architect Agent", "agent_does": "Drafts the spec",
     "role": "tech_lead", "checkpoint": "Signs off on design",
     "types": ["prd", "story", "flow", "adr", "api_contract"]},
    {"key": "build", "label": "Build", "work": "Write code · Open PR", "agent": "coding",
     "agent_label": "Coding Agent", "agent_does": "Hands the task to Claude Code, Codex or Cursor",
     "role": None, "checkpoint": "Auto retry: loops until all checks pass",
     "types": ["task", "pull_request"]},
    {"key": "test", "label": "Test", "work": "Run CI checks · Fix failures", "agent": "qa",
     "agent_label": "QA Agent", "agent_does": "Writes the test plan and runs CI",
     "role": None, "checkpoint": "Auto retry: loops until all checks pass",
     "types": ["test_plan"]},
    {"key": "review", "label": "Review", "work": "Security scan · Scorecards", "agent": "review",
     "agent_label": "Review Agent", "agent_does": "First-pass PR review",
     "role": "engineer", "checkpoint": "Approves the PR", "types": ["review"]},
    {"key": "deploy", "label": "Deploy", "work": "Promote · Auto rollback", "agent": "release",
     "agent_label": "Release Agent", "agent_does": "Rolls out behind flags",
     "role": "release_manager", "checkpoint": "Approves prod release", "types": ["release"]},
    {"key": "operate", "label": "Operate", "work": "Monitor · Triage incidents", "agent": "sre",
     "agent_label": "SRE Agent", "agent_does": "Watches SLOs", "role": "on_call",
     "checkpoint": "Owns incidents", "types": ["incident"]},
]
STAGE_BY_KEY = {s["key"]: s for s in STAGES}
LINK_KIND = {"design": "derives_from", "build": "implements", "test": "verifies",
             "review": "relates_to", "deploy": "depends_on", "operate": "relates_to"}


class DeliveryError(ValueError):
    pass


# -- graph walking ---------------------------------------------------------------------------------
def related(session: Session, root: Artifact) -> list[Artifact]:
    """Artifacts connected to a work item, stopping at other work items (issues)."""
    seen, out, queue = {root.id}, [], deque([root.id])
    while queue:
        cur = queue.popleft()
        links = session.scalars(select(Link).where(
            or_(Link.source_id == cur, Link.target_id == cur))).all()
        for lk in links:
            if lk.kind == "derives_from" and lk.source_id == root.id and root.type == "issue":
                continue  # where a work item came from (e.g. an incident) is not its progress
            other = lk.target_id if lk.source_id == cur else lk.source_id
            if other in seen:
                continue
            seen.add(other)
            art = session.get(Artifact, other)
            if art is None or art.status == "rejected":
                continue
            out.append(art)
            if art.type != "issue":
                queue.append(other)
    return out


def _pending(session: Session, ids: list[str]) -> list[Approval]:
    if not ids:
        return []
    return list(session.scalars(select(Approval).where(Approval.artifact_id.in_(ids),
                                                       Approval.status == "pending")))


def _decision(arts: list[Artifact]) -> str:
    if not arts:
        return "todo"
    if all(a.status == "approved" for a in arts):
        return "approved"
    if any(a.status == "in_review" for a in arts):
        return "waiting"
    return "draft"


def stage_states(session: Session, issue: Artifact) -> list[dict]:
    rel = related(session, issue)
    by_type: dict[str, list[Artifact]] = {}
    for a in rel:
        by_type.setdefault(a.type, []).append(a)
    triage = (issue.data or {}).get("triage")
    out = []
    for st in STAGES:
        arts = [issue] if st["key"] == "plan" else [a for t in st["types"]
                                                     for a in by_type.get(t, [])]
        detail, state = "", "todo"
        if st["key"] == "plan":
            if triage:
                detail = f"Readiness {triage['score']} ({triage['status'].replace('_', ' ')})"
                state = "approved" if issue.status == "approved" else (
                    "needs_info" if triage["status"] != "ready" else "waiting")
        elif st["key"] == "design":
            state = _decision(arts)
            detail = ", ".join(sorted({a.type.upper() if a.type == "prd" else a.type
                                       for a in arts})) if arts else ""
        elif st["key"] == "build":
            prs = by_type.get("pull_request", [])
            tasks = by_type.get("task", [])
            if prs:
                pr = prs[-1]
                # Build ends when the coding agent has opened its PR; CI fixes land on that PR.
                state = "done"
                detail = f"PR #{pr.data.get('number', '?')} " + (
                    "merged" if pr.data.get("merged") else "open")
            elif tasks:
                target = TARGET_LABEL.get(tasks[-1].data.get("target"), "the coding agent")
                state, detail = "handed_off", f"Handed to {target}"
        elif st["key"] == "test":
            plan = arts[-1] if arts else None
            ci = (plan.data if plan else {}).get("ci") or {}
            if plan:
                state = {"passing": "passed", "failing": "failing", "escalated": "blocked",
                         "pending": "running"}.get(ci.get("state"), "draft")
                detail = f"{len(plan.data.get('cases', []))} cases" + (
                    f", attempt {ci['attempt']}" if ci.get("attempt") else "")
        elif st["key"] == "review":
            if arts:
                card = arts[-1].data or {}
                state = _decision(arts[-1:])
                detail = card.get("verdict", "").replace("_", " ")
        elif st["key"] == "deploy":
            if arts:
                rel_data = arts[-1].data or {}
                stage = rel_data.get("stage", "planned")
                state = "waiting" if arts[-1].status == "in_review" else {
                    "promoted": "done", "rolled_back": "rolled_back",
                    "rolling_out": "rolling_out"}.get(stage, "ready")
                detail = _rollout_detail(rel_data)
        elif st["key"] == "operate":
            open_inc = [a for a in arts if (a.data or {}).get("state", "open") != "resolved"]
            if arts:
                state = "incident" if open_inc else "done"
                detail = f"{len(open_inc)} open incident(s)" if open_inc else "Incidents resolved"
            elif any((r.data or {}).get("stage") == "promoted" for r in by_type.get("release", [])):
                state, detail = "watching", "Watching SLOs"
        pend = _pending(session, [a.id for a in arts])
        out.append({**{k: st[k] for k in ("key", "label", "work", "agent", "agent_label",
                                          "agent_does", "role", "checkpoint")},
                    "role_label": ROLE_LABEL.get(st["role"] or "", "Automatic"),
                    "state": state, "detail": detail,
                    "artifacts": [{"id": a.id, "type": a.type, "title": a.title,
                                   "status": a.status} for a in arts if a.id != issue.id][:8],
                    "pending_approvals": [{"id": p.id, "artifact_id": p.artifact_id,
                                           "role": p.role} for p in pend]})
    return out


DONE = {"approved", "done", "passed", "watching"}


def current_stage(states: list[dict]) -> str:
    for s in states:
        if s["state"] not in DONE:
            return s["key"]
    return "operate"


def _rollout_detail(d: dict) -> str:
    steps = d.get("steps") or []
    live = [s["percent"] for s in steps if s["status"] == "done"]
    if d.get("stage") == "rolled_back":
        return "Rolled back"
    return f"{live[-1]}% live" if live else f"Flag {d.get('flag', '')}".strip()


def board(session: Session, project: Project) -> dict:
    issues = session.scalars(select(Artifact).where(
        Artifact.project_id == project.id, Artifact.type == "issue",
        Artifact.status != "rejected").order_by(Artifact.created_at.desc())).all()
    items = []
    for issue in issues:
        states = stage_states(session, issue)
        origin = next((lk.target_id for lk in session.scalars(select(Link).where(
            Link.source_id == issue.id, Link.kind == "derives_from"))), None)
        origin_art = session.get(Artifact, origin) if origin else None
        items.append({"id": issue.id, "title": issue.title, "status": issue.status,
                      "url": (issue.data or {}).get("url"),
                      "labels": (issue.data or {}).get("labels", []),
                      "readiness": ((issue.data or {}).get("triage") or {}).get("score"),
                      "follow_up_of": origin_art.id if origin_art and origin_art.type == "incident"
                      else None,
                      "stage": current_stage(states), "stages": states})
    return {"stages": [{k: s[k] for k in ("key", "label", "work", "agent", "agent_label",
                                          "agent_does", "role", "checkpoint")} |
                       {"role_label": ROLE_LABEL.get(s["role"] or "", "Automatic")}
                       for s in STAGES],
            "items": items, "slos": slo_overview(session, project)}


# -- issues ---------------------------------------------------------------------------------------
def add_issue(session: Session, project: Project, title: str, body: str = "",
              data: dict | None = None, author: str = "human") -> Artifact:
    url = (data or {}).get("url")
    if url:
        existing = next((a for a in session.scalars(select(Artifact).where(
            Artifact.project_id == project.id, Artifact.type == "issue"))
            if (a.data or {}).get("url") == url), None)
        if existing:
            return existing
    art = Artifact(project_id=project.id, type="issue", title=title[:300], content=body,
                   data=data or {}, status="draft", author=author)
    session.add(art)
    session.flush()
    return art


def import_issues(session: Session, project: Project, repo: str, state: str = "open",
                  labels: str = "", limit: int = 20) -> list[Artifact]:
    from igniteai.delivery import github

    found = github.issues(repo, state, labels, limit)
    return [add_issue(session, project, i["title"], i["body"],
                      {"url": i["url"], "number": i["number"], "labels": i["labels"],
                       "repo": i["repo"], "source": "github"}, author=f"github:{i['author']}")
            for i in found]


def triage_context(session: Session, project: Project, issue: Artifact) -> dict:
    repos = access.filter_repos(session, session.scalars(select(RepoRecord).where(
        RepoRecord.workspace_id == project.workspace_id)).all())
    terms: set[str] = set()
    for r in repos:
        terms |= {r.name.lower()}
        for e in ((r.analysis or {}).get("api") or {}).get("endpoints") or []:
            terms |= {seg.lower() for seg in re.split(r"[/{}_-]+", e["path"]) if len(seg) > 3}
        for c in ((r.analysis or {}).get("code") or {}).get("communities") or []:
            terms |= {w for w in re.split(r"\W+", c["name"].lower()) if len(w) > 3}
    others = [(a.id, a.title) for a in session.scalars(select(Artifact).where(
        Artifact.project_id == project.id, Artifact.type == "issue", Artifact.id != issue.id))]
    return {"repos": [r.name for r in repos], "known_terms": terms, "others": others}


def score_issue(session: Session, project: Project, issue: Artifact) -> dict:
    ctx = triage_context(session, project, issue)
    return checks.triage_score(issue.title, issue.content, (issue.data or {}).get("labels"),
                               ctx["repos"], ctx["known_terms"], ctx["others"])


# -- repos, PRs, impact ---------------------------------------------------------------------------
def repo_for(session: Session, project: Project, hint: str | None,
             text: str = "") -> RepoRecord | None:
    """The onboarded repo a work item is about: an explicit hint, else the one its text names,
    else the only repo in the workspace."""
    repos = access.filter_repos(session, session.scalars(select(RepoRecord).where(
        RepoRecord.workspace_id == project.workspace_id)).all())
    if not repos:
        return None
    if not hint and text:
        low = text.lower()
        named = []
        for r in repos:
            hits = [m.start() for m in re.finditer(rf"\b{re.escape(r.name.lower())}\b", low)]
            if hits:
                named.append((-len(hits), hits[0], r.name, r))
        if named:  # the repo named most often, then first
            return sorted(named, key=lambda x: x[:3])[0][3]
    hint = (hint or "").lower().removesuffix(".git")
    for r in repos:
        ref = (r.ref or "").lower().removesuffix(".git")
        if hint and (r.id == hint or r.name.lower() == hint or ref.endswith(hint) or
                     hint.endswith(r.name.lower())):
            return r
    return repos[0] if len(repos) == 1 else None


def change_impact(session: Session, record: RepoRecord | None, files: list[dict] | list[str],
                  symbols: list[str] | None = None) -> dict | None:
    """Code-graph impact of changed files (with patches when we have them) or symbols."""
    from igniteai.models import CodeGraph
    from igniteai.repos import codequery as cq
    from igniteai.repos.service import system_for

    if record is None:
        return None
    row = session.get(CodeGraph, record.id)
    if row is None or not row.graph.get("nodes"):
        return None
    g = row.graph
    changes: dict = {}
    for f in files:
        if isinstance(f, str):
            changes[f] = None
        elif f.get("patch"):
            spans = cq.parse_diff(f"diff --git a/{f['filename']} b/{f['filename']}\n"
                                  f"+++ b/{f['filename']}\n{f['patch']}")
            changes.update(spans or {f["filename"]: None})
        else:
            changes[f["filename"]] = None
    if symbols:
        from igniteai.repos.codegraph import Index, _ranges

        idx = Index(g)
        rng = _ranges(idx)
        for s in symbols:
            try:
                n = cq.resolve(idx, s)
            except (cq.Ambiguous, cq.NotFound):
                continue
            r = rng.get(n["id"])
            if n.get("file") and changes.get(n["file"], []) is not None:
                changes.setdefault(n["file"], []).append((r[1], r[2]) if r else (n["line"],) * 2)
    if not changes:
        return None
    code = (record.analysis or {}).get("code") or {}
    system = system_for(session, record.workspace_id)
    return cq.change_impact(g, code.get("endpoints") or {}, changes,
                            system["consumers"].get(record.id, {}),
                            {h["id"] for h in code.get("hotspots") or []})


def link(session: Session, project: Project, source: Artifact, target: Artifact, kind: str) -> None:
    exists = session.scalars(select(Link).where(
        or_((Link.source_id == source.id) & (Link.target_id == target.id),
            (Link.source_id == target.id) & (Link.target_id == source.id)))).first()
    if not exists:
        session.add(Link(project_id=project.id, source_id=source.id, target_id=target.id,
                         kind=kind))


def attach_pull_request(session: Session, project: Project, task: Artifact, pr: dict,
                        author: str = "human") -> Artifact:
    """Record a PR opened for a coding task (by a person, a webhook or the coding agent)."""
    existing = next((a for a in session.scalars(select(Artifact).where(
        Artifact.project_id == project.id, Artifact.type == "pull_request"))
        if (a.data or {}).get("url") == pr["url"]), None)
    data = {k: pr.get(k) for k in ("url", "repo", "number", "branch", "head_sha", "state",
                                   "merged", "author")}
    data["files"] = [{"filename": f["filename"], "additions": f.get("additions", 0),
                      "deletions": f.get("deletions", 0)} for f in pr.get("files", [])]
    if existing:
        existing.data = {**existing.data, **data}
        art = existing
    else:
        art = Artifact(project_id=project.id, type="pull_request",
                       title=pr.get("title") or f"PR #{pr.get('number')}",
                       content=pr.get("body", "")[:4000], data=data, status="in_review",
                       author=author)
        session.add(art)
        session.flush()
    link(session, project, art, task, "implements")
    return art


def pr_files(pr: Artifact) -> list[dict]:
    """Changed files of a PR artifact; fetched from GitHub when the record has none yet
    (webhook events don't carry them)."""
    from igniteai.delivery import github

    files = (pr.data or {}).get("files") or []
    if files or not (pr.data or {}).get("url"):
        return files
    try:
        fetched = github.pull_request(pr.data["url"])["files"]
    except github.GitHubError:
        return []
    pr.data = {**pr.data, "files": [{"filename": f["filename"], "additions": f.get("additions", 0),
                                     "deletions": f.get("deletions", 0)} for f in fetched]}
    return pr.data["files"]


def task_for_pr(session: Session, project: Project, pr: dict) -> Artifact | None:
    m = re.search(r"Ignite-Task:\s*([0-9a-f]{12})", pr.get("body") or "") or re.search(
        r"ignite/([0-9a-f]{12})", pr.get("branch") or "")
    if not m:
        return None
    art = session.get(Artifact, m.group(1))
    return art if art and art.project_id == project.id and art.type == "task" else None


def issue_of(session: Session, art: Artifact) -> Artifact | None:
    if art.type == "issue":
        return art
    return next((a for a in related(session, art) if a.type == "issue"), None)


# -- SLOs and releases ----------------------------------------------------------------------------
def slo_overview(session: Session, project: Project, at: datetime | None = None) -> list[dict]:
    slos = session.scalars(select(Artifact).where(Artifact.project_id == project.id,
                                                  Artifact.type == "slo")).all()
    at = at or datetime.now(UTC)
    out = []
    for s in slos:
        samples = session.scalars(select(SloSample).where(
            SloSample.slo_id == s.id, SloSample.at >= at - timedelta(hours=24))).all()
        rates = checks.burn_rates([{"at": x.at, "good": x.good, "total": x.total}
                                   for x in samples], float(s.data.get("objective", 99.9)), at)
        out.append({"id": s.id, "title": s.title, "objective": s.data.get("objective", 99.9),
                    "service": s.data.get("service"), "burn": rates,
                    **checks.slo_status(rates), "samples": len(samples)})
    return out


def active_releases(session: Session, project: Project) -> list[Artifact]:
    return [a for a in session.scalars(select(Artifact).where(
        Artifact.project_id == project.id, Artifact.type == "release"))
        if (a.data or {}).get("stage") in {"rolling_out", "promoted"}]


def promote(session: Session, release: Artifact, actor: str) -> Artifact:
    if release.status != "approved":
        raise DeliveryError("A release manager must approve this release before it rolls out")
    d = dict(release.data or {})
    if d.get("stage") == "rolled_back":
        raise DeliveryError("This release was rolled back; plan a new release")
    steps = [dict(s) for s in d.get("steps") or []]
    nxt = next((s for s in steps if s["status"] != "done"), None)
    if nxt is None:
        raise DeliveryError("Already fully rolled out")
    nxt["status"] = "done"
    nxt["at"] = datetime.now(UTC).isoformat()
    nxt["by"] = actor
    d["steps"] = steps
    d["stage"] = "promoted" if all(s["status"] == "done" for s in steps) else "rolling_out"
    d.setdefault("started_at", nxt["at"])
    release.data = d
    return release


def rollback(session: Session, release: Artifact, actor: str, reason: str) -> Artifact:
    d = dict(release.data or {})
    if d.get("stage") not in {"rolling_out", "promoted"}:
        raise DeliveryError("Only a release that is rolling out or live can be rolled back")
    d["stage"] = "rolled_back"
    d["rollback"] = {"by": actor, "reason": reason, "at": datetime.now(UTC).isoformat()}
    release.data = d
    return release


def open_incident(session: Session, project: Project, slo: dict, release: Artifact | None,
                  author: str, rolled_back: bool) -> tuple[Artifact, Artifact]:
    """An incident for an SLO breach, plus the follow-up issue that feeds back into triage."""
    body = [f"**{slo['title']}** is {slo['state']}: {slo['reason']}.",
            "Burn rates: " + ", ".join(f"{k} {v}x" for k, v in slo["burn"].items()
                                       if v is not None) + "."]
    if release is not None:
        body.append(f"Started during release artifact:{release.id} "
                    f"({_rollout_detail(release.data)})."
                    + (" It was rolled back automatically." if rolled_back else ""))
    inc = Artifact(project_id=project.id, type="incident",
                   title=f"{slo['title']} {'breach' if slo['state'] == 'breach' else 'warning'}",
                   content="\n\n".join(body), status="in_review", author=author,
                   data={"slo_id": slo["id"], "severity": slo["severity"], "state": "open",
                         "release_id": release.id if release else None,
                         "rolled_back": rolled_back})
    session.add(inc)
    session.flush()
    session.add(Approval(artifact_id=inc.id, role="on_call"))
    if release is not None:
        link(session, project, inc, release, "relates_to")
    follow = Artifact(project_id=project.id, type="issue",
                      title=f"Follow-up: {inc.title}",
                      content=("Found in production by the SRE agent.\n\n" + "\n\n".join(body) +
                               "\n\n## Acceptance criteria\n- [ ] Root cause written up\n"
                               f"- [ ] {slo['title']} back within budget for 24 hours\n"
                               "- [ ] A test or alert that would have caught it"),
                      data={"source": "incident", "labels": ["incident-follow-up"]},
                      status="draft", author=author)
    session.add(follow)
    session.flush()
    session.add(Link(project_id=project.id, source_id=follow.id, target_id=inc.id,
                     kind="derives_from"))
    return inc, follow


def check_slos(session: Session, project: Project, actor: str = "platform:slo-check") -> dict:
    """Evaluate SLOs; on a breach during a rollout with auto-rollback approved, roll it back and
    open an incident with a follow-up issue. Existing open incidents are not duplicated."""
    slos = slo_overview(session, project)
    actions = []
    for slo in slos:
        if slo["state"] == "ok":
            continue
        open_inc = [a for a in session.scalars(select(Artifact).where(
            Artifact.project_id == project.id, Artifact.type == "incident"))
            if a.data.get("slo_id") == slo["id"] and a.data.get("state") != "resolved"]
        if open_inc:
            continue
        rel = next(iter(sorted(active_releases(session, project),
                               key=lambda a: a.updated_at, reverse=True)), None)
        rolled = False
        if rel is not None and slo["state"] == "breach" and rel.data.get("auto_rollback"):
            rollback(session, rel, actor, f"{slo['title']}: {slo['reason']}")
            rolled = True
        inc, follow = open_incident(session, project, slo, rel, actor, rolled)
        actions.append({"slo": slo["title"], "incident": inc.id, "follow_up_issue": follow.id,
                        "rolled_back": rel.id if rolled else None})
    return {"slos": slos, "actions": actions}


def check_all(session: Session) -> int:
    n = 0
    for project in session.scalars(select(Project)).all():
        if session.scalars(select(Artifact.id).where(Artifact.project_id == project.id,
                                                     Artifact.type == "slo")).first():
            n += len(check_slos(session, project)["actions"])
    session.commit()
    return n
