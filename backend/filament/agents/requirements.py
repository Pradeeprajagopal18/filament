"""Requirements Agent: idea -> PRD + user stories with acceptance criteria."""

import json

from filament.agents.base import Agent, AgentError, parse_json
from filament.models import Artifact

SCHEMA = """{
  "prd": {
    "title": str,
    "problem": str,
    "goals": [str],
    "non_goals": [str],
    "users": [str],
    "requirements": [str],
    "non_functional": [str],
    "open_questions": [str]
  },
  "stories": [
    {"title": str, "as_a": str, "i_want": str, "so_that": str,
     "acceptance_criteria": ["Given ... When ... Then ..."]}
  ],
  "decisions": [str]
}"""


class RequirementsAgent(Agent):
    name = "requirements"
    description = (
        "Turns an idea or opportunity into a PRD and user stories with acceptance criteria."
    )
    system = (
        "You are the Requirements Agent in Filament, an agentic software delivery platform. "
        "You write clear, testable product requirements. Use the provided context (prior "
        "decisions, standards, linked artifacts) and do not contradict confirmed decisions. "
        "Keep stories small and independently shippable. Acceptance criteria use "
        "Given/When/Then. List genuine unknowns as open questions instead of inventing facts. "
        "Respond with a single JSON object only, matching this shape:\n" + SCHEMA
    )

    def _idea(self) -> tuple[str, Artifact | None]:
        idea_text = self.run_record.input.get("idea", "")
        idea_art = None
        for aid in self.target_artifact_ids():
            art = self.session.get(Artifact, aid)
            if art and art.type in {"idea", "opportunity", "feature_request"}:
                idea_art = art
                idea_text = idea_text or f"{art.title}\n\n{art.content}"
        if not idea_text.strip():
            raise AgentError("Provide input.idea text or an idea artifact in input.artifact_ids")
        return idea_text, idea_art

    def execute(self, context: str) -> dict:
        idea_text, idea_art = self._idea()
        if idea_art is None and self.persists:
            title = idea_text.strip().splitlines()[0][:120]
            idea_art = self.save_artifact("idea", title, idea_text)

        prompt = (
            f"Project: {self.project.name}\n{self.project.description}\n\n"
            f"## Context\n{context}\n\n## Idea\n{idea_text}\n\n"
            "Write the PRD and 3-8 user stories."
        )
        result = self.llm(prompt, offline_response=lambda: json.dumps(offline_draft(idea_text)))
        spec = parse_json(result.text)
        prd = spec.get("prd") or {}
        stories = spec.get("stories") or []
        if not prd or not stories:
            raise AgentError("Model response missing prd or stories")

        prd_art = self.save_artifact("prd", prd.get("title", "PRD"), render_prd(prd), data=prd,
                                     gate=True)
        self.link(prd_art, idea_art, "derives_from")
        story_ids = []
        for s in stories:
            art = self.save_artifact("story", s.get("title", "Story"), render_story(s), data=s,
                                     gate=True)
            self.link(art, prd_art, "implements")
            if art:
                story_ids.append(art.id)
        sources = [prd_art.id] if prd_art else []
        for d in spec.get("decisions", []):
            self.remember(d, kind="decision", sources=sources)

        return {
            "prd_id": prd_art.id if prd_art else None,
            "story_ids": story_ids,
            "idea_id": idea_art.id if idea_art else None,
            "spec": spec,
        }


def render_prd(prd: dict) -> str:
    def section(title: str, items: list[str]) -> str:
        return f"## {title}\n" + "\n".join(f"- {i}" for i in items) + "\n" if items else ""

    return (
        f"# {prd.get('title', 'PRD')}\n\n## Problem\n{prd.get('problem', '')}\n\n"
        + section("Goals", prd.get("goals", []))
        + section("Non-goals", prd.get("non_goals", []))
        + section("Users", prd.get("users", []))
        + section("Requirements", prd.get("requirements", []))
        + section("Non-functional requirements", prd.get("non_functional", []))
        + section("Open questions", prd.get("open_questions", []))
    ).strip()


def render_story(s: dict) -> str:
    ac = "\n".join(f"- {c}" for c in s.get("acceptance_criteria", []))
    return (
        f"As a {s.get('as_a', 'user')}, I want {s.get('i_want', '')} "
        f"so that {s.get('so_that', '')}.\n\n### Acceptance criteria\n{ac}"
    )


def offline_draft(idea: str) -> dict:
    """Deterministic draft used when no API key is configured (demo and tests)."""
    first = idea.strip().splitlines()[0][:80] if idea.strip() else "New capability"
    return {
        "prd": {
            "title": f"PRD: {first}",
            "problem": idea.strip(),
            "goals": [f"Deliver: {first}"],
            "non_goals": ["Anything not described in the idea"],
            "users": ["Primary user (to confirm)"],
            "requirements": [f"The system supports: {first}"],
            "non_functional": ["Meets the team's adopted standards"],
            "open_questions": ["Offline draft - set ANTHROPIC_API_KEY for a full analysis"],
        },
        "stories": [
            {
                "title": f"Basic flow for {first}",
                "as_a": "user",
                "i_want": first.lower(),
                "so_that": "I get the intended outcome",
                "acceptance_criteria": [
                    "Given a signed-in user When they use the feature Then it completes",
                ],
            },
            {
                "title": "Error handling",
                "as_a": "user",
                "i_want": "clear errors when something fails",
                "so_that": "I know what to do next",
                "acceptance_criteria": [
                    "Given invalid input When submitted Then a clear error is shown",
                ],
            },
        ],
        "decisions": [],
    }
