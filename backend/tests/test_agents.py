"""Agent loop, guardrails and orchestration."""

from types import SimpleNamespace

import pytest

from filament.agents import REGISTRY
from filament.agents.base import Agent
from filament.agents.tools import SPECIALIST_TOOLS
from filament.config import get_settings


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


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "max_agent_turns", s.max_agent_turns)
    monkeypatch.setattr(s, "run_token_budget", s.run_token_budget)
    return s


def run(client, project_id, agent, **kw):
    body = {"agent": agent, "goal": kw.pop("goal", "test"), **kw}
    return client.post(f"/api/projects/{project_id}/runs", json=body).json()


def test_orchestrator_plans_and_delegates(client, project):
    _, p = project
    r = run(client, p["id"], "orchestrator", goal="Take this idea to a reviewed design",
            input={"idea": "Let shoppers split a payment across two cards"}, autonomy="L2")
    assert r["status"] == "succeeded", r["error"]
    children = r["output"]["child_run_ids"]
    assert len(children) == 2
    kids = [client.get(f"/api/runs/{c}").json() for c in children]
    assert [k["agent"] for k in kids] == ["requirements", "flow"]
    assert all(k["parent_run_id"] == r["id"] and k["status"] == "succeeded" for k in kids)
    assert all(k["autonomy"] == "L2" for k in kids)  # children inherit the parent's autonomy
    delegated = [e for e in r["trace"] if e["type"] == "tool" and e["tool"] == "delegate"]
    assert len(delegated) == 2
    types = {a["type"] for a in client.get(f"/api/projects/{p['id']}/artifacts").json()}
    assert {"idea", "prd", "story", "flow"} <= types


def test_orchestrator_skips_work_that_already_exists(client, project):
    _, p = project
    run(client, p["id"], "requirements", input={"idea": "Saved carts"})
    r = run(client, p["id"], "orchestrator", input={"idea": "Saved carts"})
    kids = [client.get(f"/api/runs/{c}").json()["agent"] for c in r["output"]["child_run_ids"]]
    assert kids == ["flow"]  # PRD already existed, so only the flow was delegated


def test_trace_records_every_turn_and_tool_call(client, project):
    _, p = project
    r = run(client, p["id"], "requirements", input={"idea": "Gift cards"})
    kinds = [e["type"] for e in r["trace"]]
    assert kinds.count("model") == r["turns"] >= 3
    assert {"search_memory", "create_artifact", "finish"} <= {
        e["tool"] for e in r["trace"] if e["type"] == "tool"}
    assert r["trace"][-1] == {"type": "stop", "reason": "finished"}


def test_max_turns_guardrail_stops_a_looping_agent(client, project, register, settings):
    @register
    class Looper(Agent):
        name, description, tools = "looper", "loops forever", SPECIALIST_TOOLS

        def task(self, context):
            return "loop"

        def offline_script(self):
            while True:
                yield [("search_graph", {})]

    settings.max_agent_turns = 3
    _, p = project
    r = run(client, p["id"], "looper")
    assert r["status"] == "stopped"
    assert r["turns"] == 3 and "max_turns" in r["error"]


def test_token_budget_guardrail(client, project, settings):
    settings.run_token_budget = 1
    _, p = project
    r = run(client, p["id"], "requirements", input={"idea": "Wishlists"})
    assert r["status"] == "stopped" and "token_budget" in r["error"]


def test_tool_errors_are_returned_to_the_model_not_raised(client, project, register):
    @register
    class Clumsy(Agent):
        name, description, tools = "clumsy", "makes mistakes", SPECIALIST_TOOLS

        def task(self, context):
            return "go"

        def offline_script(self):
            res = yield [("read_artifact", {"id": "does-not-exist"}),
                         ("create_artifact", {"type": "not-a-type", "title": "x", "content": ""}),
                         ("delegate", {"agent": "flow", "goal": "not allowed here"})]
            assert all("error" in r for r in res)
            yield [("finish", {"summary": "recovered"})]

    _, p = project
    r = run(client, p["id"], "clumsy")
    assert r["status"] == "succeeded" and r["output"]["summary"] == "recovered"
    errors = [e for e in r["trace"] if e["type"] == "tool" and e["error"]]
    assert len(errors) == 3
    assert "not available" in str(errors[2]["result"])  # delegate is orchestrator-only


def test_failed_run_rolls_back_writes_but_keeps_audit_trail(client, project, register):
    @register
    class Crasher(Agent):
        name, description, tools = "crasher", "fails midway", SPECIALIST_TOOLS

        def task(self, context):
            return "go"

        def offline_script(self):
            yield [("create_artifact", {"type": "doc", "title": "half done", "content": "x"})]
            raise RuntimeError("model went away")

    _, p = project
    r = run(client, p["id"], "crasher")
    assert r["status"] == "failed" and "model went away" in r["error"]
    assert r["trace"] and r["input_tokens"] > 0  # audit kept
    assert client.get(f"/api/projects/{p['id']}/artifacts").json() == []  # writes undone


def test_l0_write_tools_save_nothing(client, project):
    _, p = project
    r = run(client, p["id"], "orchestrator", input={"idea": "Coupons"}, autonomy="L0")
    assert r["status"] == "succeeded"
    assert client.get(f"/api/projects/{p['id']}/artifacts").json() == []
    creates = [e for e in r["trace"] if e["type"] == "tool" and e["tool"] == "delegate"]
    assert creates  # it still planned and delegated; specialists just could not save


def test_live_api_path_round_trips_tool_use(client, project, monkeypatch, settings):
    """With an API key set, the loop sends tools and echoes tool_use/tool_result correctly."""
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            usage = SimpleNamespace(input_tokens=100, output_tokens=20)
            if len(calls) == 1:
                block = SimpleNamespace(type="tool_use", id="tu_1", name="search_memory",
                                        input={"query": "checkout"})
                return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
            block = SimpleNamespace(type="tool_use", id="tu_2", name="finish",
                                    input={"summary": "live ok"})
            text = SimpleNamespace(type="text", text="All done.")
            return SimpleNamespace(content=[text, block], stop_reason="tool_use", usage=usage)

    import anthropic

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "pricing", {"claude-sonnet-5-5": {"input": 3, "output": 15}})
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda api_key: SimpleNamespace(messages=FakeMessages()))
    _, p = project
    r = run(client, p["id"], "requirements", input={"idea": "Checkout tipping"})
    assert r["status"] == "succeeded", r["error"]
    assert r["output"]["summary"] == "live ok"
    assert r["input_tokens"] == 200 and r["output_tokens"] == 40
    assert r["cost_usd"] == pytest.approx(200 / 1e6 * 3 + 40 / 1e6 * 15)
    second = calls[1]["messages"]
    expected = {"type": "tool_use", "id": "tu_1", "name": "search_memory",
                "input": {"query": "checkout"}}
    assert second[1] == {"role": "assistant", "content": [expected]}
    assert second[2]["content"][0]["type"] == "tool_result"
    assert second[2]["content"][0]["tool_use_id"] == "tu_1"
    assert {t["name"] for t in calls[0]["tools"]} == set(SPECIALIST_TOOLS)
