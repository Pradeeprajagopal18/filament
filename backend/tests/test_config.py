"""MCP configuration, runners, agent defaults, config-as-code, context control and grounding."""

import sys
from pathlib import Path

import pytest

from filament.agents import REGISTRY
from filament.agents.base import Agent
from filament.agents.tools import SPECIALIST_TOOLS
from filament.config import get_settings

SERVER = str(Path(__file__).parent / "fixtures" / "mcp_demo_server.py")


@pytest.fixture
def register():
    added = []

    def _add(cls):
        REGISTRY[cls.name] = cls
        added.append(cls.name)
        return cls

    yield _add
    for name in added:
        REGISTRY.pop(name, None)


def scripted(name, script_fn, uses_mcp=True):
    """Build a test agent whose offline 'model' follows script_fn(agent)."""

    class Scripted(Agent):
        description = "test agent"
        tools = SPECIALIST_TOOLS

        def task(self, context):
            return "go"

        def offline_script(self):
            return script_fn(self)

    Scripted.name = name
    Scripted.uses_mcp = uses_mcp
    return Scripted


def add_demo_server(client, ws, **extra):
    body = {"name": "demo", "transport": "stdio", "command": sys.executable, "args": [SERVER],
            "env": {"DEMO_TOKEN": "${env:FIL_TEST_TOKEN}"}, "write_tools": ["create_ticket"],
            **extra}
    r = client.post(f"/api/workspaces/{ws}/mcp", params={"test": True}, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def run(client, pid, agent, **kw):
    return client.post(f"/api/projects/{pid}/runs",
                       json={"agent": agent, "goal": "test", **kw}).json()


# -- MCP configuration ----------------------------------------------------------------------------
def test_secrets_must_be_references(client, project):
    ws, _ = project
    body = {"name": "gh", "transport": "http", "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ghp_live_secret"}}
    r = client.post(f"/api/workspaces/{ws['id']}/mcp", json=body)
    assert r.status_code == 422 and "reference" in r.json()["detail"]
    body["headers"] = {"Authorization": "${env:GITHUB_TOKEN}"}
    assert client.post(f"/api/workspaces/{ws['id']}/mcp", json=body).status_code == 201
    assert client.post(f"/api/workspaces/{ws['id']}/mcp", json=body).status_code == 409


def test_add_test_and_edit_mcp_server(client, project, monkeypatch):
    monkeypatch.setenv("FIL_TEST_TOKEN", "t0k3n")
    ws, p = project
    server = add_demo_server(client, ws["id"])
    assert server["last_test_ok"] is True, server["last_test_error"]
    assert {t["name"] for t in server["tools_cache"]} == {"add", "whoami", "create_ticket",
                                                          "big_report"}
    tools = client.get(f"/api/projects/{p['id']}/mcp-tools").json()
    assert {t["name"] for t in tools} >= {"mcp__demo__add", "mcp__demo__create_ticket"}
    assert next(t for t in tools if t["tool"] == "create_ticket")["writes"] is True

    # Restrict to an allow-list, then disable entirely.
    client.patch(f"/api/mcp/{server['id']}", json={"allowed_tools": ["add"]})
    assert [t["tool"] for t in client.get(f"/api/projects/{p['id']}/mcp-tools").json()] == ["add"]
    client.patch(f"/api/mcp/{server['id']}", json={"enabled": False})
    assert client.get(f"/api/projects/{p['id']}/mcp-tools").json() == []


def test_failed_connection_is_reported_not_raised(client, project):
    ws, _ = project
    r = client.post(f"/api/workspaces/{ws['id']}/mcp", params={"test": True}, json={
        "name": "broken", "transport": "stdio", "command": "/nonexistent/binary"}).json()
    assert r["last_test_ok"] is False and r["last_test_error"]


def test_project_scope_overrides_workspace_scope(client, project, monkeypatch):
    monkeypatch.setenv("FIL_TEST_TOKEN", "x")
    ws, p = project
    add_demo_server(client, ws["id"], allowed_tools=["add"])
    add_demo_server(client, ws["id"], scope="project", scope_ref=p["id"],
                    allowed_tools=["whoami"])
    tools = client.get(f"/api/projects/{p['id']}/mcp-tools").json()
    assert [t["tool"] for t in tools] == ["whoami"]  # narrowest scope wins


