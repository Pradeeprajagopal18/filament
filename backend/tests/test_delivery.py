"""Delivery pipeline: one work item through Plan → Design → Build → Test → Review → Deploy →
Operate, with each human checkpoint enforced, GitHub mocked, and the SRE feedback loop."""

from datetime import UTC, datetime, timedelta

import pytest

from filament.delivery import checks, github
from tests.test_codegraph import FILES, WEB, pytestmark, write  # noqa: F401  (skip w/o grammars)

GOOD_ISSUE = (
    "When a customer places an order on checkout-api, fulfilment needs the cart total in the "
    "order.placed event so it can pick the right shipping tier without another call.\n\n"
    "## Acceptance criteria\n- [ ] order.placed includes total_cents\n"
    "- [ ] existing consumers keep working\n")
PR_URL = "https://github.com/acme/checkout-api/pull/42"


@pytest.fixture
def setup(client, ws, project, tmp_path):
    client.post(f"/api/workspaces/{project[0]['id']}/repos",
                json={"repo": str(write(tmp_path / "acme" / "checkout-api", FILES))})
    client.post(f"/api/workspaces/{project[0]['id']}/repos",
                json={"repo": str(write(tmp_path / "acme" / "web-storefront", WEB))})
    return project[1]


@pytest.fixture
def gh(monkeypatch):
    state = {"checks": [{"name": "pytest", "status": "completed", "conclusion": "failure",
                         "summary": "test_place_order failed", "url": ""},
                        {"name": "ruff", "status": "completed", "conclusion": "success",
                         "summary": "", "url": ""}],
             "comments": []}

    def pull_request(url):
        return {"url": url, "repo": "acme/checkout-api", "number": 42,
                "title": "Add total to order.placed",
                "body": state.get("body", ""), "branch": state.get("branch", "feature"),
                "head_sha": "abc123", "state": "open", "merged": False, "author": "claude",
                "files": [{"filename": "app/main.py", "status": "modified", "additions": 2,
                           "deletions": 0,
                           "patch": "@@ -12,2 +12,4 @@\n     cart = load_cart(cart_id)\n"
                                    "+    print(order)\n+    # TODO: currency\n"
                                    "     order = save_order(cart['id'])"}]}

    monkeypatch.setattr(github, "pull_request", pull_request)
    monkeypatch.setattr(github, "check_runs", lambda url, sha=None: state["checks"])
    monkeypatch.setattr(github, "comment", lambda url, body: state["comments"].append(body)
                        or {"url": url + "#c1"})
    return state


def board(client, pid, item_id=None):
    b = client.get(f"/api/projects/{pid}/delivery").json()
    if item_id is None:
        return b
    item = next(i for i in b["items"] if i["id"] == item_id)
    return {s["key"]: s for s in item["stages"]} | {"_item": item}


def approve_pending(client, pid, role=None):
    pending = client.get(f"/api/projects/{pid}/approvals").json()
    done = []
    for ap in pending:
        if role is None or ap["role"] == role:
            r = client.post(f"/api/artifacts/{ap['artifact_id']}/approve",
                            json={"decision": "approved", "decided_by": role or "person"})
            assert r.status_code == 200
            done.append(ap)
    return done


def advance(client, item, stage=None, **kw):
    return client.post(f"/api/work-items/{item}/advance", json={"stage": stage, **kw})


