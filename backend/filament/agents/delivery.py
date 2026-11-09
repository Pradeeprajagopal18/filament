"""Delivery agents: one per pipeline stage after design.

Each gets a goal and tools; the model decides the steps (the system prompt describes a sensible
order, it is not hard-coded). The platform enforces the rules in the tools: checkpoints, retry
caps, autonomy for GitHub writes and rollouts, release approval.
"""

from __future__ import annotations

from filament.agents.base import Agent, AgentError, OfflineScript
from filament.agents.tools import READ_TOOLS

BASE = ["ask_human", "finish"]


class _Delivery(Agent):
    uses_mcp = True
    needs: tuple[str, ...] = ()

    def prepare(self) -> None:
        missing = [k for k in self.needs if not self.run_record.input.get(k)]
        if missing:
            raise AgentError(f"{self.name} needs {', '.join(missing)} in the run input")
        self.inp = self.run_record.input or {}

    def target_artifact_ids(self) -> list[str]:
        inp = self.run_record.input or {}
        ids = [inp.get(k) for k in ("issue_id", "pr_id", "release_id") if inp.get(k)]
        return ids + list(inp.get("pr_ids") or []) + list(inp.get("issue_ids") or [])

    def task(self, context: str) -> str:
        fields = "\n".join(f"- {k}: {v}" for k, v in (self.run_record.input or {}).items())
        return (f"Goal: {self.run_record.goal}\n\nInput:\n{fields or '- (none)'}\n\n"
                f"## Context already gathered\n{context}")


class TriageAgent(_Delivery):
    name = "triage"
    description = "Imports issues and scores their readiness; ready ones go to the product owner."
    tools = READ_TOOLS + ["list_issues", "import_issues", "score_issue", *BASE]
    system = (
        "You are the Triage Agent. You decide which work items are ready to build.\n"
        "1. If the input names a GitHub repo, import its open issues (import_issues). Otherwise "
        "work on the issues listed in the input, or the untriaged ones (list_issues).\n"
        "2. Score each issue (score_issue). Set a priority (P0-P3) from impact: production "
        "incidents and bugs that block users are P0-P1.\n"
        "3. For issues that are not ready, ask the author one concrete question per missing item "
        "(ask_human), e.g. the acceptance criteria or the steps to reproduce.\n"
        "4. Flag possible duplicates rather than scoring both as ready.\n"
        "Never mark scope as approved: the product owner does that.")

    def offline_script(self) -> OfflineScript:
        if self.inp.get("repo"):
            yield [("import_issues", {"repo": self.inp["repo"]})]
        ids = self.inp.get("issue_ids")
        if not ids:
            found = yield [("list_issues", {"untriaged_only": True})]
            ids = [i["id"] for i in found[0].get("results", [])]
        if not ids:
            yield [("finish", {"summary": "No untriaged issues."})]
            return
        scores = yield [("score_issue", {"id": i, "priority": "P2"}) for i in ids]
        asks = [("ask_human", {"question": f"Issue {i}: please add " +
                               "; ".join(s["missing"]).lower()})
                for i, s in zip(ids, scores, strict=True) if s.get("missing")]
        if asks:
            yield asks
        ready = sum(1 for s in scores if s.get("status") == "ready")
        yield [("finish", {"summary": f"Scored {len(ids)} issue(s): {ready} ready for the "
                                      f"product owner, {len(ids) - ready} need more detail."})]


class CodingAgent(_Delivery):
    name = "coding"
    description = ("Hands a work item to an external coding agent (Claude Code, Codex, Cursor) "
                   "with a grounded brief, and links the pull request it opens.")
    tools = READ_TOOLS + ["create_handoff", "link_pull_request", *BASE]
    needs = ("issue_id",)
    system = (
        "You are the Coding Agent. Filament does not write code itself: it hands each task to "
        "the team's coding agent (Claude Code, Codex or Cursor) with everything it needs.\n"
        "1. Read the work item and its approved design (read_artifact, search_graph).\n"
        "2. Find where the change goes (code_query, code_explain) and what it affects "
        "(code_impact), so the brief names real files, functions and endpoints.\n"
        "3. Write the handoff (create_handoff) for the requested target.\n"
        "4. If a pull request URL is in the input, link it (link_pull_request).\n"
        "Do not guess file names; only use what the code tools return.")

    def offline_script(self) -> OfflineScript:
        out = yield [("create_handoff", {"issue_id": self.inp["issue_id"],
                                         "repo": self.inp.get("repo"),
                                         "target": self.inp.get("target", "claude_code")})]
        task_id = out[0].get("id")
        if self.inp.get("pr_url") and task_id:
            yield [("link_pull_request", {"task_id": task_id, "url": self.inp["pr_url"]})]
        yield [("finish", {"summary": "Handed off to the coding agent" +
                           (" and linked its pull request." if self.inp.get("pr_url") else
                            "; waiting for its pull request.")})]