def test_agent_calls_mcp_tools_with_autonomy_rules(client, project, register, monkeypatch):
    monkeypatch.setenv("FIL_TEST_TOKEN", "s3cret")
    ws, p = project
    add_demo_server(client, ws["id"])
    seen = {}

    def script(agent):
        res = yield [("mcp__demo__add", {"a": 2, "b": 3}), ("mcp__demo__whoami", {}),
                     ("mcp__demo__create_ticket", {"title": "Fix login"}),
                     ("mcp__demo__big_report", {})]
        seen[agent.autonomy] = res
        yield [("finish", {"summary": "ok"})]

    register(scripted("mcp_user", script))
    r = run(client, p["id"], "mcp_user", autonomy="L1")
    assert r["status"] == "succeeded", r["error"]
    add, who, ticket, big = seen["L1"]
    assert add["content"] == "5" and who["content"] == "s3cret"  # secret ref resolved
    assert "needs autonomy L2" in ticket["error"]  # write blocked below the server's minimum
    assert big["truncated"]  # large results are capped before reaching the model

    run(client, p["id"], "mcp_user", autonomy="L2")
    assert seen["L2"][2]["content"] == "created: Fix login"
    run(client, p["id"], "mcp_user", autonomy="L0")
    assert seen["L0"][2]["saved"] is False  # L0 never executes external writes


def test_missing_secret_is_a_tool_error(client, project, register, monkeypatch):
    monkeypatch.setenv("FIL_TEST_TOKEN", "x")
    ws, p = project
    add_demo_server(client, ws["id"])
    monkeypatch.delenv("FIL_TEST_TOKEN")
    seen = []

    def script(agent):
        seen.extend((yield [("mcp__demo__add", {"a": 1, "b": 1})]))
        yield [("finish", {"summary": "done"})]

    register(scripted("nosecret", script))
    assert run(client, p["id"], "nosecret")["status"] == "succeeded"
    assert "FIL_TEST_TOKEN is not set" in seen[0]["error"]


# -- runners and agent defaults ------------------------------------------------------------------
def test_agent_defaults_and_overrides_resolve_in_order(client, project):
    ws, p = project
    rows = {a["agent"]: a for a in client.get(f"/api/workspaces/{ws['id']}/agent-settings").json()}
    assert rows["requirements"]["runner"] == "inline"
    assert rows["requirements"]["max_turns"] == get_settings().max_agent_turns

    client.put(f"/api/workspaces/{ws['id']}/agent-settings/requirements",
               json={"model": "claude-opus-5-5", "max_turns": 2})
    r = run(client, p["id"], "requirements", input={"idea": "Gift cards"})
    assert r["config"]["model"] == "claude-opus-5-5" and r["config"]["max_turns"] == 2
    assert r["status"] == "stopped" and "max_turns" in r["error"]  # 2 turns isn't enough

    r = run(client, p["id"], "requirements", input={"idea": "Gift cards"}, max_turns=10)
    assert r["config"]["max_turns"] == 10 and r["status"] == "succeeded"  # per-run override


def test_queued_run_executes_on_a_worker(client, project):
    from filament.db import get_session
    from filament.models import Runner
    from filament.runtime import work_once

    ws, p = project
    runner = client.post(f"/api/workspaces/{ws['id']}/runners",
                         json={"name": "build-box", "kind": "worker", "is_default": True}).json()
    r = run(client, p["id"], "requirements", input={"idea": "Wishlists"})
    assert r["status"] == "queued" and r["config"]["runner"] == "build-box"
    assert client.get(f"/api/workspaces/{ws['id']}/runners").json()[0]["queued"] == 1

    # Override back to inline for one run.
    assert run(client, p["id"], "flow", runner="inline")["config"]["runner"] == "inline"

    session = next(client.app.dependency_overrides[get_session]())
    done = work_once(session, session.get(Runner, runner["id"]), worker="box-1")
    assert done.id == r["id"] and done.status == "succeeded" and done.claimed_by == "box-1"
    assert work_once(session, session.get(Runner, runner["id"]), worker="box-1") is None
    session.close()  # the test shares one in-memory connection with the API
    assert client.get(f"/api/runs/{r['id']}").json()["status"] == "succeeded"


