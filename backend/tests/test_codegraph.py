# ruff: noqa: E501  (fixture source files are embedded verbatim)
"""Code graph (Graphify): handler-level services, notes as memory, explain/path/query,
change and PR impact, conflicts, data model, agent and Omni tools, incremental scans."""

from pathlib import Path

import pytest

from filament.repos import codegraph, codequery
from tests.test_config import register, run, scripted  # noqa: F401

pytestmark = pytest.mark.skipif(codegraph.engine_path() is None, reason="graphify not installed")

FILES = {
    "pyproject.toml": '[project]\nname="checkout-api"\ndependencies=["fastapi","kafka-python","stripe","sqlalchemy"]\n',
    "app/__init__.py": "",
    "app/events.py": (
        "from kafka import KafkaProducer\n"
        "producer = KafkaProducer()\n\n"
        "def publish(topic, payload):\n"
        "    # WHY: fulfilment listens on order.placed; keep payloads small (ADR-7).\n"
        "    producer.send(topic, payload)\n"
    ),
    "app/store.py": (
        "def save_order(cart_id):\n    return {'id': cart_id}\n\n"
        "def load_cart(cart_id):\n    return {'id': cart_id}\n"
    ),
    "app/main.py": (
        "import stripe\n"
        "from fastapi import FastAPI, APIRouter\n"
        "from app.events import publish\n"
        "from app.store import save_order, load_cart\n\n"
        "app = FastAPI()\nrouter = APIRouter(prefix='/orders')\n\n"
        "@router.post('')\ndef place_order(cart_id: str):\n"
        "    \"\"\"Turn a cart into an order.\"\"\"\n"
        "    cart = load_cart(cart_id)\n    order = save_order(cart['id'])\n"
        "    publish('order.placed', order)\n    return order\n\n"
        "@router.post('/{order_id}/refund')\ndef refund(order_id: str):\n"
        "    # NOTE: refunds go straight to Stripe.\n"
        "    return stripe.Refund.create(payment_intent=order_id)\n\n"
        "@router.get('/{order_id}')\ndef get_order(order_id: str):\n"
        "    return load_cart(order_id)\n\n"
        "app.include_router(router, prefix='/v1')\n"
    ),
    "app/admin.py": "def get_order(order_id):\n    return None\n",
    "migrations/001_init.sql": (
        "CREATE TABLE orders (id uuid PRIMARY KEY, cart_id uuid NOT NULL, total numeric);\n"
        "CREATE TABLE refunds (id serial PRIMARY KEY, order_id uuid NOT NULL REFERENCES orders(id));\n"
    ),
}
WEB = {
    "package.json": '{"name":"web-storefront","dependencies":{"react":"19"}}',
    "src/api.ts": "const BASE = process.env.REACT_APP_CHECKOUT_API_URL\n"
                  "export const place = () => fetch(`${BASE}/v1/orders`, { method: 'POST' })\n",
}


