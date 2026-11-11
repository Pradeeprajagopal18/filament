"""Tools for the delivery agents (triage, coding handoff, QA, review, release, SRE).

The checks themselves are deterministic (`delivery.checks`); these handlers add the platform
rules: writes go through autonomy, checkpoints get the right role, retries are capped, GitHub
comments need L3+, releases roll out only after a release manager approves.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from igniteai.agents.tools import Tool, ToolError, _not_saved, _obj
from igniteai.config import get_settings
from igniteai.delivery import checks, github
from igniteai.delivery import service as svc
from igniteai.models import Approval, Artifact, MemoryEntry

if TYPE_CHECKING:
    from igniteai.agents.base import Agent

CHECKLIST = re.compile(r"^\s*[-*]\s*\[[ x]\]\s*(.+)$", re.M | re.I)


def _art(agent: Agent, aid: str, type_: str | None = None) -> Artifact:
    a = agent.session.get(Artifact, aid or "")
    if a is None or a.project_id != agent.project.id or (type_ and a.type != type_):
        raise ToolError(f"No {type_ or 'artifact'} {aid} in this project")
    agent.seen_ids.add(a.id)
    return a


def _gh(fn, *args):
    try:
        return fn(*args)
    except github.GitHubError as exc:
        raise ToolError(str(exc)) from exc


def _acceptance(agent: Agent, issue: Artifact) -> list[str]:
    items = CHECKLIST.findall(issue.content or "")
    for a in svc.related(agent.session, issue):
        if a.type == "story":
            items += CHECKLIST.findall(a.content or "")
    seen, out = set(), []
    for i in items:
        if i.strip().lower() not in seen:
            seen.add(i.strip().lower())
            out.append(i.strip())
    return out[:20]


def _latest(agent: Agent, root: Artifact, type_: str) -> Artifact | None:
    arts = [a for a in svc.related(agent.session, root) if a.type == type_]
    return sorted(arts, key=lambda a: a.created_at)[-1] if arts else None


# -- triage ---------------------------------------------------------------------------------------
def list_issues(agent: Agent, inp: dict) -> dict:
    stmt = select(Artifact).where(Artifact.project_id == agent.project.id,
                                  Artifact.type == "issue", Artifact.status != "rejected")
    issues = agent.session.scalars(stmt.order_by(Artifact.created_at)).all()
    if inp.get("untriaged_only"):
        issues = [i for i in issues if not (i.data or {}).get("triage")]
    return {"results": [{"id": i.id, "title": i.title, "status": i.status,
                         "readiness": ((i.data or {}).get("triage") or {}).get("score"),
                         "labels": (i.data or {}).get("labels", []),
                         "snippet": (i.content or "")[:200]} for i in issues[:30]],
            "total": len(issues)}


def import_issues(agent: Agent, inp: dict) -> dict:
    if not agent.persists:
        return _not_saved()
    arts = _gh(svc.import_issues, agent.session, agent.project, inp["repo"],
               inp.get("state", "open"), inp.get("labels", ""), int(inp.get("limit", 20)))
    agent.seen_ids |= {a.id for a in arts}
    return {"imported": [{"id": a.id, "title": a.title} for a in arts]}


def score_issue(agent: Agent, inp: dict) -> dict:
    issue = _art(agent, inp["id"], "issue")
    result = svc.score_issue(agent.session, agent.project, issue)
    if not agent.persists:
        return {**result, "saved": False}
    issue.data = {**(issue.data or {}), "triage": {**result, "by": f"agent:{agent.name}",
                                                   "priority": inp.get("priority")}}
    if result["status"] == "ready" and issue.status == "draft":
        # Ready to build: the product owner approves the scope (the agent cannot).
        issue.status = "in_review"
        agent.session.add(Approval(artifact_id=issue.id, role="product_owner"))
    agent.touched.append(issue)
    return {**result, "saved": True, "issue_status": issue.status}


# -- coding handoff -------------------------------------------------------------------------------
def create_handoff(agent: Agent, inp: dict, retry: dict | None = None) -> dict:
    from igniteai.models import CodeGraph

    issue = _art(agent, inp["issue_id"], "issue")
    record = svc.repo_for(agent.session, agent.project, inp.get("repo") or issue.data.get("repo"),
                          f"{issue.title}\n{issue.content}")
    symbols: list[dict] = []
    if record is not None:
        row = agent.session.get(CodeGraph, record.id)
        if row is not None and row.graph.get("nodes"):
            symbols = [{k: n.get(k) for k in ("id", "label", "kind", "file", "line", "is_class")}
                       for n in checks.start_symbols(row.graph["nodes"], issue.title,
                                                     issue.content[:1500])]
    for name in inp.get("symbols") or []:
        symbols.append({"label": name, "file": None, "line": None})
    # Impact from the functions to change, not the models they share with every endpoint.
    funcs = [s["id"] for s in symbols if s.get("id") and not s.get("is_class")]
    impact = svc.change_impact(agent.session, record, [], funcs) if funcs else None
    conventions = [m.content for m in agent.session.scalars(select(MemoryEntry).where(
        MemoryEntry.workspace_id == agent.project.workspace_id, MemoryEntry.status == "confirmed",
        MemoryEntry.kind.in_(["standard", "decision", "procedure"])))][:8]
    tests = []
    if record is not None:
        cmds = (record.analysis or {}).get("commands") or {}
        tests = [f"`{cmds[k]}`" for k in ("test", "lint") if cmds.get(k)]
    if not agent.persists:
        return _not_saved()
    task = agent.save_artifact("task", (f"Fix CI for: {issue.title}" if retry else
                                        issue.title)[:300], "")
    # The goal is the issue's description; its checklist goes in the acceptance section.
    goal = re.split(r"^#+\s*acceptance criteria", issue.content or "", flags=re.I | re.M)[0]
    goal = CHECKLIST.sub("", goal).strip()[:1500] or issue.title
    brief = checks.handoff_brief(
        issue.title, goal, _acceptance(agent, issue),
        record.name if record else None, symbols, (impact or {}).get("endpoints", []),
        [c["repo"] for c in (impact or {}).get("consumers", [])], conventions,
        tests, task.id, inp.get("target", "claude_code"), retry, (retry or {}).get("branch"))
    task.content = brief["brief"]
    task.data = {**brief, "issue_id": issue.id, "repo": record.ref if record else None,
                 "symbols": symbols, "attempt": (retry or {}).get("attempt", 1),
                 "retry_of": (retry or {}).get("pr_id")}
    svc.link(agent.session, agent.project, task, issue, "implements")
    return {"saved": True, "id": task.id, "branch": brief["branch"],
            "endpoints": (impact or {}).get("endpoints", []),
            "start_from": [s["label"] for s in symbols],
            "how_to_run": brief["commands"].get(inp.get("target", "claude_code"))}


def link_pull_request(agent: Agent, inp: dict) -> dict:
    task = _art(agent, inp["task_id"], "task")
    if not agent.persists:
        return _not_saved()
    pr = _gh(github.pull_request, inp["url"])
    art = svc.attach_pull_request(agent.session, agent.project, task, pr, f"agent:{agent.name}")
    agent.seen_ids.add(art.id)
    return {"saved": True, "id": art.id, "files": len(pr["files"]), "branch": pr["branch"]}


# -- QA -------------------------------------------------------------------------------------------
def write_test_plan(agent: Agent, inp: dict) -> dict:
    pr = _art(agent, inp["pr_id"], "pull_request")
    issue = svc.issue_of(agent.session, pr)
    record = svc.repo_for(agent.session, agent.project, pr.data.get("repo"))
    try:  # patches give the changed functions, not whole files
        files = github.pull_request(pr.data["url"])["files"]
    except (github.GitHubError, KeyError):
        files = svc.pr_files(pr)
    impact = svc.change_impact(agent.session, record, files)
    tested = {f["filename"] for f in files if checks.TEST_FILE.search(f["filename"])}
    cases = checks.test_plan(_acceptance(agent, issue) if issue else [],
                             (impact or {}).get("endpoints", []),
                             (impact or {}).get("changed", []), tested)
    if not agent.persists:
        return {"saved": False, "cases": cases}
    existing = _latest(agent, pr, "test_plan")
    body = "\n".join(f"- [{c['kind']}] {c['title']}" for c in cases) or "- No cases derived"
    if existing is not None:
        existing.data = {**existing.data, "cases": cases}
        existing.content = body
        return {"saved": True, "id": existing.id, "cases": len(cases), "updated": True}
    plan = agent.save_artifact("test_plan", f"Test plan: {pr.title}"[:300], body,
                               {"cases": cases, "pr_id": pr.id, "ci": {"state": "pending",
                                                                       "attempt": 1}})
    svc.link(agent.session, agent.project, plan, pr, "verifies")
    return {"saved": True, "id": plan.id, "cases": len(cases)}


def check_ci(agent: Agent, inp: dict) -> dict:
    pr = _art(agent, inp["pr_id"], "pull_request")
    runs = _gh(github.check_runs, pr.data["url"], pr.data.get("head_sha"))
    result = checks.classify_checks(runs)
    attempt = int(pr.data.get("attempt", 1))
    decision = checks.retry_decision(result["state"], attempt, get_settings().delivery_max_retries)
    if agent.persists:
        pr.data = {**pr.data, "checks": result}
        plan = _latest(agent, pr, "test_plan")
        if plan is not None:
            state = "escalated" if decision["action"] == "escalate" else result["state"]
            plan.data = {**plan.data, "ci": {"state": state, "attempt": attempt,
                                             "failed": [f["name"] for f in result["failed"]]}}
    return {**result, "attempt": attempt, "decision": decision}


def request_fix(agent: Agent, inp: dict) -> dict:
    """Send failing checks back to the coding agent as a new handoff (capped by the platform)."""
    pr = _art(agent, inp["pr_id"], "pull_request")
    failed = (pr.data.get("checks") or {}).get("failed") or []
    if not failed:
        raise ToolError("No failing checks recorded; call check_ci first")
    attempt = int(pr.data.get("attempt", 1))
    limit = get_settings().delivery_max_retries
    if attempt >= limit:
        raise ToolError(f"Retry limit reached ({limit}); ask a person to look (ask_human)")
    issue = svc.issue_of(agent.session, pr)
    if issue is None:
        raise ToolError("This PR is not linked to a work item")
    if not agent.persists:
        return _not_saved()
    out = create_handoff(agent, {"issue_id": issue.id, "repo": pr.data.get("repo")},
                         retry={"attempt": attempt + 1, "failures": failed, "pr_id": pr.id,
                                "branch": pr.data.get("branch")})
    pr.data = {**pr.data, "attempt": attempt + 1}
    task = agent.session.get(Artifact, out["id"])
    svc.link(agent.session, agent.project, task, pr, "relates_to")
    return {**out, "attempt": attempt + 1}


# -- review ---------------------------------------------------------------------------------------
def review_pull_request(agent: Agent, inp: dict) -> dict:
    pr = _art(agent, inp["pr_id"], "pull_request")
    data = _gh(github.pull_request, pr.data["url"])
    record = svc.repo_for(agent.session, agent.project, pr.data.get("repo"))
    impact = svc.change_impact(agent.session, record, data["files"])
    card = checks.review_findings(data["files"], impact)
    if impact:
        card["impact"] = {k: impact.get(k) for k in ("endpoints", "consumers", "hotspots_touched",
                                                     "summary")}
    if not agent.persists:
        return {"saved": False, **card}
    review = agent.save_artifact("review", f"Review: {pr.title}"[:300],
                                 checks.review_markdown(card, pr.title),
                                 {**card, "pr_id": pr.id, "pr_url": pr.data["url"]}, gate=True)
    svc.link(agent.session, agent.project, review, pr, "relates_to")
    return {"saved": True, "id": review.id, "verdict": card["verdict"], "risk": card["risk"],
            "counts": card["counts"], "top": card["findings"][:5]}


def post_review_comment(agent: Agent, inp: dict) -> dict:
    review = _art(agent, inp["review_id"], "review")
    if agent.autonomy not in {"L3", "L4"}:
        raise ToolError(f"Posting to GitHub needs autonomy L3 or higher; this run is "
                        f"{agent.autonomy}. The review is saved in Ignite.")
    out = _gh(github.comment, review.data["pr_url"], review.content)
    review.data = {**review.data, "posted": out["url"]}
    return {"posted": out["url"]}


# -- release --------------------------------------------------------------------------------------
def plan_release(agent: Agent, inp: dict) -> dict:
    prs = [_art(agent, i, "pull_request") for i in inp["pr_ids"]]
    risk_order = ["unknown", "low", "medium", "high"]
    risk, items = "low", []
    for pr in prs:
        review = _latest(agent, pr, "review")
        if review is None or review.status != "approved":
            raise ToolError(f"PR '{pr.title}' needs an approved review before it can be released")
        r = (review.data or {}).get("risk", "unknown")
        risk = max(risk, r, key=risk_order.index)
        impact = (review.data or {}).get("impact") or {}
        users = [c["repo"] for c in impact.get("consumers") or []]
        items.append({"url": pr.data.get("url"), "title": pr.title, "consumers": users})
    slos = svc.slo_overview(agent.session, agent.project)
    plan = checks.release_plan(inp.get("title") or prs[0].title, items, slos, risk)
    if not agent.persists:
        return {"saved": False, **plan}
    body = "\n".join([
        f"Behind flag `{plan['flag']}`, risk {risk}.", "",
        "Rollout: " + " → ".join(f"{s['percent']}%" for s in plan["steps"]), "",
        "Roll back automatically when:", *[f"- {r}" for r in plan["rollback_when"]],
        *(["", "Tell: " + ", ".join(plan["notify"])] if plan["notify"] else [])])
    rel = agent.save_artifact("release", f"Release: {inp.get('title') or prs[0].title}"[:300],
                              body, plan)
    for pr in prs:
        svc.link(agent.session, agent.project, rel, pr, "depends_on")
    return {"saved": True, "id": rel.id, "status": rel.status, "flag": plan["flag"],
            "steps": [s["percent"] for s in plan["steps"]],
            "note": "A release manager must approve before the first step rolls out."}


def promote_release(agent: Agent, inp: dict) -> dict:
    rel = _art(agent, inp["release_id"], "release")
    if agent.autonomy not in {"L3", "L4"}:
        raise ToolError("Promoting a rollout step needs autonomy L3 or higher")
    try:
        svc.promote(agent.session, rel, f"agent:{agent.name}")
    except svc.DeliveryError as exc:
        raise ToolError(str(exc)) from exc
    return {"stage": rel.data["stage"], "live": svc._rollout_detail(rel.data)}


# -- SRE ------------------------------------------------------------------------------------------
def slo_status(agent: Agent, inp: dict) -> dict:
    return {"slos": svc.slo_overview(agent.session, agent.project),
            "active_releases": [{"id": r.id, "title": r.title, "stage": r.data.get("stage"),
                                 "auto_rollback": r.data.get("auto_rollback")}
                                for r in svc.active_releases(agent.session, agent.project)]}


def handle_slo_breaches(agent: Agent, inp: dict) -> dict:
    if not agent.persists:
        return _not_saved()
    out = svc.check_slos(agent.session, agent.project, f"agent:{agent.name}")
    for a in out["actions"]:
        agent.seen_ids |= {a["incident"], a["follow_up_issue"]}
        for aid in (a["incident"], a["follow_up_issue"]):
            agent.created.append(agent.session.get(Artifact, aid))
    return {"actions": out["actions"],
            "slos": [{"id": s["id"], "title": s["title"], "state": s["state"]}
                     for s in out["slos"]]}


S = {"type": "string"}
TOOLS = [
    Tool("list_issues", "Work items (issues) in this project with their readiness score.",
         _obj({"untriaged_only": {"type": "boolean"}}), list_issues),
    Tool("import_issues", "Import open issues from a GitHub repo as work items (skips ones "
         "already imported).", _obj({"repo": S, "labels": S, "state": S,
                                     "limit": {"type": "integer"}}, ["repo"]),
         import_issues, writes=True),
    Tool("score_issue", "Score an issue's readiness (problem, acceptance criteria, scope, where "
         "in the code, repro steps, duplicates) and save it. Ready issues go to the product "
         "owner for scope approval.", _obj({"id": S, "priority": {"type": "string", "enum":
                                                                   ["P0", "P1", "P2", "P3"]}},
                                           ["id"]), score_issue, writes=True),
    Tool("create_handoff", "Write the task brief an external coding agent (Claude Code, Codex, "
         "Cursor) works from: acceptance criteria, code to start from, endpoints and consumers, "
         "conventions, tests, branch name.",
         _obj({"issue_id": S, "repo": S, "target": {"type": "string", "enum":
                                                     ["claude_code", "codex", "cursor"]},
               "symbols": {"type": "array", "items": S}}, ["issue_id"]),
         create_handoff, writes=True),
    Tool("link_pull_request", "Attach the pull request opened for a task.",
         _obj({"task_id": S, "url": S}, ["task_id", "url"]), link_pull_request, writes=True),
    Tool("write_test_plan", "Derive the test plan for a PR: acceptance cases, contract tests for "
         "touched endpoints, unit tests for changed code without tests.",
         _obj({"pr_id": S}, ["pr_id"]), write_test_plan, writes=True),
    Tool("check_ci", "Read the PR's CI checks, classify failures and get the retry decision "
         "(retry, wait, done or escalate).", _obj({"pr_id": S}, ["pr_id"]), check_ci),
    Tool("request_fix", "Send failing CI back to the coding agent as a new handoff. The "
         "platform caps the number of attempts.", _obj({"pr_id": S}, ["pr_id"]), request_fix,
         writes=True),
    Tool("review_pull_request", "First-pass review of a PR: secrets, debug output, endpoints "
         "other repos call, hotspots, missing tests, migrations, size. Saves a scorecard for "
         "the engineer who approves the PR.", _obj({"pr_id": S}, ["pr_id"]),
         review_pull_request, writes=True),
    Tool("post_review_comment", "Post a saved review as a PR comment (autonomy L3+).",
         _obj({"review_id": S}, ["review_id"]), post_review_comment, writes=True),
    Tool("plan_release", "Plan a flagged, staged rollout with rollback criteria for reviewed "
         "PRs. Always waits for a release manager's approval.",
         _obj({"pr_ids": {"type": "array", "items": S}, "title": S}, ["pr_ids"]),
         plan_release, writes=True),
    Tool("promote_release", "Roll an approved release out to its next step (autonomy L3+).",
         _obj({"release_id": S}, ["release_id"]), promote_release, writes=True),
    Tool("slo_status", "SLOs with error-budget burn rates, plus releases rolling out.",
         _obj({}), slo_status),
    Tool("handle_slo_breaches", "For each SLO out of budget: roll back the active release when "
         "its approved plan allows, open an incident for on-call, and file a follow-up issue "
         "for triage. Does not duplicate open incidents.", _obj({}), handle_slo_breaches,
         writes=True),
]
