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
from filament.llm import LLMResult, ModelTurn, ToolCall, chat, complete
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

    def __init__(self, session: Session, project: Project, run: Run):
        self.session = session
        self.project = project
        self.run_record = run
        self.created: list[Artifact] = []
        self.touched: list[Artifact] = []
        self.questions: list[str] = []
        self.children: list[str] = []  # child run ids (delegation)
        self.finished_summary: str | None = None

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
        model = settings.model_small if self.model_tier == "small" else settings.model_default
        tool_specs = [TOOLS[n].spec() for n in self.tools]
        messages: list[dict] = [{"role": "user", "content": self.task(context)}]
        policy = ScriptPolicy(self.offline_script())
        stop_reason = "max_turns"
        final_text = ""

        for _ in range(settings.max_agent_turns):
            if run.input_tokens + run.output_tokens >= settings.run_token_budget:
                stop_reason = "token_budget"
                break
            turn = chat(self._system(), messages, tool_specs, model=model, offline_policy=policy)
            self._account(turn)
            run.turns += 1
            messages.append({"role": "assistant", "content": turn.assistant_content()})
            self._trace({"type": "model", "text": turn.text[:2000],
                         "tool_calls": [c.name for c in turn.tool_calls],
                         "tokens": [turn.input_tokens, turn.output_tokens]})
            if not turn.tool_calls:
                stop_reason, final_text = "end_turn", turn.text
                break

            results = []
            for call in turn.tool_calls:
                tool = TOOLS.get(call.name)
                is_error = False
                if tool is None or call.name not in self.tools:
                    out, is_error = {"error": f"Tool '{call.name}' is not available"}, True
                else:
                    try:
                        with self.session.begin_nested():
                            out = tool.handler(self, call.input)
                    except ToolError as exc:
                        out, is_error = {"error": str(exc)}, True
                    except (KeyError, TypeError, ValueError) as exc:
                        out, is_error = {"error": f"Invalid input: {exc}"}, True
                self._trace({"type": "tool", "tool": call.name, "input": _clip(call.input),
                             "result": _clip(out), "error": is_error})
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(out, default=str)[:12_000],
                                "is_error": is_error})
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

    def _system(self) -> str:
        level = {"L0": "L0 (suggest only: write tools save nothing)",
                 "L1": "L1 (writes are saved as drafts)",
                 "L2": "L2 (writes go to human review)"}.get(self.autonomy, self.autonomy)
        return (
            f"{self.system}\n\nYou work inside Filament for project '{self.project.name}'. "
            f"Autonomy level: {level}. Use tools to inspect the project graph and shared "
            "memory before writing, never contradict confirmed standards or decisions, and "
            "call `finish` with a short summary when the goal is complete."
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
