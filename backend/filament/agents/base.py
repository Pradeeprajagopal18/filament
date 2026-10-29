"""Agent base class and shared helpers."""

import json
import re
from typing import ClassVar

from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.llm import LLMResult, complete
from filament.models import Approval, Artifact, Link, MemoryEntry, Project, Run

# Autonomy levels (see spec, "Governance and human-in-the-loop").
#   L0 suggest  - output only, nothing saved to the graph
#   L1 draft    - artifacts saved as drafts, a human moves them forward
#   L2 approval - artifacts saved and sent to review with a pending approval
#   L3/L4       - artifacts saved as approved (within policy), humans review exceptions
PERSISTS = {"L1", "L2", "L3", "L4"}


class AgentError(RuntimeError):
    pass


class Agent:
    name: ClassVar[str]
    description: ClassVar[str]
    model_tier: ClassVar[str] = "default"  # "default" | "small"
    system: ClassVar[str] = ""

    def __init__(self, session: Session, project: Project, run: Run):
        self.session = session
        self.project = project
        self.run_record = run

    # -- to implement -------------------------------------------------------
    def execute(self, context: str) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def target_artifact_ids(self) -> list[str]:
        """Artifacts this run is about; used to seed the context pack."""
        return list(self.run_record.input.get("artifact_ids", []))

    # -- helpers ------------------------------------------------------------
    def llm(self, prompt: str, offline_response) -> LLMResult:
        settings = get_settings()
        model = settings.model_small if self.model_tier == "small" else settings.model_default
        result = complete(self.system, prompt, model=model, offline_response=offline_response)
        run = self.run_record
        run.model = result.model
        run.input_tokens += result.input_tokens
        run.output_tokens += result.output_tokens
        cost = result.cost_usd(settings)
        if cost is not None:
            run.cost_usd = (run.cost_usd or 0) + cost
        return result

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
