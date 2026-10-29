"""Flow & Design Agent: stories -> user flow and sequence diagrams (Mermaid)."""

import json
import re

from sqlalchemy import select

from filament.agents.base import Agent, AgentError, parse_json
from filament.models import Artifact


class FlowAgent(Agent):
    name = "flow"
    description = "Draws user flows and sequence diagrams (Mermaid) from PRDs and stories."
    system = (
        "You are the Flow & Design Agent in Filament. From the PRD and stories in the context, "
        "produce diagrams that a product team can review. Use valid Mermaid syntax only. "
        "Respond with a single JSON object: "
        '{"user_flow": {"title": str, "mermaid": str, "notes": str}, '
        '"sequence": {"title": str, "mermaid": str, "notes": str}}. '
        "user_flow must be a `flowchart TD`; sequence must be a `sequenceDiagram`."
    )

    def target_artifact_ids(self) -> list[str]:
        ids = super().target_artifact_ids()
        if ids:
            return ids
        stmt = select(Artifact.id).where(
            Artifact.project_id == self.project.id,
            Artifact.type.in_(["prd", "story"]),
            Artifact.status != "rejected",
        )
        return list(self.session.scalars(stmt))

    def execute(self, context: str) -> dict:
        targets = [self.session.get(Artifact, i) for i in self.target_artifact_ids()]
        targets = [t for t in targets if t is not None]
        if not targets:
            raise AgentError("No PRD or stories found to draw from")
        prompt = f"Project: {self.project.name}\n\n## Context\n{context}\n\nDraw the diagrams."
        result = self.llm(prompt, offline_response=lambda: json.dumps(offline_diagrams(targets)))
        spec = parse_json(result.text)

        out: dict = {"diagram_ids": []}
        for key, label in (("user_flow", "User flow"), ("sequence", "Sequence")):
            d = spec.get(key)
            if not d or "mermaid" not in d:
                continue
            content = f"```mermaid\n{d['mermaid'].strip()}\n```\n\n{d.get('notes', '')}".strip()
            art = self.save_artifact("flow", d.get("title", label), content,
                                     data={"kind": key, "mermaid": d["mermaid"]}, gate=True)
            for t in targets:
                self.link(art, t, "visualizes")
            if art:
                out["diagram_ids"].append(art.id)
        out["spec"] = spec
        return out


def _node(i: int) -> str:
    return f"S{i}"


def _label(text: str) -> str:
    return re.sub(r'["\[\]{}()<>|]', "", text)[:60]


def offline_diagrams(targets: list[Artifact]) -> dict:
    stories = [t for t in targets if t.type == "story"] or targets
    lines = ["flowchart TD", '  Start(["Start"])']
    prev = "Start"
    for i, s in enumerate(stories, 1):
        lines.append(f'  {_node(i)}["{_label(s.title)}"]')
        lines.append(f"  {prev} --> {_node(i)}")
        prev = _node(i)
    lines.append('  Done(["Done"])')
    lines.append(f"  {prev} --> Done")
    seq = ["sequenceDiagram", "  actor User", "  participant App", "  participant API"]
    for s in stories:
        seq += [f"  User->>App: {_label(s.title)}", "  App->>API: request", "  API-->>App: result"]
    return {
        "user_flow": {"title": "User flow (offline draft)", "mermaid": "\n".join(lines),
                      "notes": "Generated without a model; set ANTHROPIC_API_KEY for a full flow."},
        "sequence": {"title": "Sequence (offline draft)", "mermaid": "\n".join(seq), "notes": ""},
    }