def test_work_item_through_all_stages(client, setup, gh):
    pid = setup["id"]
    item = client.post(f"/api/projects/{pid}/issues",
                       json={"title": "Include cart total in order.placed",
                             "body": GOOD_ISSUE}).json()
    vague = client.post(f"/api/projects/{pid}/issues",
                        json={"title": "Make checkout better"}).json()
    assert [s["key"] for s in item["stages"]] == ["plan", "design", "build", "test", "review",
                                                   "deploy", "operate"]

    # Plan: triage scores readiness; the product owner approves scope.
    r = advance(client, item["id"])
    assert r.status_code == 200 and r.json()["run"]["agent"] == "triage"
    st = board(client, pid, item["id"])
    assert st["plan"]["state"] == "waiting"
    assert st["plan"]["pending_approvals"][0]["role"] == "product_owner"
    assert st["_item"]["readiness"] >= 70
    advance(client, vague["id"])
    v = board(client, pid, vague["id"])
    assert v["plan"]["state"] == "needs_info" and v["_item"]["readiness"] < 40
    assert v["plan"]["detail"].endswith("(not ready)")
    assert advance(client, item["id"], "design").status_code == 409  # scope not approved yet
    assert approve_pending(client, pid, "product_owner")

    # Design: the architect (orchestrator) drafts PRD/stories/flows; tech lead signs off.
    r = advance(client, item["id"])
    assert r.json()["run"]["agent"] == "orchestrator"
    st = board(client, pid, item["id"])
    assert st["design"]["state"] == "waiting" and st["design"]["artifacts"]
    assert advance(client, item["id"], "build").status_code == 409  # design not signed off
    approve_pending(client, pid, "tech_lead")
    assert board(client, pid, item["id"])["design"]["state"] == "approved"

    # Build: a grounded handoff for Claude Code, then the PR links itself via the webhook.
    r = advance(client, item["id"], target="claude_code")
    assert r.json()["run"]["agent"] == "coding", r.json()
    st = board(client, pid, item["id"])
    task_id = next(a["id"] for a in st["build"]["artifacts"] if a["type"] == "task")
    h = client.get(f"/api/tasks/{task_id}/handoff").json()
    assert h["branch"].startswith(f"filament/{task_id}-")
    assert "place_order()" in h["brief"] and "POST /v1/orders" in h["brief"]
    assert "web-storefront" in h["brief"] and "order.placed includes total_cents" in h["brief"]
    assert task_id in h["commands"]["claude_code"]
    assert advance(client, item["id"], "test").status_code == 409  # no PR yet
    gh["branch"] = h["branch"]
    hook = client.post("/api/webhooks/github", headers={"X-GitHub-Event": "pull_request"},
                       json={"action": "opened", "repository": {"full_name": "acme/checkout-api"},
                             "pull_request": {"html_url": PR_URL, "title": "Add total",
                                              "head": {"ref": h["branch"], "sha": "abc123"},
                                              "number": 42, "state": "open"}}).json()
    assert hook.get("task") == task_id
    st = board(client, pid, item["id"])
    assert st["build"]["state"] == "done" and st["build"]["detail"] == "PR #42 open"
    assert st["_item"]["stage"] == "test"

    # Test: plan + failing CI -> sent back to the coding agent (attempt 2); then green.
    r = advance(client, item["id"], "test")
    st = board(client, pid, item["id"])
    assert st["test"]["state"] == "failing"
    tasks = [a["id"] for a in st["build"]["artifacts"] if a["type"] == "task"]
    assert len(tasks) == 2  # retry handoff, on the PR's branch, with the failure in it
    retry = client.get(f"/api/tasks/{tasks[-1]}/handoff").json()
    assert retry["attempt"] == 2 and retry["branch"] == h["branch"]
    assert "test_place_order failed" in retry["brief"] and "Push the fix" in retry["brief"]
    plan_id = next(a["id"] for a in st["test"]["artifacts"] if a["type"] == "test_plan")
    cases = client.get(f"/api/artifacts/{plan_id}").json()["data"]["cases"]
    assert any(c["kind"] == "contract" and "POST /v1/orders" in c["title"] for c in cases)
    assert any(c["kind"] == "acceptance" for c in cases)
    gh["checks"] = [{"name": "pytest", "status": "completed", "conclusion": "success",
                     "summary": "", "url": ""}]
    advance(client, item["id"], "test")
    assert board(client, pid, item["id"])["test"]["state"] == "passed"

    # Review: scorecard flags the consumer-facing endpoint, debug print and TODO.
    r = advance(client, item["id"], "review")
    st = board(client, pid, item["id"])
    review_id = st["review"]["artifacts"][0]["id"]
    card = client.get(f"/api/artifacts/{review_id}").json()["data"]
    titles = {f["title"] for f in card["findings"]}
    assert card["verdict"] == "needs_attention" and card["risk"] == "high"
    assert {"Changes endpoints other repos call", "Debug output left in", "New TODO",
            "No test changes"} <= titles
    assert st["review"]["pending_approvals"][0]["role"] == "engineer"
    assert not gh["comments"]  # posting needs L3+
    assert advance(client, item["id"], "deploy").status_code == 409  # PR not approved
    approve_pending(client, pid, "engineer")

    # Deploy: release always needs the release manager, even at L3.
    r = advance(client, item["id"], "deploy", autonomy="L3")
    st = board(client, pid, item["id"])
    rel_id = st["deploy"]["artifacts"][0]["id"]
    assert st["deploy"]["state"] == "waiting"
    assert st["deploy"]["pending_approvals"][0]["role"] == "release_manager"
    assert client.post(f"/api/releases/{rel_id}/promote", json={}).status_code == 409
    approve_pending(client, pid, "release_manager")
    assert board(client, pid, item["id"])["deploy"]["state"] == "ready"
    out = client.post(f"/api/releases/{rel_id}/promote", json={"by": "rm"}).json()
    assert out["stage"] == "rolling_out" and out["steps"][0]["percent"] == 5  # high risk
    assert board(client, pid, item["id"])["deploy"]["detail"] == "5% live"

    # Operate: an SLO burns during the rollout -> auto rollback, incident, follow-up issue.
    slo = client.post(f"/api/projects/{pid}/slos", json={"title": "Checkout availability",
                                                          "objective": 99.9}).json()
    now = datetime.now(UTC)
    client.post(f"/api/slos/{slo['id']}/samples",
                json=[{"good": 900, "total": 1000, "at": (now - timedelta(minutes=2)).isoformat()}])
    assert advance(client, item["id"], "operate").json()["run"]["agent"] == "sre"
    st = board(client, pid, item["id"])
    assert st["deploy"]["state"] == "rolled_back" and st["operate"]["state"] == "incident"
    assert st["operate"]["pending_approvals"][0]["role"] == "on_call"
    items = board(client, pid)["items"]
    follow = next(i for i in items if i["follow_up_of"])
    assert follow["title"].startswith("Follow-up: Checkout availability")
    assert client.post(f"/api/projects/{pid}/slo-check").json()["actions"] == []  # no duplicates
    inc_id = st["operate"]["artifacts"][0]["id"]
    client.post(f"/api/incidents/{inc_id}/resolve", json={"by": "oncall", "reason": "rolled back"})
    assert board(client, pid, item["id"])["operate"]["state"] == "done"

    # The follow-up goes back through triage, starting from scratch.
    assert [s["state"] for s in follow["stages"]][1:] == ["todo"] * 6
    advance(client, follow["id"])
    assert board(client, pid, follow["id"])["plan"]["state"] in {"waiting", "needs_info"}


