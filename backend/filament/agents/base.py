"""Agent base class: an agentic tool-use loop with platform guardrails.

The model chooses which tools to call and when it is done. The platform enforces what it may
do: autonomy levels, approval gates, turn and token budgets, delegation depth, and a full trace.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from typing import ClassVar

from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.llm import LLMResult, ModelTurn, ToolCall, approx_tokens, chat, complete
from filament.models import Approval, Artifact, Link, MemoryEntry, Project, Run

# Autonomy levels (see spec, "Governance and human-in-the-loop").
#   L0 suggest  - output only, nothing saved to the graph
#   L1 draft    - artifacts saved as drafts, a human moves them forward
#   L2 approval - artifacts saved and sent to review with a pending approval
#   L3/L4       - artifacts saved as approved (within policy), humans review exceptions
PERSISTS = {"L1", "L2", "L3", "L4"}


class AgentError(RuntimeError):
    pass


# An offline script is a generator that yields lists of (tool_name, input) and receives the
# list of tool results back. It lets every agent run end to end without an API key.
OfflineScript = Generator[list[tuple[str, dict]], list[dict], None]


class ScriptPolicy:
    """Adapts an offline script generator to the llm.chat offline policy interface."""

    def __init__(self, script: OfflineScript):
        self.script = script
        self.started = False
        self.n = 0

    def __call__(self, messages: list[dict]) -> ModelTurn:
        try:
            if not self.started:
                self.started = True
                calls = next(self.script)
            else:
                last = messages[-1]["content"]
                results = [json.loads(b["content"]) for b in last if b["type"] == "tool_result"]
                calls = self.script.send(results)
        except StopIteration:
            return ModelTurn("Done.", [], "end_turn", "", 0, 0)
        tool_calls = []
        for name, inp in calls:
            self.n += 1
            tool_calls.append(ToolCall(f"offline_{self.n}", name, inp))
        return ModelTurn("", tool_calls, "tool_use", "", 0, 0)


class Agent:
    name: ClassVar[str]
    description: ClassVar[str]
    model_tier: ClassVar[str] = "default"  # "default" | "small"
    system: ClassVar[str] = ""
    tools: ClassVar[list[str]] = []
    uses_mcp: ClassVar[bool] = False  # whether configured MCP tools are offered to this agent

    def __init__(self, session: Session, project: Project, run: Run):
        self.session = session
        self.project = project
        self.run_record = run
        self.created: list[Artifact] = []
        self.touched: list[Artifact] = []
        self.questions: list[str] = []
        self.children: list[str] = []  # child run ids (delegation)
        self.finished_summary: str | None = None
        # Ids the agent has actually seen (context pack, tool results, its own writes).
        # Citations must come from this set: the agent cannot reference what it never read.
        self.seen_ids: set[str] = set()
        self.mcp_tools: dict = {}

    # -- to implement -------------------------------------------------------
    def task(self, context: str) -> str:  # pragma: no cover - interface
        """The opening user message for the loop."""
        raise NotImplementedError

    def offline_script(self) -> OfflineScript:  # pragma: no cover - interface
        raise NotImplementedError

    def result(self) -> dict:
        """Structured output assembled after the loop (override to add fields)."""
        return {}

    def target_artifact_ids(self) -> list[str]:
        """Artifacts this run is about; used to seed the context pack."""
        return list(self.run_record.input.get("artifact_ids", []))

    def prepare(self) -> None:
        """Validate input and set up before the loop (override as needed)."""

    # -- the loop -----------------------------------------------------------
    def execute(self, context: str) -> dict:
        from filament.agents.tools import TOOLS, ToolError

        self.prepare()
        settings = get_settings()
        run = self.run_record
        cfg = run.config or {}
        model = cfg.get("model") if cfg.get("model_explicit") else (
            settings.model_small if self.model_tier == "small" else settings.model_default)
        max_turns = int(cfg.get("max_turns", settings.max_agent_turns))
        token_budget = int(cfg.get("token_budget", settings.run_token_budget))

        tool_specs = [TOOLS[n].spec() for n in self.tools]
        if self.uses_mcp:
            from filament.mcp_gateway import tools_for

            for t in tools_for(self.session, self.project):
                self.mcp_tools[t.agent_name] = t
                note = " Changes external systems." if t.writes else ""
                tool_specs.append({
                    "name": t.agent_name,
                    "description": f"[MCP {t.server.name}] {t.tool['description']}{note}"[:1000],
                    "input_schema": t.tool.get("input_schema") or {"type": "object"},
                })

        self.seen_ids |= {i["id"] for i in (run.context_pack or {}).get("items", [])}
        messages: list[dict] = [{"role": "user", "content": self.task(context)}]
        policy = ScriptPolicy(self.offline_script())
        stop_reason = "max_turns"
        final_text = ""
        system = self._system()

        for _ in range(max_turns):
            if run.input_tokens + run.output_tokens >= token_budget:
                stop_reason = "token_budget"
                break
            self._compact(messages, settings)
            size = approx_tokens(
                system + json.dumps(messages, default=str) + json.dumps(tool_specs))
            run.peak_context_tokens = max(run.peak_context_tokens or 0, size)
            turn = chat(system, messages, tool_specs, model=model, offline_policy=policy)
            self._account(turn)
            run.turns += 1
            messages.append({"role": "assistant", "content": turn.assistant_content()})
            self._trace({"type": "model", "text": turn.text[:2000],
                         "tool_calls": [c.name for c in turn.tool_calls],
                         "tokens": [turn.input_tokens, turn.output_tokens],
                         "context_tokens": size})
            if not turn.tool_calls:
                stop_reason, final_text = "end_turn", turn.text
                break

            results = []
            for call in turn.tool_calls:
                is_error = False
                try:
                    if call.name in self.mcp_tools:
                        out = self._call_mcp(call)
                    elif call.name in TOOLS and call.name in self.tools:
                        with self.session.begin_nested():
                            out = TOOLS[call.name].handler(self, call.input)
                    else:
                        raise ToolError(f"Tool '{call.name}' is not available")
                except ToolError as exc:
                    out, is_error = {"error": str(exc)}, True
                except (KeyError, TypeError, ValueError) as exc:
                    out, is_error = {"error": f"Invalid input: {exc}"}, True
                if not is_error:
                    self._learn_ids(out)
                content = json.dumps(out, default=str)
                limit = settings.tool_result_max_chars
                if len(content) > limit:  # keep it valid JSON so the model can still parse it
                    content = json.dumps({
                        "truncated": True, "omitted_chars": len(content) - limit,
                        "hint": "Narrow the query or read a specific id.",
                        "preview": content[:limit]})
                self._trace({"type": "tool", "tool": call.name, "input": _clip(call.input),
                             "result": _clip(out), "error": is_error})
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            if self.finished_summary is not None:
                stop_reason = "finished"
                break

        self._trace({"type": "stop", "reason": stop_reason})
        return {
            "summary": self.finished_summary or final_text,
            "stop_reason": stop_reason,
            "created": [{"id": a.id, "type": a.type, "title": a.title} for a in self.created],
            "questions": self.questions,
            **self.result(),
        }

    # -- context control ----------------------------------------------------
    def _compact(self, messages: list[dict], settings) -> None:
        """Replace old tool results with short pointers once the conversation gets large.

        The agent keeps the most recent results verbatim and can re-read anything compacted
        by id, so context stays lean without losing the ability to look things up.
        """
        size = approx_tokens(json.dumps(messages, default=str))
        if size <= settings.compaction_threshold_tokens:
            return
        names = {b["id"]: b["name"] for m in messages if m["role"] == "assistant"
                 for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
        result_msgs = [m for m in messages
                       if m["role"] == "user" and isinstance(m["content"], list)]
        keep = max(1, settings.keep_recent_tool_turns)  # the latest results stay verbatim
        compacted = 0
        for m in result_msgs[:-keep]:
            for block in m["content"]:
                text = block.get("content", "")
                if block.get("type") == "tool_result" and not text.startswith("[compacted"):
                    tool = names.get(block["tool_use_id"], "tool")
                    block["content"] = (f"[compacted: earlier {tool} result, {len(text)} chars. "
                                        "Call the tool again if you need it.]")
                    compacted += 1
        if compacted:
            after = approx_tokens(json.dumps(messages, default=str))
            self._trace({"type": "compact", "results": compacted, "before": size, "after": after})

    def _learn_ids(self, value) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in {"id", "run_id", "target_id", "source_id"} and isinstance(v, str):
                    self.seen_ids.add(v)
                else:
                    self._learn_ids(v)
        elif isinstance(value, list):
            for v in value:
                self._learn_ids(v)

    def _call_mcp(self, call: ToolCall) -> dict:
        from filament.agents.tools import ToolError
        from filament.mcp_gateway import McpCallError, autonomy_allows_write
        from filament.mcp_gateway import call as mcp_call

        t = self.mcp_tools[call.name]
        if t.writes:
            if not self.persists:
                return {"saved": False, "reason": "Autonomy L0 (suggest only): external write "
                        "tools are not executed. Describe the change instead."}
            required = t.server.min_autonomy_for_writes or "L2"
            if not autonomy_allows_write(self.autonomy, required):
                raise ToolError(f"{t.tool['name']} changes external systems and needs autonomy "
                                f"{required} or higher; this run is {self.autonomy}.")
        try:
            return mcp_call(t.server, t.tool["name"], call.input or {},
                            get_settings().tool_result_max_chars)
        except McpCallError as exc:
            raise ToolError(f"MCP {t.server.name}: {exc}") from exc

    def _system(self) -> str:
        level = {"L0": "L0 (suggest only: write tools save nothing)",
                 "L1": "L1 (writes are saved as drafts)",
                 "L2": "L2 (writes go to human review)"}.get(self.autonomy, self.autonomy)
        return (
            f"{self.system}\n\nYou work inside Filament for project '{self.project.name}'. "
            f"Autonomy level: {level}.\n\n"
            "Grounding rules (enforced by the platform):\n"
            "- Inspect the project graph and shared memory before writing. Never contradict "
            "confirmed standards or decisions.\n"
            "- State only facts found in the context or in tool results. When you rely on one, "
            "cite it as artifact:<id> or memory:<id>; citations to ids you have not seen are "
            "rejected.\n"
            "- Anything you do not know is an assumption or an open question, labelled as such. "
            "Never invent metrics, users, systems, APIs or people.\n"
            "- Keep tool queries narrow; read specific ids rather than listing everything.\n"
            "Call `finish` with a short summary when the goal is complete."
        )

    def _account(self, turn: ModelTurn | LLMResult) -> None:
        run = self.run_record
        run.model = turn.model
        run.input_tokens += turn.input_tokens
        run.output_tokens += turn.output_tokens
        cost = turn.cost_usd(get_settings())
        if cost is not None:
            run.cost_usd = (run.cost_usd or 0) + cost

    def _trace(self, event: dict) -> None:
        self.run_record.trace = [*(self.run_record.trace or []), event]

    # -- single-shot helper (for simple, non-agentic utilities) ---------------
    def llm(self, prompt: str, offline_response) -> LLMResult:
        settings = get_settings()
        model = settings.model_small if self.model_tier == "small" else settings.model_default
        result = complete(self.system, prompt, model=model, offline_response=offline_response)
        self._account(result)
        return result

    # -- write helpers used by tools ------------------------------------------
    @property
    def autonomy(self) -> str:
        return self.run_record.autonomy

    @property
    def persists(self) -> bool:
        return self.autonomy in PERSISTS

    def initial_status(self) -> str:
        return {"L1": "draft", "L2": "in_review"}.get(self.autonomy, "approved")

    def save_artifact(self, type_: str, title: str, content: str, data: dict | None = None,
                      gate: bool = False) -> Artifact | None:
        if not self.persists:
            return None
        art = Artifact(
            project_id=self.project.id, type=type_, title=title, content=content,
            data=data or {}, status=self.initial_status(), author=f"agent:{self.name}",
            run_id=self.run_record.id,
        )
        self.session.add(art)
        self.session.flush()
        self.seen_ids.add(art.id)
        if gate and self.autonomy == "L2":
            self.session.add(Approval(artifact_id=art.id))
        self.created.append(art)
        return art

    def link(self, source: Artifact | None, target: Artifact | None, kind: str) -> None:
        if source is None or target is None:
            return
        self.session.add(Link(project_id=self.project.id, source_id=source.id,
                              target_id=target.id, kind=kind))

    def remember(self, content: str, kind: str = "fact", sources: list[str] | None = None) -> None:
        """Agent-written facts land as *proposed* project memory until a human confirms them."""
        if not self.persists:
            return
        self.session.add(MemoryEntry(
            workspace_id=self.project.workspace_id, scope="project", scope_ref=self.project.id,
            kind=kind, content=content, status="proposed", author=f"agent:{self.name}",
            run_id=self.run_record.id, source_artifact_ids=sources or [], confidence=0.7,
        ))

    def ids_of(self, type_: str) -> list[str]:
        return [a.id for a in self.created if a.type == type_]


def _clip(value, limit: int = 600):
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "…"


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(text: str) -> dict:
    """Extract the first JSON object from a model response (tolerates code fences / prose)."""
    match = _JSON_BLOCK.search(text)
    if not match:
        raise AgentError("Model response did not contain a JSON object")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise AgentError(f"Model returned invalid JSON: {exc}") from exc