def write(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


@pytest.fixture
def repos(client, ws, tmp_path):
    # An ADR that the code's notes cite, so notes link to it.
    proj = client.post(f"/api/workspaces/{ws['id']}/projects", json={"name": "Arch", "description": ""}).json()
    client.post(f"/api/projects/{proj['id']}/artifacts", json={"type": "adr", "title": "ADR-7: Small events",
                                                               "content": "Keep events small."})
    api_root = write(tmp_path / "acme" / "checkout-api", FILES)
    api = client.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": str(api_root)}).json()
    web = client.post(f"/api/workspaces/{ws['id']}/repos",
                      json={"repo": str(write(tmp_path / "acme" / "web-storefront", WEB))}).json()
    return api, web, api_root


def test_normalize_and_schema_guard():
    raw = {"graph": {"schema_version": 1, "graphify_version": "x"}, "nodes": [
        {"id": "a", "label": "a()", "file_type": "code", "source_file": "m.py", "source_location": "L3", "_callable": True},
        {"id": "b", "label": "b()", "file_type": "code", "source_file": "m.py", "source_location": "L9"}],
        "links": [{"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
                  {"source": "a", "target": "missing", "relation": "calls"}]}
    g = codegraph.normalize(raw)
    assert [e["relation"] for e in g["edges"]] == ["calls"] and g["nodes"][0]["line"] == 3
    assert g["warnings"] == []
    assert codegraph.normalize({**raw, "graph": {"schema_version": 9}})["warnings"]


def test_handler_level_services_notes_and_subsystems(client, ws, repos):
    api, _, _ = repos
    code = api["analysis"]["code"]
    assert code["engine"] == "graphify" and code["symbols"] >= 6
    place = code["endpoints"]["POST /v1/orders"]["services"]
    assert [(s["label"], s["via"]) for s in place] == [("Kafka", "publish()")]
    refund = code["endpoints"]["POST /v1/orders/{order_id}/refund"]["services"]
    assert [(s["label"], s["via"]) for s in refund] == [("Stripe", "direct")]
    assert code["endpoints"]["GET /v1/orders/{order_id}"]["services"] == []
    notes = {n["tag"]: n for n in code["rationale"]}
    assert notes["WHY"]["refs"] == ["ADR-7"] and notes["NOTE"]["text"] == "refunds go straight to Stripe."
    assert code["communities"] and code["hotspots"][0]["fan_in"] >= 1
    # Notes become proposed memory, linked to the ADR they cite.
    mem = client.get(f"/api/workspaces/{ws['id']}/memory").json()
    why = next(m for m in mem if "fulfilment listens" in m["content"])
    assert why["status"] == "proposed" and why["kind"] == "decision"
    # Request flow diagram draws calls only where the code shows them.
    flows = client.get(f"/api/artifacts/{api['generated']['request_flows']}").json()["content"]
    assert "S->>kafka: publish via publish()" in flows and "S->>stripe: call" in flows
    assert "module uses" not in flows


def test_explain_path_query_and_ambiguity(client, repos):
    api, _, _ = repos
    base = f"/api/repos/{api['id']}/code"
    ex = client.get(f"{base}/explain", params={"name": "place_order"}).json()
    assert ex["serves_endpoints"] == ["POST /v1/orders"]
    assert {c["node"]["label"] for c in ex["calls"]} >= {"publish()", "save_order()", "load_cart()"}
    amb = client.get(f"{base}/explain", params={"name": "get_order"})
    assert amb.status_code == 409 and len(amb.json()["candidates"]) == 2
    exact = client.get(f"{base}/explain", params={"name": amb.json()["candidates"][0]["id"]})
    assert exact.status_code == 200
    miss = client.get(f"{base}/explain", params={"name": "publsh"})
    assert miss.status_code == 404
    p = client.get(f"{base}/path", params={"a": "place_order", "b": "publish"}).json()
    assert p["found"] and p["hops"][0]["relation"] == "calls" and p["confidence"] == "extracted"
    q = client.get(f"{base}/query", params={"q": "how is an order saved"}).json()
    assert any(s["label"] == "save_order()" for s in q["seeds"])
    g = client.get(f"{base}/graph").json()
    assert g["mode"] == "subsystems" and g["nodes"]
    one = client.get(f"{base}/graph", params={"community": g["nodes"][0]["community"]}).json()
    assert one["mode"] == "symbols" and any(n["inside"] for n in one["nodes"])


def test_change_impact_from_diff_reaches_endpoints_and_consumers(client, repos):
    api, _, _ = repos
    diff = ("diff --git a/app/events.py b/app/events.py\n--- a/app/events.py\n+++ b/app/events.py\n"
            "@@ -4,3 +4,4 @@ def publish(topic, payload):\n+    payload = dict(payload)\n")
    r = client.post(f"/api/repos/{api['id']}/code/changes", json={"diff": diff}).json()
    assert [c["label"] for c in r["changed"]] == ["publish()"]
    assert any(a["label"] == "place_order()" for a in r["affected"])
    assert r["endpoints"] == ["POST /v1/orders"]
    assert r["consumers"] == [{"repo": "web-storefront", "endpoints": ["POST /v1/orders"]}]
    assert r["risk"] == "high"
    files = client.post(f"/api/repos/{api['id']}/code/changes", json={"files": ["app/store.py"]}).json()
    assert {"GET /v1/orders/{order_id}", "POST /v1/orders"} <= set(files["endpoints"])
    c = client.post(f"/api/repos/{api['id']}/code/conflicts", json={"changes": [
        {"name": "PR 1", "files": ["app/store.py"]}, {"name": "PR 2", "files": ["app/store.py"]},
        {"name": "PR 3", "files": ["app/admin.py"]}]}).json()
    pair = next(x for x in c if {x["a"], x["b"]} == {"PR 1", "PR 2"})
    assert pair["severity"] == "conflict" and "save_order()" in pair["same_symbols"]
    assert not any("PR 3" in (x["a"], x["b"]) and x["severity"] == "conflict" for x in c)


def test_pr_impact_reads_github_files(client, repos, monkeypatch):
    api, _, _ = repos

    class Resp:
        status_code = 200

        def json(self):
            return [{"filename": "app/events.py", "patch": "@@ -5,2 +5,3 @@\n+    x = 1"}]

    seen = {}
    monkeypatch.setattr("httpx.get", lambda url, **kw: seen.setdefault("url", url) and Resp())
    r = client.post(f"/api/repos/{api['id']}/code/changes",
                    json={"pr": "https://github.com/acme/checkout-api/pull/42"}).json()
    assert seen["url"].endswith("/repos/acme/checkout-api/pulls/42/files")
    assert r["endpoints"] == ["POST /v1/orders"]


def test_data_model(client, repos):
    api, _, _ = repos
    dm = client.get(f"/api/repos/{api['id']}/data-model").json()
    assert {e["name"] for e in dm["entities"]} == {"orders", "refunds"}
    assert dm["relations"][0] == {"from": "refunds", "to": "orders", "field": "order_id",
                                  "kind": "many-to-one", "file": "migrations/001_init.sql"}
    assert "erDiagram" in dm["mermaid"] and "data_model" in api["generated"]


def test_agent_and_omni_code_tools(client, ws, repos, register):  # noqa: F811
    api, _, _ = repos
    seen = {}

    def script(agent):
        res = yield [("code_explain", {"repo": "checkout-api", "name": "refund"}),
                     ("code_explain", {"repo": "checkout-api", "name": "get_order"}),
                     ("code_impact", {"repo": "checkout-api", "symbols": ["publish"]})]
        seen["res"] = res
        yield [("finish", {"summary": "ok"})]

    register(scripted("coder", script, uses_mcp=False))
    out = run(client, api["project_id"], "coder")
    assert out["status"] == "succeeded", out["error"]
    refund, amb, impact = seen["res"]
    assert refund["uses_services"][0]["label"] == "Stripe"
    assert amb["ambiguous"] is True and len(amb["candidates"]) == 2
    assert impact["endpoints"] == ["POST /v1/orders"]
    omni = client.post(f"/api/workspaces/{ws['id']}/mcp/omni", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "code_path", "arguments": {"repo": "checkout-api", "from": "place_order",
                                                      "to": "publish"}}}).json()
    assert omni["result"]["isError"] is False and omni["result"]["structuredContent"]["found"]


def test_scheduled_rescan_skips_unchanged_commit(client, repos, monkeypatch):
    api, _, _ = repos
    from filament.db import get_session  # noqa: F401
    from filament.repos import service

    monkeypatch.setattr("filament.readiness.scanner.remote_head", lambda ref: "abc123")
    calls = []
    monkeypatch.setattr(service, "analyze", lambda ref: calls.append(ref) or {})
    # Pretend the last scan saw the same commit, then run a scheduled scan.
    gen = client.app.dependency_overrides
    session = next(next(iter(gen.values()))())
    from filament.models import RepoRecord

    record = session.get(RepoRecord, api["id"])
    record.commit = "abc123"
    scan = service.scan_record(session, record, "schedule")
    session.commit()
    assert scan.changes["skipped"] is True and calls == []


def test_codequery_parse_diff():
    d = codequery.parse_diff("diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1,2 +10,3 @@\n+a\n@@ -20 +30 @@\n+b\n")
    assert d == {"x.py": [(10, 12), (30, 30)]}
