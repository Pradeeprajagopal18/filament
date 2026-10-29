"""Flow & Design Agent: PRD + stories -> user flow and sequence diagrams (Mermaid), agentically."""

import re

from sqlalchemy import select

from filament.agents.base import Agent, AgentError, OfflineScript
from filament.agents.tools import SPECIALIST_TOOLS
from filament.models import Artifact


class FlowAgent(Agent):
    name = "flow"
    description = "Draws user flows and sequence diagrams (Mermaid) from PRDs and stories."
    tools = SPECIALIST_TOOLS
    system = (
        "You are the Flow & Design Agent. You turn requirements into diagrams a product team "
        "can review.\n"
        "1. Read the PRD and stories (search_graph, read_artifact). Check for existing flow "
        "artifacts; update a draft flow rather than creating a duplicate.\n"
        "2. Create a user flow (type 'flow', a ```mermaid flowchart TD block covering the happy "
        "path, decisions and error paths) and a sequence diagram (type 'flow', a ```mermaid "
        "sequenceDiagram block showing user, UI, services and data stores).\n"
        "3. Link each diagram to what it visualizes (kind 'visualizes').\n"
        "4. If the stories leave a gap the flow exposes (a missing error path, an undefined "
        "actor), say so with ask_human and note it under the diagram.\n"
        "Use only valid Mermaid syntax."
    )

    def prepare(self) -> None:
        ids = super().target_artifact_ids()
        if not ids:
            stmt = select(Artifact.id).where(Artifact.project_id == self.project.id,
                                             Artifact.type.in_(["prd", "story"]),
                                             Artifact.status != "rejected")
            ids = list(self.session.scalars(stmt))
        if not ids:
            raise AgentError("No PRD or stories found to draw from")
        self._targets = ids

    def target_artifact_ids(self) -> list[str]:
        if not hasattr(self, "_targets"):
            self.prepare()
        return self._targets

    def task(self, context: str) -> str:
        return (f"Goal: {self.run_record.goal}\n\nDraw the flows for these artifacts: "
                f"{', '.join(self._targets)}\n\n## Context already gathered\n{context}")

    def result(self) -> dict:
        return {"diagram_ids": self.ids_of("flow")}

    def offline_script(self) -> OfflineScript:
        found = yield [("search_graph", {"types": ["prd", "story"], "limit": 25})]
        items = found[0].get("results", [])
        stories = [i for i in items if i["type"] == "story"] or items
        lines = ["flowchart TD", '  Start(["Start"])']
        prev = "Start"
        for n, s in enumerate(stories, 1):
            lines += [f'  S{n}["{_label(s["title"])}"]', f"  {prev} --> S{n}"]
            prev = f"S{n}"
        lines += ['  Done(["Done"])', f"  {prev} --> Done"]
        seq = ["sequenceDiagram", "  actor User", "  participant App", "  participant API"]
        for s in stories:
            seq += [f"  User->>App: {_label(s['title'])}", "  App->>API: request",
                    "  API-->>App: result"]
        links = [{"target_id": i["id"], "kind": "visualizes"} for i in items]
        note = "Generated without a model; set ANTHROPIC_API_KEY for a full flow."
        yield [
            ("create_artifact", {"type": "flow", "title": "User flow (offline draft)",
                                 "content": "```mermaid\n" + "\n".join(lines) + f"\n```\n\n{note}",
                                 "data": {"kind": "user_flow"}, "link_to": links}),
            ("create_artifact", {"type": "flow", "title": "Sequence (offline draft)",
                                 "content": "```mermaid\n" + "\n".join(seq) + "\n```",
                                 "data": {"kind": "sequence"}, "link_to": links}),
        ]
        yield [("finish", {"summary": f"Drew a user flow and a sequence diagram from "
                                      f"{len(stories)} stories."})]


def _label(text: str) -> str:
    return re.sub(r'["\[\]{}()<>|:;]', "", text)[:60]