def test_unknown_runner_is_rejected(client, project):
    _, p = project
    r = client.post(f"/api/projects/{p['id']}/runs",
                    json={"agent": "flow", "goal": "x", "runner": "nope"})
    assert r.status_code == 422


# -- config as code -------------------------------------------------------------------------------
YAML = """
runners:
  - name: k8s-prod
    kind: kubernetes
    config: {namespace: agents}
    default: true
mcp_servers:
  - name: github
    transport: http
    url: https://api.example.com/mcp
    headers: {Authorization: "${env:GITHUB_TOKEN}"}
    write_tools: [create_pull_request]
    min_autonomy_for_writes: L3
agents:
  requirements: {model: claude-opus-5-5, max_turns: 8}
  flow: {runner: k8s-prod}
"""


def test_yaml_apply_and_export_round_trip(client, project):
    ws, _ = project
    r = client.post(f"/api/workspaces/{ws['id']}/config", content=YAML,
                    headers={"Content-Type": "text/plain"})
    assert r.json()["applied"] == {"runners": 1, "mcp_servers": 1, "agents": 2}
    exported = client.get(f"/api/workspaces/{ws['id']}/config").text
    assert "k8s-prod" in exported and "${env:GITHUB_TOKEN}" in exported
    rows = {a["agent"]: a for a in client.get(f"/api/workspaces/{ws['id']}/agent-settings").json()}
    assert rows["flow"]["runner"] == "k8s-prod" and rows["requirements"]["max_turns"] == 8
    # Applying the export again is a no-op in effect (idempotent upsert by name).
    client.post(f"/api/workspaces/{ws['id']}/config", content=exported,
                headers={"Content-Type": "text/plain"})
    assert len(client.get(f"/api/workspaces/{ws['id']}/mcp").json()) == 1


def test_invalid_yaml_changes_nothing(client, project):
    ws, _ = project
    bad = YAML.replace('"${env:GITHUB_TOKEN}"', "Bearer plain-text-secret")
    r = client.post(f"/api/workspaces/{ws['id']}/config", content=bad,
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 422
    assert client.get(f"/api/workspaces/{ws['id']}/runners").json() == []  # rolled back


# -- context control and grounding ----------------------------------------------------------------
def test_citations_must_reference_seen_ids(client, project, register):
    _, p = project
    errors = []

    def script(agent):
        res = yield [("create_artifact", {"type": "doc", "title": "x",
                                          "content": "Per artifact:abcdef123456 we must..."}),
                     ("create_artifact", {"type": "doc", "title": "y", "content": "ok",
                                          "sources": ["deadbeef0000"]}),
                     ("propose_memory", {"content": "unsourced fact"})]
        errors.extend(res)
        created = yield [("create_artifact", {"type": "doc", "title": "base", "content": "a"})]
        cid = created[0]["id"]
        res = yield [("create_artifact", {"type": "doc", "title": "cites",
                                          "content": f"Builds on artifact:{cid}",
                                          "sources": [cid]}),
                     ("propose_memory", {"content": "grounded", "source_artifact_ids": [cid]})]
        errors.extend(res)
        yield [("finish", {"summary": "done"})]

    register(scripted("citer", script, uses_mcp=False))
    r = run(client, p["id"], "citer")
    assert r["status"] == "succeeded"
    assert "unread references" in errors[0]["error"] and "deadbeef0000" in errors[1]["error"]
    assert "grounded" in errors[2]["error"]
    assert errors[3]["saved"] and errors[4]["saved"]  # citing what it actually saw is fine
    cites = client.get(f"/api/artifacts/{errors[3]['id']}").json()
    assert cites["data"]["sources"]


def test_conversation_is_compacted_when_large(client, project, register, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "compaction_threshold_tokens", 300)
    _, p = project
    for i in range(6):
        client.post(f"/api/projects/{p['id']}/artifacts",
                    json={"type": "doc", "title": f"Doc {i}", "content": "lorem ipsum " * 200})

    def script(agent):
        for _ in range(4):
            yield [("search_graph", {"limit": 6})]
        yield [("finish", {"summary": "done"})]

    register(scripted("reader", script, uses_mcp=False))
    r = run(client, p["id"], "reader")
    assert r["status"] == "succeeded"
    compactions = [e for e in r["trace"] if e["type"] == "compact"]
    assert compactions and compactions[-1]["after"] < compactions[-1]["before"]
    assert r["peak_context_tokens"] > 0
