# ruff: noqa: E501  (fixture source files are embedded verbatim)
"""MCP gateway: generated servers from repo APIs, registry review, policy, projection,
redaction, metrics, progressive discovery for agents and the Omni MCP endpoint."""

from pathlib import Path

import pytest

from igniteai import mcp_registry as reg
from igniteai.config import get_settings
from tests.test_config import add_demo_server, register, run, scripted  # noqa: F401

API = '''
from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel

class Item(BaseModel):
    sku: str
    quantity: int = 1

class Cart(BaseModel):
    id: str
    owner_email: str
    items: list[Item]
    total: float

class AddItem(BaseModel):
    sku: str
    quantity: int = 1

app = FastAPI()
router = APIRouter(prefix="/carts")

@router.get("/{cart_id}")
def get_cart(cart_id: str, expand: bool = False) -> Cart:
    """Read a cart."""
    raise HTTPException(status_code=404, detail="no cart")

@router.post("/{cart_id}/items", response_model=Cart)
def add_item(cart_id: str, body: AddItem):
    """Add an item."""

@router.delete("/{cart_id}")
def delete_cart(cart_id: str):
    """Delete a cart."""

app.include_router(router, prefix="/v1")
'''


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


@pytest.fixture
def repo(tmp_path):
    return write(tmp_path / "acme" / "checkout-api", {
        "pyproject.toml": '[project]\nname="checkout-api"\ndependencies=["fastapi"]\n',
        "app/main.py": API})


@pytest.fixture
def gen(client, ws, repo):
    """Onboard the repo and return (repo record, generated server, {tool: entry})."""
    r = client.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": str(repo)}).json()
    servers = client.get(f"/api/workspaces/{ws['id']}/mcp/registry").json()
    server = next(s for s in servers if s["kind"] == "generated")
    tools = {t["name"]: t for t in client.get(f"/api/mcp/{server['id']}/tools").json()}
    return r, server, tools


def enable(client, *tools, **extra):
    for t in tools:
        assert client.patch(f"/api/mcp-tools/{t['id']}", json={"enabled": True, **extra}).status_code == 200


def invoke(client, ws, tool, args=None, **kw):
    return client.post(f"/api/workspaces/{ws['id']}/mcp/invoke",
                       json={"server": "api-checkout-api", "tool": tool, "arguments": args or {}, **kw})


# -- control plane -----------------------------------------------------------------------------
def test_repo_api_becomes_generated_server_with_disabled_tools(gen):
    _, server, tools = gen
    assert server["name"] == "api-checkout-api" and server["enabled_tools"] == 0
    assert set(tools) == {"get_cart", "add_item", "delete_cart"}
    assert all(t["status"] == "new" and not t["enabled"] for t in tools.values())
    add = tools["add_item"]
    assert add["write"] is True and tools["get_cart"]["write"] is False
    schema = add["input_schema"]
    assert schema["required"] == ["cart_id", "body"]
    assert set(schema["properties"]["body"]["properties"]) == {"sku", "quantity"}
    assert tools["get_cart"]["input_schema"]["properties"]["expand"]["type"] == "boolean"
    assert "POST /v1/carts/{cart_id}/items" in add["description"]


def test_review_flow_edit_approve_rollback(client, gen):
    _, _, tools = gen
    t = tools["get_cart"]
    p = client.patch(f"/api/mcp-tools/{t['id']}", json={"description": "Fetch a shopper's cart"}).json()
    assert p["status"] == "changed" and p["description"] == t["description"]  # not applied yet
    a = client.post(f"/api/mcp-tools/{t['id']}/approve").json()
    assert a["description"] == "Fetch a shopper's cart" and a["version"] == 2 and a["pending"] is None
    rb = client.post(f"/api/mcp-tools/{t['id']}/rollback", json={"version": 1}).json()
    assert rb["description"] == t["description"] and rb["version"] == 3
    assert client.post(f"/api/mcp-tools/{t['id']}/approve").status_code == 409