def test_retry_cap_and_escalation(client, setup, gh, monkeypatch):
    from filament.config import get_settings

    monkeypatch.setattr(get_settings(), "delivery_max_retries", 1)
    pid = setup["id"]
    item = client.post(f"/api/projects/{pid}/issues", json={"title": "Total in order.placed",
                                                             "body": GOOD_ISSUE}).json()
    advance(client, item["id"])
    approve_pending(client, pid)
    r = advance(client, item["id"], "build", pr_url=PR_URL)  # no design artifacts: allowed
    assert r.status_code == 200
    advance(client, item["id"], "test")
    st = board(client, pid, item["id"])
    assert st["test"]["state"] == "blocked"  # escalated to a person, no retry handoff
    assert sum(1 for a in st["build"]["artifacts"] if a["type"] == "task") == 1


def test_checks_and_guards():
    t = checks.triage_score("Crash on refund", "It crashes.", ["bug"])
    assert t["status"] == "not_ready" and "Has steps to reproduce" in t["missing"]
    dup = checks.triage_score("Refund button crashes checkout", "",
                              others=[("a1", "Checkout crashes on refund button")])
    assert dup["possible_duplicates"] == ["a1"] and dup["status"] != "ready"
    card = checks.review_findings([{"filename": "app/settings.py", "additions": 1, "deletions": 0,
                                    "patch": "@@ -1 +1,2 @@\n"
                                             "+API_KEY = 'sk-live-abcdefghijklmnopqrstuv'"}],
                                  None)
    assert card["verdict"] == "request_changes" and card["findings"][0]["line"] == 1
    assert checks.retry_decision("failing", 3, 3)["action"] == "escalate"
    nodes = [{"kind": "code", "callable": True, "file": "app/orders.py", "label": lb}
             for lb in ("place_order()", "cancel_order()", "list_carts()")]
    top = checks.start_symbols(nodes, "Include total in order.placed", "")
    assert [n["label"] for n in top][:2] == ["place_order()", "cancel_order()"]
    assert checks.classify_checks([{"name": "eslint", "status": "completed",
                                    "conclusion": "failure"}])["failed"][0]["kind"] == "lint"
    now = datetime.now(UTC)
    rates = checks.burn_rates([{"at": now - timedelta(minutes=1), "good": 990, "total": 1000}],
                              99.9, now)
    assert rates["5m"] == 10.0 and checks.slo_status({"1h": 7, "6h": 7})["state"] == "warning"
    from filament.agents.tools import DELEGABLE

    assert DELEGABLE == {"requirements", "flow"}  # orchestrators cannot skip checkpoints


def test_omni_task_tools(client, setup, gh):
    pid, ws_id = setup["id"], setup["workspace_id"]
    item = client.post(f"/api/projects/{pid}/issues", json={"title": "Total in order.placed",
                                                             "body": GOOD_ISSUE}).json()
    advance(client, item["id"])
    approve_pending(client, pid)
    advance(client, item["id"], "build")

    def call(name, args):
        r = client.post(f"/api/workspaces/{ws_id}/mcp/omni", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}).json()
        return r["result"]["structuredContent"]

    tools = client.post(f"/api/workspaces/{ws_id}/mcp/omni", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert {"filament_get_task", "filament_report_pr"} <= {t["name"] for t in tools}
    task_id = call("filament_list_tasks", {})["tasks"][0]["task_id"]
    assert "Acceptance criteria" in call("filament_get_task", {"task_id": task_id})["brief"]
    assert call("filament_report_pr", {"task_id": task_id, "url": PR_URL})["linked"]
    assert call("filament_list_tasks", {})["tasks"] == []
    assert board(client, pid, item["id"])["build"]["state"] == "done"
