"""Requirements Agent: idea -> PRD + user stories with acceptance criteria, agentically.

The agent inspects the graph and memory, decides what to write, checks for duplicates, links
its work, asks humans about real ambiguity, and proposes decisions for shared memory.
"""

from filament.agents.base import Agent, AgentError, OfflineScript
from filament.agents.tools import SPECIALIST_TOOLS
from filament.models import Artifact


class RequirementsAgent(Agent):
    name = "requirements"
    description = (
        "Turns an idea or opportunity into a PRD and user stories with acceptance criteria."
    )
    tools = SPECIALIST_TOOLS
    uses_mcp = True
    system = (
        "You are the Requirements Agent. You turn ideas into clear, testable requirements.\n"
        "Work like a strong product manager:\n"
        "1. Search shared memory for standards and decisions that constrain this idea, and "
        "search the project graph for existing PRDs/stories so you extend rather than duplicate.\n"
        "2. Create one PRD (type 'prd', link derives_from the idea) with sections: Problem, "
        "Goals, Non-goals, Users, Requirements, Non-functional requirements, Open questions.\n"
        "3. Create 3-8 small, independently shippable stories (type 'story', link implements "
        "the PRD). Each story: 'As a ..., I want ..., so that ...' then Given/When/Then "
        "acceptance criteria.\n"
        "4. If something important is ambiguous, call ask_human, then proceed with a stated "
        "assumption listed under Open questions.\n"
        "5. Propose durable product decisions with propose_memory.\n"
        "Never invent facts about users, metrics or systems; list unknowns instead."
    )

    def prepare(self) -> None:
        self.idea_text = self.run_record.input.get("idea", "") or ""
        self.idea_art: Artifact | None = None
        for aid in self.target_artifact_ids():
            art = self.session.get(Artifact, aid)
            if art and art.type in {"idea", "opportunity", "feature_request"}:
                self.idea_art = art
                self.idea_text = self.idea_text or f"{art.title}\n\n{art.content}"
        if not self.idea_text.strip():
            raise AgentError("Provide input.idea text or an idea artifact in input.artifact_ids")
        if self.idea_art is None and self.persists:
            # The idea is the requester's own input, saved as approved human work.
            title = self.idea_text.strip().splitlines()[0][:120]
            self.idea_art = Artifact(project_id=self.project.id, type="idea", title=title,
                                     content=self.idea_text, status="approved",
                                     author=self.run_record.requested_by,
                                     run_id=self.run_record.id)
            self.session.add(self.idea_art)
            self.session.flush()

    def task(self, context: str) -> str:
        idea_ref = f" (artifact id {self.idea_art.id})" if self.idea_art else ""
        return (f"Goal: {self.run_record.goal}\n\nProject: {self.project.name}\n"
                f"{self.project.description}\n\n## Context already gathered\n{context}\n\n"
                f"## Idea{idea_ref}\n{self.idea_text}")

    def result(self) -> dict:
        prds = self.ids_of("prd")
        return {"idea_id": self.idea_art.id if self.idea_art else None,
                "prd_id": prds[0] if prds else None, "story_ids": self.ids_of("story")}

    def offline_script(self) -> OfflineScript:
        first = self.idea_text.strip().splitlines()[0][:80]
        mem = yield [("search_memory", {"query": self.idea_text[:300]}),
                     ("search_graph", {"query": first, "types": ["prd", "story"]})]
        standards = [m["content"] for m in mem[0].get("results", [])]
        nfr = "\n".join(f"- {s}" for s in standards) or "- Meets the team's adopted standards"
        idea_link = ([{"target_id": self.idea_art.id, "kind": "derives_from"}]
                     if self.idea_art else [])
        prd = (
            f"# PRD: {first}\n\n## Problem\n{self.idea_text.strip()}\n\n## Goals\n- Deliver: "
            f"{first}\n\n## Non-goals\n- Anything not described in the idea\n\n## Users\n"
            f"- Primary user (to confirm)\n\n## Requirements\n- The system supports: {first}\n\n"
            f"## Non-functional requirements\n{nfr}\n\n## Open questions\n"
            "- Offline draft: set ANTHROPIC_API_KEY for a full analysis"
        )
        res = yield [("create_artifact", {"type": "prd", "title": f"PRD: {first}",
                                          "content": prd, "link_to": idea_link})]
        prd_id = res[0].get("id")
        link = [{"target_id": prd_id, "kind": "implements"}] if prd_id else []
        stories = [
            (f"Basic flow for {first}", "user", first.lower(), "I get the intended outcome",
             "Given a signed-in user When they use the feature Then it completes"),
            ("Error handling", "user", "clear errors when something fails",
             "I know what to do next",
             "Given invalid input When submitted Then a clear error is shown"),
        ]
        yield [("create_artifact", {
            "type": "story", "title": t, "link_to": link,
            "content": f"As a {who}, I want {want}, so that {why}.\n\n"
                       f"### Acceptance criteria\n- {ac}"}) for t, who, want, why, ac in stories]
        yield [("finish", {"summary": f"Drafted a PRD and {len(stories)} stories for '{first}'"
                                      f" using {len(standards)} memory entries."})]