def test_rescan_marks_changed_and_removed_tools(client, gen, repo):
    r, server, tools = gen
    enable(client, tools["get_cart"], tools["delete_cart"])
    src = (repo / "app/main.py").read_text()
    src = src.replace("class AddItem(BaseModel):\n    sku: str\n", "class AddItem(BaseModel):\n    sku: str\n    store_id: str\n")
    src = src.replace('@router.delete("/{cart_id}")\ndef delete_cart(cart_id: str):\n    """Delete a cart."""\n', "")
    (repo / "app/main.py").write_text(src)
    client.post(f"/api/repos/{r['id']}/rescan")
    now = {t["name"]: t for t in client.get(f"/api/mcp/{server['id']}/tools").json()}
    assert now["add_item"]["status"] == "changed"
    assert "store_id" in now["add_item"]["pending"]["input_schema"]["properties"]["body"]["properties"]
    assert "store_id" not in now["add_item"]["input_schema"]["properties"]["body"]["properties"]
    assert now["delete_cart"]["status"] == "removed" and now["delete_cart"]["enabled"] is False
    assert now["get_cart"]["enabled"] is True  # untouched tools keep their state


# -- data plane ----------------------------------------------------------------------------------
def test_invoke_policy_projection_redaction_and_metrics(client, ws, gen):
    _, _, tools = gen
    assert invoke(client, ws, "get_cart", {"cart_id": "c1"}).status_code == 403  # disabled
    enable(client, tools["get_cart"], tools["add_item"])
    full = invoke(client, ws, "get_cart", {"cart_id": "c1"}).json()
    assert full["simulated"] is True and full["result"]["items"][0]["sku"] == "SKU-1001"
    assert full["result"]["owner_email"] == "[REDACTED:email]"  # PII scrubbed
    slim = invoke(client, ws, "get_cart", {"cart_id": "c1"}, fields=["id", "items.sku"]).json()
    assert slim["result"] == {"id": "cart_1042", "items": [{"sku": "SKU-1001"}]}
    # Writes follow autonomy: L1 is refused, L3 goes through.
    assert invoke(client, ws, "add_item", {"cart_id": "c1", "body": {"sku": "A"}}).status_code == 403
    assert invoke(client, ws, "add_item", {"cart_id": "c1", "body": {"sku": "A"}}, autonomy="L3").status_code == 200
    assert invoke(client, ws, "get_cart", {}).status_code == 400  # missing path parameter
    m = client.get(f"/api/workspaces/{ws['id']}/mcp/metrics").json()
    row = next(t for t in m["tools"] if t["tool"] == "get_cart")
    assert row["calls"] == 4 and row["denied"] == 1 and row["simulated"] == 2
    assert row["bytes_saved"] > 0 and row["redactions"] >= 1
    calls = client.get(f"/api/workspaces/{ws['id']}/mcp/calls").json()
    assert calls[0]["tool"] in {"get_cart", "add_item"} and calls[-1]["status"] == "denied"


def test_rate_limit_and_agent_policy(client, ws, gen):
    _, _, tools = gen
    enable(client, tools["get_cart"], rate_limit_per_min=1)
    assert invoke(client, ws, "get_cart", {"cart_id": "a"}).status_code == 200
    assert invoke(client, ws, "get_cart", {"cart_id": "a"}).status_code == 429
    client.patch(f"/api/mcp-tools/{tools['get_cart']['id']}", json={"rate_limit_per_min": 0, "allowed_agents": ["flow"]})
    r = invoke(client, ws, "get_cart", {"cart_id": "a"})
    assert r.status_code == 403 and "limited to agents: flow" in r.json()["detail"]


def test_generated_tool_calls_real_service_when_base_url_set(client, ws, gen, monkeypatch):
    _, server, tools = gen
    enable(client, tools["get_cart"])
    client.patch(f"/api/mcp/{server['id']}", json={"url": "https://checkout.internal"})
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"id": "c9", "owner_email": "ada@acme.com", "items": [], "total": 3}

    def fake(method, url, **kw):
        seen.update(method=method, url=url, params=kw["params"])
        return Resp()

    monkeypatch.setattr("httpx.request", fake)
    out = invoke(client, ws, "get_cart", {"cart_id": "c9", "expand": True}).json()
    assert seen == {"method": "GET", "url": "https://checkout.internal/v1/carts/c9", "params": {"expand": True}}
    assert "simulated" not in out and out["result"]["owner_email"] == "[REDACTED:email]"


def test_redaction_and_projection_units():
    text, n = reg.redact_text("mail ada@acme.com card 4242 4242 4242 4242 order 1234 5678 9012 3456 "
                              "key sk_live_abcdefgh12345678 jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4")
    assert "[REDACTED:email]" in text and "[REDACTED:card]" in text and "[REDACTED:api_key]" in text
    assert "[REDACTED:jwt]" in text and "1234 5678 9012 3456" in text  # fails Luhn: not a card
    assert n == 4
    data = {"a": 1, "b": {"c": 2, "d": 3}, "l": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]}
    assert reg.project(data, ["b.c", "l.x"]) == {"b": {"c": 2}, "l": [{"x": 1}, {"x": 3}]}
    assert reg.project(data, None) == data


