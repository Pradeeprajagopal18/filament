"""Orchestrator Agent: plans across specialists to reach a goal.

Given a goal such as "take this idea to a reviewed design", it inspects the project, decides
which specialists to call and in what order, reviews their results, and can send work back
(for example re-running requirements when the flow exposes a gap).
"""

from filament.agents.base import Agent, OfflineScript
from filament.agents.tools import ORCHESTRATOR_TOOLS


class OrchestratorAgent(Agent):
    name = "orchestrator"
    description = "Plans a goal across specialist agents, delegates, reviews and iterates."
    tools = ORCHESTRATOR_TOOLS
    system = (
        "You are the Orchestrator. You reach the user's goal by delegating to specialist "
        "agents and reviewing their output; you do not write artifacts yourself.\n"
        "Specialists: 'requirements' (idea -> PRD + stories; pass the idea text) and 'flow' "
        "(PRD + stories -> user flow and sequence diagrams).\n"
        "1. Inspect the project first (search_graph, search_memory) to see what already exists.\n"
        "2. Plan the smallest sequence of delegations that reaches the goal; skip steps whose "
        "output already exists and is still valid.\n"
        "3. After each delegation, read the key outputs and decide: continue, redo with a "
        "sharper goal, or ask a human.\n"
        "4. Finish with a summary that lists what was produced and what needs human review."
    )

    def prepare(self) -> None:
        self.idea = self.run_record.input.get("idea", "") or ""

    def task(self, context: str) -> str:
        idea = f"\n\n## Idea\n{self.idea}" if self.idea else ""
        return (f"Goal: {self.run_record.goal}\n\nProject: {self.project.name}\n\n"
                f"## Context already gathered\n{context}{idea}")

    def result(self) -> dict:
        return {"child_run_ids": self.children}

    def offline_script(self) -> OfflineScript:
        found = yield [("search_graph", {"types": ["prd", "story", "flow"]})]
        existing = {r["type"] for r in found[0].get("results", [])}
        if self.idea and "prd" not in existing:
            yield [("delegate", {"agent": "requirements", "goal": "Draft PRD and stories",
                                 "idea": self.idea})]
        if "flow" not in existing:
            yield [("delegate", {"agent": "flow", "goal": "Draw the user flow and sequence"})]
        yield [("finish", {"summary": "Planned and delegated: requirements then flow. All "
                                      "outputs are waiting for human review."})]
