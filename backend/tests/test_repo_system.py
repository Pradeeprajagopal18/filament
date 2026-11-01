# ruff: noqa: E501  (fixture source files are embedded verbatim)
"""System map, agent repo context, rescans with drift/breaking changes, richer contracts."""

from pathlib import Path

import pytest

from filament.readiness.scanner import Repo
from filament.repos import api_extract, diff

API_V1 = '''
from typing import Optional
from fastapi import FastAPI, APIRouter, Depends, HTTPException
from pydantic import BaseModel

class CartItem(BaseModel):
    sku: str
    quantity: int = 1

class Cart(BaseModel):
    id: str
    items: list[CartItem]
    total: float
    coupon: Optional[str] = None

class AddItem(BaseModel):
    sku: str
    quantity: int = 1

def get_current_user(): ...

app = FastAPI()
router = APIRouter(prefix="/carts")

@router.get("/{cart_id}")
def get_cart(cart_id: str) -> Cart:
    raise HTTPException(status_code=404, detail="no cart")

@router.post("/{cart_id}/items", response_model=Cart)
def add_item(cart_id: str, body: AddItem, user=Depends(get_current_user)):
    return {}

@router.delete("/{cart_id}")
def delete_cart(cart_id: str):
    return None

app.include_router(router, prefix="/v1")
'''

STOREFRONT = {
    "package.json": '{"name":"web-storefront","dependencies":{"react":"19","react-router-dom":"7"}}',
    "src/api.ts": (
        "const BASE = process.env.REACT_APP_CHECKOUT_API_URL\n"
        "export const getCart = (id: string) => fetch(`${BASE}/v1/carts/${id}`)\n"
        "export const addItem = (id: string, sku: string) =>\n"
        "  fetch(`${BASE}/v1/carts/${id}/items`, { method: 'POST', body: JSON.stringify({ sku }) })\n"
    ),
    "src/App.tsx": "import { Routes, Route } from 'react-router-dom'\nimport Cart from './Cart'\n"
                   "export default function App() {\n  return (\n    <Routes>\n"
                   "      <Route path=\"/cart\" element={<Cart />} />\n    </Routes>\n  )\n}\n",
    "src/Cart.tsx": "export default function Cart() {\n  return (\n    <main>\n"
                    "      <h1>Your cart</h1>\n      <button>Checkout</button>\n    </main>\n  )\n}\n",
}
INFRA = {"main.tf": 'resource "aws_db_instance" "checkout" { engine = "postgres" }\n'
                    'resource "aws_s3_bucket" "assets" {}\n'}
CONFIG = {"envs/dev/checkout-api.yaml": "replicas: 1\n", "envs/prod/checkout-api.yaml":
          "replicas: 3\n", "envs/prod/flags.json": "{}"}


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


@pytest.fixture
def company(tmp_path):
    base = tmp_path / "acme"
    api = write(base / "checkout-api", {
        "pyproject.toml": '[project]\nname="checkout-api"\ndependencies=["fastapi","pydantic"]\n',
        "app/main.py": API_V1})
    return {"api": api, "web": write(base / "web-storefront", STOREFRONT),
            "infra": write(base / "platform-infra", INFRA),
            "config": write(base / "service-config", CONFIG)}


def onboard(client, ws, path):
    r = client.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": str(path)})
    assert r.status_code == 201, r.text
    return r.json()