# -- agents and the Omni MCP endpoint -------------------------------------------------------------
def test_agents_use_progressive_discovery_when_many_tools(client, ws, gen, register, monkeypatch):  # noqa: F811
    r, _, tools = gen
    enable(client, tools["get_cart"])
    monkeypatch.setattr(get_settings(), "mcp_discovery_threshold", 0)
    seen = {}

    def script(agent):
        found = yield [("gateway_discover_servers", {"query": "cart"})]
        server = found[0]["servers"][0]["server"]
        listed = yield [("gateway_list_tools", {"server": server}),
                        ("gateway_get_schema", {"server": server, "tool": "get_cart"})]
        res = yield [("gateway_invoke", {"server": server, "tool": "get_cart",
                                         "arguments": {"cart_id": "c1"}, "fields": ["id"]}),
                     ("gateway_invoke", {"server": server, "tool": "add_item",
                                         "arguments": {"cart_id": "c1"}})]
        seen.update(found=found, listed=listed, res=res, names=list(agent.mcp_tools))
        yield [("finish", {"summary": "ok"})]

    register(scripted("explorer", script))
    out = run(client, r["project_id"], "explorer")
    assert out["status"] == "succeeded", out["error"]
    assert seen["names"] == []  # no per-tool definitions were sent to the model
    assert seen["found"][0]["servers"][0]["server"] == "api-checkout-api"
    assert [t["name"] for t in seen["listed"][0]["tools"]] == ["get_cart"]  # only enabled tools
    assert seen["listed"][1]["input_schema"]["required"] == ["cart_id"]
    assert seen["res"][0]["result"] == {"id": "cart_1042"}
    assert "not enabled" in seen["res"][1]["error"]
    m = client.get(f"/api/workspaces/{ws['id']}/mcp/metrics").json()
    assert m["tools"][0]["actors"] == ["agent:explorer"]


def test_omni_mcp_endpoint(client, ws, gen):
    _, _, tools = gen
    enable(client, tools["get_cart"], tools["add_item"])
    url = f"/api/workspaces/{ws['id']}/mcp/omni"
    init = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}}).json()
    assert init["result"]["serverInfo"]["name"] == "ignite-gateway"
    assert client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    listed = client.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    names = [t["name"] for t in listed["result"]["tools"]]
    assert names[:4] == ["discover_servers", "list_tools", "get_tool_schema", "invoke_tool"]
    assert {"code_query", "code_explain", "code_path", "code_impact"} <= set(names)
    call = lambda name, args, **h: client.post(url, headers=h, json={  # noqa: E731
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": name, "arguments": args}}).json()["result"]
    found = call("discover_servers", {"query": "carts"})
    assert found["structuredContent"]["servers"][0]["server"] == "api-checkout-api"
    got = call("invoke_tool", {"server": "api-checkout-api", "tool": "get_cart",
                               "arguments": {"cart_id": "x"}, "fields": ["total"]})
    assert got["isError"] is False and got["structuredContent"]["result"] == {"total": 49.99}
    write = {"server": "api-checkout-api", "tool": "add_item",
             "arguments": {"cart_id": "x", "body": {"sku": "A"}}}
    assert call("invoke_tool", write)["isError"] is True  # default autonomy L1
    assert call("invoke_tool", write, **{"X-Ignite-Autonomy": "L3"})["isError"] is False


def test_native_server_sync_and_new_tools_need_review(client, project, monkeypatch):
    monkeypatch.setenv("FIL_TEST_TOKEN", "t")
    ws, _ = project
    server = add_demo_server(client, ws["id"])
    entries = {t["name"]: t for t in client.get(f"/api/mcp/{server['id']}/tools").json()}
    assert entries["add"]["enabled"] and entries["create_ticket"]["write"]  # first sync: owner opted in
    # A tool that later appears on the server waits for review.
    from igniteai.db import get_session  # noqa: F401
    monkeypatch.setattr("igniteai.mcp_gateway.discover", lambda s: [
        *s.tools_cache, {"name": "drop_table", "description": "danger", "input_schema": {}}])
    client.post(f"/api/mcp/{server['id']}/sync")
    after = {t["name"]: t for t in client.get(f"/api/mcp/{server['id']}/tools").json()}
    assert after["drop_table"]["status"] == "new" and after["drop_table"]["enabled"] is False
    assert after["drop_table"]["meta"].get("looks_like_write") is True
