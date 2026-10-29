def test_health(client):
    r = client.get("/api/health").json()
    assert r["status"] == "ok"
    assert r["offline"] is True


def test_requirements_then_flow_builds_linked_graph(client, project):
    ws, p = project
    r = client.post(f"/api/projects/{p['id']}/runs", json={
        "agent": "requirements", "goal": "Draft requirements",
        "input": {"idea": "Let shoppers save a card for one-click checkout"}, "autonomy": "L2",
    }).json()
    assert r["status"] == "succeeded", r["error"]
    assert r["input_tokens"] > 0 and r["output_tokens"] > 0
    out = r["output"]
    assert out["prd_id"] and len(out["story_ids"]) >= 2 and out["idea_id"]

    prd = client.get(f"/api/artifacts/{out['prd_id']}").json()
    assert prd["status"] == "in_review"  # L2 sends work to review
    approvals = client.get(f"/api/projects/{p['id']}/approvals").json()
    assert len(approvals) == 1 + len(out["story_ids"])

    # Approve the PRD at the human gate.
    approved = client.post(f"/api/artifacts/{out['prd_id']}/approve",
                           json={"decision": "approved"}).json()
    assert approved["status"] == "approved"

    flow = client.post(f"/api/projects/{p['id']}/runs",
                       json={"agent": "flow", "goal": "Draw the flow"}).json()
    assert flow["status"] == "succeeded", flow["error"]
    assert len(flow["output"]["diagram_ids"]) == 2
    diagram = client.get(f"/api/artifacts/{flow['output']['diagram_ids'][0]}").json()
    assert "flowchart TD" in diagram["content"]
    # The flow run's context pack included the PRD and stories it drew from.
    assert any(i["id"] == out["prd_id"] for i in flow["context_pack"]["items"])

    graph = client.get(f"/api/projects/{p['id']}/graph").json()
    kinds = {e["kind"] for e in graph["edges"]}
    assert {"implements", "derives_from", "visualizes"} <= kinds

    # Changing the PRD affects stories and diagrams.
    impact = client.get(f"/api/artifacts/{out['prd_id']}/impact").json()
    affected_types = {a["type"] for a in impact["affected"]}
    assert {"story", "flow"} <= affected_types


def test_l0_suggest_persists_nothing(client, project):
    _, p = project
    r = client.post(f"/api/projects/{p['id']}/runs", json={
        "agent": "requirements", "goal": "Suggest", "input": {"idea": "Dark mode"},
        "autonomy": "L0"}).json()
    assert r["status"] == "succeeded"
    assert r["output"]["prd_id"] is None
    assert client.get(f"/api/projects/{p['id']}/artifacts").json() == []


def test_failed_run_records_error(client, project):
    _, p = project
    r = client.post(f"/api/projects/{p['id']}/runs",
                    json={"agent": "requirements", "goal": "No idea given"}).json()
    assert r["status"] == "failed"
    assert "idea" in r["error"]


def test_memory_feeds_context_and_ask(client, project):
    ws, p = project
    m = client.post(f"/api/workspaces/{ws['id']}/memory", json={
        "scope": "org", "kind": "standard",
        "content": "All checkout payments use idempotency keys"}).json()
    assert m["status"] == "confirmed"

    r = client.post(f"/api/projects/{p['id']}/runs", json={
        "agent": "requirements", "goal": "checkout payments retries",
        "input": {"idea": "Retry failed checkout payments"}}).json()
    assert any(i["kind"] == "memory" for i in r["context_pack"]["items"])

    ans = client.post(f"/api/workspaces/{ws['id']}/ask",
                      json={"question": "Do checkout payments use idempotency keys?"}).json()
    assert ans["answered"] is True
    assert f"memory:{m['id']}" in ans["citations"]


def test_unanswered_questions_become_gaps(client, project):
    ws, _ = project
    for q in ["How do we rotate kafka credentials?", "Where is the kafka runbook?"]:
        a = client.post(f"/api/workspaces/{ws['id']}/ask", json={"question": q}).json()
        assert a["answered"] is False
    gaps = client.get(f"/api/workspaces/{ws['id']}/gaps").json()
    assert gaps[0]["topic"] == "kafka" and gaps[0]["count"] == 2


def test_usage_groups_tokens(client, project):
    ws, p = project
    for agent, inp in (("requirements", {"idea": "Search"}), ("flow", {})):
        client.post(f"/api/projects/{p['id']}/runs",
                    json={"agent": agent, "goal": "x", "input": inp})
    rows = client.get(f"/api/workspaces/{ws['id']}/usage", params={"by": "agent"}).json()
    assert {r["key"] for r in rows} == {"requirements", "flow"}
    assert all(r["input_tokens"] > 0 for r in rows)
    assert all(r["cost_usd"] is None for r in rows)  # no pricing configured


def test_unknown_agent_rejected(client, project):
    _, p = project
    r = client.post(f"/api/projects/{p['id']}/runs", json={"agent": "nope", "goal": "x"})
    assert r.status_code == 400