# -- richer contracts -------------------------------------------------------------------------
def test_fastapi_contract_has_models_auth_errors_examples(company):
    api = api_extract.extract(Repo(company["api"]), "checkout-api")
    eps = {f"{e['method']} {e['path']}": e for e in api["endpoints"]}
    add = eps["POST /v1/carts/{cart_id}/items"]
    assert add["request"]["schema"] == {"$ref": "#/components/schemas/AddItem"}
    assert add["auth"] == "bearer" and "401" in add["responses"]
    assert add["example_request"] == {"sku": "SKU-1001", "quantity": 2}
    get = eps["GET /v1/carts/{cart_id}"]
    assert "404" in get["responses"]
    assert get["example_response"]["items"][0]["sku"] == "SKU-1001"
    doc = api["openapi"]
    assert set(doc["components"]["schemas"]) >= {"Cart", "CartItem", "AddItem"}
    assert doc["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert doc["paths"]["/v1/carts/{cart_id}/items"]["post"]["security"] == [{"bearerAuth": []}]
    assert doc["components"]["schemas"]["Cart"]["required"] == ["id", "items", "total"]


def test_express_request_fields_and_auth(tmp_path):
    root = write(tmp_path / "svc", {
        "package.json": '{"name":"svc","dependencies":{"express":"4"}}',
        "server.js": "const express = require('express')\nconst app = express()\n"
                     "app.post('/orders', requireAuth, (req, res) => {\n"
                     "  const { sku, qty } = req.body\n  if (!sku) return res.status(400).end()\n"
                     "  res.status(201).json({})\n})\n"
                     "app.get('/orders', (req, res) => res.json(req.query.status))\n"})
    eps = {f"{e['method']} {e['path']}": e for e in api_extract.extract(Repo(root))["endpoints"]}
    post = eps["POST /orders"]
    assert post["auth"] == "bearer"
    assert set(post["request"]["schema"]["properties"]) == {"sku", "qty"}
    assert {"201", "400", "401"} <= set(post["responses"])
    assert [p["name"] for p in eps["GET /orders"]["params"]] == ["status"]


def test_contract_drift_against_code(company):
    write(company["api"], {"openapi.yaml": (
        "openapi: 3.0.0\ninfo: {title: x, version: '1'}\npaths:\n"
        "  /v1/carts/{id}:\n    get: {responses: {'200': {description: ok}}}\n"
        "  /v1/coupons:\n    post: {responses: {'201': {description: ok}}}\n")})
    api = api_extract.extract(Repo(company["api"]), "checkout-api")
    assert api["source"] == "spec"
    missing_in_code = {(d["method"], d["path"]) for d in api["drift"]["in_contract_not_in_code"]}
    undocumented = {(d["method"], d["path"]) for d in api["drift"]["in_code_not_in_contract"]}
    assert ("POST", "/v1/coupons") in missing_in_code
    assert ("POST", "/v1/carts/{cart_id}/items") in undocumented
    assert ("GET", "/v1/carts/{cart_id}") not in undocumented  # same endpoint, other param name


# -- system map, impact and agent context ---------------------------------------------------------
def test_system_map_links_repos_with_evidence(client, ws, company):
    repos = {k: onboard(client, ws, p) for k, p in company.items()}
    m = client.get(f"/api/workspaces/{ws['id']}/system-map").json()
    edges = {(e["source"], e["kind"], e["target"]): e for e in m["edges"]}
    api, web = repos["api"]["id"], repos["web"]["id"]
    calls = edges[(web, "calls", api)]
    assert set(calls["endpoints"]) == {"GET /v1/carts/{cart_id}", "POST /v1/carts/{cart_id}/items"}
    assert any(ev["file"] == "src/api.ts" for ev in calls["evidence"])
    assert (repos["config"]["id"], "configures", api) in edges
    assert (repos["infra"]["id"], "provisions", api) in edges
    assert "flowchart LR" in m["mermaid"]

    imp = client.get(f"/api/repos/{api}/impact").json()
    assert {d["repo"] for d in imp["dependents"]} == {"web-storefront", "service-config",
                                                       "platform-infra"}
    assert imp["endpoint_consumers"]["GET /v1/carts/{cart_id}"][0]["repo"] == "web-storefront"

    ctx = client.get(f"/api/repos/{api}/context").json()
    assert "GET /v1/carts/{cart_id}" in ctx["text"] and "web-storefront (calls" in ctx["text"]


def test_agents_use_repo_context(client, ws, company):
    for p in company.values():
        onboard(client, ws, p)
    project = client.post(f"/api/workspaces/{ws['id']}/projects",
                          json={"name": "Saved carts", "description": ""}).json()
    run = client.post(f"/api/projects/{project['id']}/runs", json={
        "agent": "requirements", "goal": "PRD",
        "input": {"idea": "Let shoppers keep their cart items and add item quantity from the cart"},
    }).json()
    assert run["status"] == "succeeded", run.get("error")
    pack_kinds = {i["kind"] for i in run["context_pack"]["items"]}
    assert "repo" in pack_kinds
    tools = [e.get("tool") for e in run["trace"] if e["type"] == "tool"]
    assert "search_repos" in tools and "get_repo" in tools
    prd = client.get(f"/api/artifacts/{run['output']['prd_id']}").json()
    assert "## Affected systems" in prd["content"]
    assert "checkout-api" in prd["content"] and "/v1/carts" in prd["content"]


# -- rescans: history, breaking changes, references -------------------------------------------
def test_rescan_detects_breaking_changes_and_refreshes_references(client, ws, company):
    api = onboard(client, ws, company["api"])
    onboard(client, ws, company["web"])
    g = client.post(f"/api/repos/{api['id']}/generate", json={"items": ["api_contract",
                                                                          "agents_md"]}).json()
    contract_id = g["generated"]["api_contract"]
    client.post(f"/api/artifacts/{contract_id}/approve", json={"decision": "approved"})

    # The team ships a change: GET cart removed, AddItem gains a required field.
    src = (company["api"] / "app/main.py").read_text()
    src = src.replace('@router.get("/{cart_id}")\ndef get_cart(cart_id: str) -> Cart:\n'
                      '    raise HTTPException(status_code=404, detail="no cart")\n', "")
    src = src.replace("class AddItem(BaseModel):\n    sku: str\n",
                      "class AddItem(BaseModel):\n    sku: str\n    store_id: str\n")
    (company["api"] / "app/main.py").write_text(src)

    r = client.post(f"/api/repos/{api['id']}/rescan")
    assert r.status_code == 200
    imp = client.get(f"/api/repos/{api['id']}/impact").json()
    assert {(u["repo"], u["method"], u["path"]) for u in imp["unresolved_calls"]} == {
        ("web-storefront", "GET", "/v1/carts/{}")}  # the storefront still calls the removed route
    scans = client.get(f"/api/repos/{api['id']}/scans").json()
    assert [s["trigger"] for s in scans] == ["manual", "onboard"]
    ch = scans[0]["changes"]
    assert "GET /v1/carts/{cart_id}" in ch["endpoints_removed"]
    breaking = {(b["endpoint"], b["change"]): b for b in ch["breaking"]}
    removed = breaking[("GET /v1/carts/{cart_id}", "endpoint removed")]
    assert removed["consumers"] == ["web-storefront"]
    assert ("POST /v1/carts/{cart_id}/items", "new required request field store_id") in breaking

    # Draft reference refreshed in place; approved one waits for review.
    assert "agents_md" in ch["references"]["unchanged"] + ch["references"]["refreshed"]
    assert ch["references"]["pending_review"] == ["api_contract"]
    contract = client.get(f"/api/artifacts/{contract_id}").json()
    assert contract["status"] == "approved" and contract["data"]["stale"] is True
    accepted = client.post(f"/api/repos/{api['id']}/references/api_contract/accept").json()
    assert accepted["status"] == "in_review" and accepted["version"] == contract["version"] + 1
    assert "store_id" in accepted["content"]
    assert client.post(f"/api/repos/{api['id']}/references/api_contract/accept").status_code == 409

    gaps = client.get(f"/api/workspaces/{ws['id']}/signals?kind=breaking_change")
    if gaps.status_code == 200:
        assert any("web-storefront" in s["text"] for s in gaps.json())


def test_scan_schedule_and_due(client, ws, company):
    api = onboard(client, ws, company["api"])
    assert api["next_scan_at"] is not None  # default interval
    off = client.patch(f"/api/repos/{api['id']}", json={"scan_interval_hours": 0}).json()
    assert off["next_scan_at"] is None
    assert client.post("/api/repos/scan-due").json()["scanned"] == []


def test_diff_compare_unit():
    old = {"api": {"endpoints": [{"method": "GET", "path": "/a/{id}", "params": []}]},
           "readiness": {"checks": [{"id": "ci", "status": "fail", "title": "CI"}], "score": 50}}
    new = {"api": {"endpoints": [{"method": "GET", "path": "/a/{x}", "auth": "bearer",
                                  "params": [{"name": "q", "in": "query", "required": True}]},
                                 {"method": "POST", "path": "/b", "params": []}]},
           "readiness": {"checks": [{"id": "ci", "status": "pass", "title": "CI"}], "score": 60}}
    ch = diff.compare(old, new)
    assert ch["endpoints_added"] == ["POST /b"] and ch["checks_fixed"] == ["CI"]
    assert {b["change"] for b in ch["breaking"]} == {"new required query parameter q",
                                                     "now requires authentication"}
    assert ch["score_delta"] == 10


def test_github_webhook_rescans(client, ws, company):
    api = onboard(client, ws, company["api"])
    r = client.post("/api/webhooks/github", headers={"X-GitHub-Event": "push"},
                    json={"ref": "refs/heads/main",
                          "repository": {"full_name": "acme/checkout-api",
                                         "default_branch": "main"}})
    assert r.status_code == 202 and r.json()["queued"] == [api["id"]]
    scans = client.get(f"/api/repos/{api['id']}/scans").json()
    assert scans[0]["trigger"] == "webhook"
    other = client.post("/api/webhooks/github", headers={"X-GitHub-Event": "push"},
                        json={"ref": "refs/heads/feature",
                              "repository": {"full_name": "acme/checkout-api",
                                             "default_branch": "main"}})
    assert other.json() == {"ignored": "not the default branch"}


# -- API simulator and model enrichment ----------------------------------------------------------
def test_mock_server_and_enrichment(client, ws, company):
    api = onboard(client, ws, company["api"])
    got = client.get(f"/api/repos/{api['id']}/mock/v1/carts/c_9")
    assert got.status_code == 200 and got.json()["items"][0]["sku"] == "SKU-1001"
    assert got.headers["X-Filament-Mock"] == "GET /v1/carts/{cart_id}"
    assert client.get(f"/api/repos/{api['id']}/mock/v1/nope").status_code == 404

    enriched = client.post(f"/api/repos/{api['id']}/enrich-api").json()
    eps = {f"{e['method']} {e['path']}": e for e in enriched["analysis"]["api"]["endpoints"]}
    assert eps["POST /v1/carts/{cart_id}/items"]["summary"] == "Add item"
    assert enriched["analysis"]["api"]["enrichment"]["applied"] == 3
    usage = client.get(f"/api/workspaces/{ws['id']}/usage?by=agent").json()
    assert any(row["key"] == "api-enricher" for row in usage)
