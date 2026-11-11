"""Record a delivery-pipeline scenario for the interactive preview.

Runs the real backend (offline agents, GitHub mocked) through one work item's journey, from
triage to an SLO breach that rolls back the release and files a follow-up, and writes every
step's response and resulting board to frontend/src/demo-delivery.json. The preview plays the
steps back in order, so what it shows is what Ignite actually produces.

    .venv/bin/python scripts/demo_delivery.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ["IGNITE_DATABASE_URL"] = "sqlite://"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["IGNITE_REPO_SCHEDULER_ENABLED"] = "false"
os.environ["IGNITE_DATA_DIR"] = tempfile.mkdtemp(prefix="ignite-demo-")
sys.path.insert(0, str(Path(__file__).parent))

from demo_repos import REPOS, write  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from igniteai.db import Base, get_session, make_engine  # noqa: E402
from igniteai.delivery import github  # noqa: E402
from igniteai.main import create_app  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "demo-delivery.json"
PR_URL = "https://github.com/acme/checkout-api/pull/42"
ISSUE = (
    "When a customer places an order on checkout-api, fulfilment needs the cart total in the "
    "order.placed event so it can pick the shipping tier without calling us back.\n\n"
    "## Acceptance criteria\n- [ ] order.placed includes total_cents\n"
    "- [ ] web-storefront and fulfilment keep working unchanged\n"
    "- [ ] An alert fires if order.placed stops being published\n")
GH = {"checks": [{"name": "pytest", "status": "completed", "conclusion": "failure",
                  "summary": "tests/test_orders.py::test_place_order_event - KeyError: "
                             "'total_cents'", "url": ""},
                 {"name": "ruff", "status": "completed", "conclusion": "success", "summary": "",
                  "url": ""}]}
PATCH = ("@@ -12,4 +12,6 @@\n     \"\"\"Turn a cart into an order and emit order.placed.\"\"\"\n"
         "     order = save_order(body.cart_id)\n"
         "+    print('placing', order)\n"
         "+    order['total_cents'] = cart_total(body.cart_id)  # TODO: currency\n"
         "     publish_order_event('order.placed', order)\n     return order\n")


def fake_pr(url: str) -> dict:
    return {"url": url, "repo": "acme/checkout-api", "number": 42,
            "title": "Include total_cents in order.placed", "body": GH.get("body", ""),
            "branch": GH.get("branch", "feature"), "head_sha": "9f2c1e7", "state": "open",
            "merged": False, "author": "claude",
            "files": [{"filename": "app/routers/orders.py", "status": "modified", "additions": 2,
                       "deletions": 0, "patch": PATCH}]}


github.pull_request = fake_pr
github.check_runs = lambda url, sha=None: GH["checks"]


def main() -> None:
    engine = make_engine("sqlite://", poolclass=StaticPool)
    from igniteai import models  # noqa: F401

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = create_app()
    app.dependency_overrides[get_session] = override
    tmp = Path(tempfile.mkdtemp())
    with TestClient(app) as c:
        ws = c.post("/api/workspaces", json={"name": "Acme"}).json()
        pid = c.post(f"/api/workspaces/{ws['id']}/projects",
                     json={"name": "Checkout v2", "description": ""}).json()["id"]
        for ref in ("github.com/acme/checkout-api", "github.com/acme/web-storefront"):
            root = tmp / "acme" / ref.rsplit("/", 1)[-1]
            write(root, REPOS[ref])
            c.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": str(root)})
        # Coding conventions the handoff carries (confirmed shared memory).
        for text in ["Money is integer cents (total_cents), never floats.",
                     "Events keep their existing fields; add new ones, never rename."]:
            m = c.post(f"/api/workspaces/{ws['id']}/memory",
                       json={"scope": "org", "kind": "standard", "content": text}).json()
            c.post(f"/api/memory/{m['id']}/confirm")
        item = c.post(f"/api/projects/{pid}/issues",
                      json={"title": "Include the cart total in order.placed",
                            "body": ISSUE}).json()["id"]
        vague = c.post(f"/api/projects/{pid}/issues",
                       json={"title": "Checkout feels slow sometimes",
                             "body": "Customers say it is slow."}).json()["id"]
        slo = c.post(f"/api/projects/{pid}/slos",
                     json={"title": "Checkout availability", "objective": 99.9,
                           "service": "checkout-api"}).json()["id"]
        now = datetime.now(UTC)
        c.post(f"/api/slos/{slo}/samples", json=[
            {"good": 99_990, "total": 100_000, "at": (now - timedelta(minutes=m)).isoformat()}
            for m in (300, 200, 90)])

        steps: list[dict] = []
        errors: dict[str, str] = {}

        def board():
            return c.get(f"/api/projects/{pid}/delivery").json()

        def step(key: str, hint: str, fn, covers: list[str] | None = None):
            resp = fn()
            assert resp.status_code < 400, (key, resp.status_code, resp.text)
            body = resp.json()
            if isinstance(body, dict) and "run" in body:
                body["run"] = {k: v for k, v in body["run"].items()
                               if k not in {"trace", "context_pack", "config"}}
            steps.append({"key": key, "hint": hint, "covers": covers or [], "response": body,
                          "board": board()})

        def gate(key: str, fn):
            r = fn()
            assert r.status_code == 409, (key, r.status_code)
            errors[key] = r.json()["detail"]

        def adv(i, stage, **kw):
            return lambda: c.post(f"/api/work-items/{i}/advance",
                                  json={"stage": stage, "requested_by": "web", **kw})

        def pending(role):
            return [a["artifact_id"] for a in c.get(f"/api/projects/{pid}/approvals").json()
                    if a["role"] == role]

        def approve(role, hint, stage):
            ids = pending(role)
            assert ids, role

            def go():
                last = None
                for a in ids:
                    last = c.post(f"/api/artifacts/{a}/approve",
                                  json={"decision": "approved", "decided_by": "web"})
                return last
            step(f"approve:{stage}", hint, go, ids)

        def stage_arts(i, key):
            items = {x["id"]: x for x in board()["items"]}
            return next(s for s in items[i]["stages"] if s["key"] == key)["artifacts"]

        # The vague item is already triaged when the preview opens: it shows a low score.
        adv(vague, "plan")()
        initial = board()
        step(f"advance:{item}:plan", "Run the Triage Agent (Plan) on 'Include the cart total'",
             adv(item, "plan"))
        gate(f"advance:{item}:design", adv(item, "design"))
        approve("product_owner", "Approve the scope (product owner checkpoint)", "plan")
        step(f"advance:{item}:design", "Run the Architect Agent (Design)", adv(item, "design"))
        gate(f"advance:{item}:build", adv(item, "build"))
        approve("tech_lead", "Approve the design (tech lead checkpoint)", "design")
        step(f"advance:{item}:build", "Run the Coding Agent (Build)",
             adv(item, "build", target="claude_code"))
        task = next(a["id"] for a in stage_arts(item, "build") if a["type"] == "task")
        gate(f"advance:{item}:test", adv(item, "test"))
        handoff = c.get(f"/api/tasks/{task}/handoff").json()
        GH["branch"] = handoff["branch"]
        step(f"linkpr:{task}", "Link the pull request Claude Code opened",
             lambda: c.post(f"/api/tasks/{task}/pull-request", json={"url": PR_URL}))
        step(f"advance:{item}:test", "Run the QA Agent (Test)", adv(item, "test"))
        GH["checks"] = [{"name": "pytest", "status": "completed", "conclusion": "success",
                         "summary": "", "url": ""},
                        {"name": "ruff", "status": "completed", "conclusion": "success",
                         "summary": "", "url": ""}]
        step(f"advance:{item}:test", "Run the QA Agent again: the fix is pushed",
             adv(item, "test"))
        step(f"advance:{item}:review", "Run the Review Agent", adv(item, "review"))
        gate(f"advance:{item}:deploy", adv(item, "deploy"))
        approve("engineer", "Approve the PR (engineer checkpoint)", "review")
        step(f"advance:{item}:deploy", "Run the Release Agent (Deploy)", adv(item, "deploy"))
        release = stage_arts(item, "deploy")[0]["id"]
        gate(f"promote:{release}",
             lambda: c.post(f"/api/releases/{release}/promote", json={"by": "web"}))
        approve("release_manager", "Approve the production release (release manager)", "deploy")
        step(f"promote:{release}", "Promote the release to its first step",
             lambda: c.post(f"/api/releases/{release}/promote", json={"by": "web"}))
        # An error spike starts during the 5% rollout.
        c.post(f"/api/slos/{slo}/samples",
               json=[{"good": 9_600, "total": 10_000,
                      "at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}])
        step(f"advance:{item}:operate", "Run the SRE Agent: an error spike started",
             adv(item, "operate"))
        incident = next(a["id"] for a in stage_arts(item, "operate") if a["type"] == "incident")
        follow = next(i["id"] for i in board()["items"] if i["follow_up_of"])
        step(f"advance:{follow}:plan", "Triage the follow-up issue the SRE Agent filed",
             adv(follow, "plan"))
        step(f"resolve:{incident}", "Resolve the incident (on-call)",
             lambda: c.post(f"/api/incidents/{incident}/resolve",
                            json={"by": "web", "reason": "Rolled back; fix tracked in follow-up"}))

        ids = set()
        for s in [initial, *[x["board"] for x in steps]]:
            for i in s["items"]:
                ids.add(i["id"])
                for st in i["stages"]:
                    ids |= {a["id"] for a in st["artifacts"]}
        artifacts = {a: c.get(f"/api/artifacts/{a}").json() for a in sorted(ids)}
        tasks = [a for a, v in artifacts.items() if v["type"] == "task"]
        handoffs = {t: c.get(f"/api/tasks/{t}/handoff").json() for t in tasks}
    OUT.write_text(json.dumps({"initial": initial, "steps": steps, "errors": errors,
                               "artifacts": artifacts, "handoffs": handoffs}, default=str))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB): {len(steps)} steps, "
          f"{len(artifacts)} artifacts, gates: {list(errors.values())}")
    for s in steps:
        it = next((i for i in s["board"]["items"] if i["id"] == item), None)
        print(f"  {s['key'].split(':')[0]:<8} {s['hint'][:50]:<50} " +
              " ".join(f"{x['key'][:4]}={x['state']}" for x in it["stages"]))


if __name__ == "__main__":
    main()