class QAAgent(_Delivery):
    name = "qa"
    description = "Writes a PR's test plan, reads CI, and sends failures back until they pass."
    tools = READ_TOOLS + ["write_test_plan", "check_ci", "request_fix", *BASE]
    needs = ("pr_id",)
    system = (
        "You are the QA Agent.\n"
        "1. Write or update the test plan for the pull request (write_test_plan).\n"
        "2. Read its CI (check_ci). Follow the decision it returns: 'retry' -> send the "
        "failures back to the coding agent (request_fix); 'escalate' -> ask a person "
        "(ask_human) with the failing checks; 'wait' -> finish and say checks are running; "
        "'done' -> finish.\n"
        "Never retry past the platform's limit and never mark failing checks as passing.")

    def offline_script(self) -> OfflineScript:
        pr = self.inp["pr_id"]
        yield [("write_test_plan", {"pr_id": pr})]
        ci = (yield [("check_ci", {"pr_id": pr})])[0]
        action = (ci.get("decision") or {}).get("action")
        if action == "retry":
            yield [("request_fix", {"pr_id": pr})]
        elif action == "escalate":
            names = ", ".join(f["name"] for f in ci.get("failed", []))
            yield [("ask_human", {"question": f"CI still fails after {ci.get('attempt')} "
                                              f"attempts ({names}). Can someone take a look?"})]
        yield [("finish", {"summary": {"retry": "CI failed; sent the failures back to the "
                                                "coding agent.",
                                       "escalate": "CI keeps failing; asked a person.",
                                       "wait": "Test plan written; CI is still running.",
                                       "done": "Test plan written; all checks pass."}
                           .get(action, "Test plan written.")})]


class ReviewAgent(_Delivery):
    name = "review"
    description = "First-pass PR review: security, contract impact, hotspots, tests; scorecard."
    tools = READ_TOOLS + ["review_pull_request", "post_review_comment", *BASE]
    needs = ("pr_id",)
    system = (
        "You are the Review Agent: the first pass before an engineer approves the PR.\n"
        "1. Run the review (review_pull_request).\n"
        "2. For high or blocker findings, check them with the code tools (code_explain, "
        "code_impact) and say concretely what to change.\n"
        "3. If autonomy allows and the input asks for it, post the review on the PR "
        "(post_review_comment).\n"
        "You never approve the PR: an engineer does.")

    def offline_script(self) -> OfflineScript:
        out = (yield [("review_pull_request", {"pr_id": self.inp["pr_id"]})])[0]
        if self.inp.get("post") and out.get("id"):
            yield [("post_review_comment", {"review_id": out["id"]})]
        verdict = str(out.get("verdict", "")).replace("_", " ")
        yield [("finish", {"summary": f"First-pass review: {verdict} (risk {out.get('risk')}). "
                                      "Waiting for an engineer to approve."})]


class ReleaseAgent(_Delivery):
    name = "release"
    description = ("Plans a flagged, staged rollout with rollback criteria; a release manager "
                   "approves.")
    tools = READ_TOOLS + ["plan_release", "promote_release", "slo_status", *BASE]
    needs = ("pr_ids",)
    system = (
        "You are the Release Agent.\n"
        "1. Check the SLOs that will guard the rollout (slo_status).\n"
        "2. Plan the release (plan_release): flag, rollout steps sized to risk, rollback "
        "criteria and who to tell.\n"
        "3. Only promote (promote_release) when the release is approved and autonomy allows.\n"
        "A release manager approves every production release.")

    def offline_script(self) -> OfflineScript:
        yield [("slo_status", {})]
        out = (yield [("plan_release", {"pr_ids": self.inp["pr_ids"],
                                        "title": self.inp.get("title")})])[0]
        yield [("finish", {"summary": f"Planned a rollout behind {out.get('flag')} "
                                      f"({' → '.join(f'{s}%' for s in out.get('steps', []))}); "
                                      "waiting for the release manager."})]


class SREAgent(_Delivery):
    name = "sre"
    description = "Watches SLOs, rolls back bad releases, opens incidents and follow-up issues."
    tools = READ_TOOLS + ["slo_status", "handle_slo_breaches", *BASE]
    system = (
        "You are the SRE Agent.\n"
        "1. Read SLO burn rates and active releases (slo_status).\n"
        "2. If any SLO is out of budget, handle it (handle_slo_breaches): the platform rolls "
        "back a release when its approved plan allows, opens an incident for on-call and files "
        "a follow-up issue that goes back to triage.\n"
        "3. Summarise what changed and what on-call should look at first.")

    def offline_script(self) -> OfflineScript:
        status = (yield [("slo_status", {})])[0]
        bad = [s for s in status.get("slos", []) if s["state"] != "ok"]
        if not bad:
            yield [("finish", {"summary": f"All {len(status.get('slos', []))} SLO(s) within "
                                          "budget."})]
            return
        out = (yield [("handle_slo_breaches", {})])[0]
        acts = out.get("actions", [])
        rolled = sum(1 for a in acts if a.get("rolled_back"))
        yield [("finish", {"summary": f"{len(bad)} SLO(s) out of budget: opened {len(acts)} "
                                      f"incident(s), rolled back {rolled} release(s), filed "
                                      f"{len(acts)} follow-up issue(s) for triage."})]


AGENTS = [TriageAgent, CodingAgent, QAAgent, ReviewAgent, ReleaseAgent, SREAgent]
