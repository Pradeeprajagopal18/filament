"""Agent registry and runner."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from filament.agents.base import Agent, AgentError
from filament.agents.flow import FlowAgent
from filament.agents.requirements import RequirementsAgent
from filament.context import build_context_pack, render_context
from filament.models import Project, Run, Signal

REGISTRY: dict[str, type[Agent]] = {
    RequirementsAgent.name: RequirementsAgent,
    FlowAgent.name: FlowAgent,
}


def list_agents() -> list[dict]:
    return [{"name": a.name, "description": a.description, "model_tier": a.model_tier}
            for a in REGISTRY.values()]


def execute_run(session: Session, project: Project, run: Run) -> Run:
    """Run an agent synchronously: build context, call the model, persist results."""
    agent_cls = REGISTRY.get(run.agent)
    if agent_cls is None:
        run.status, run.error = "failed", f"Unknown agent '{run.agent}'"
        run.finished_at = datetime.now(UTC)
        session.add(run)
        session.commit()
        return run

    session.add(run)
    session.flush()
    agent = agent_cls(session, project, run)
    run.status = "running"
    try:
        pack = build_context_pack(session, project, run.goal, agent.target_artifact_ids())
        run.context_pack = pack
        if not pack["items"]:
            # No prior context at all is worth knowing about: feed gap detection.
            session.add(Signal(workspace_id=project.workspace_id, project_id=project.id,
                               kind="context_miss", text=f"[{run.agent}] {run.goal}",
                               asker=f"agent:{run.agent}"))
        run.output = agent.execute(render_context(pack))
        run.status = "succeeded"
    except AgentError as exc:
        session.rollback()
        session.add(run)
        run.status, run.error = "failed", str(exc)
    except Exception as exc:  # model/network errors: record, don't crash the API
        session.rollback()
        session.add(run)
        run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
    run.finished_at = datetime.now(UTC)
    session.commit()
    session.refresh(run)
    return run


__all__ = ["REGISTRY", "execute_run", "list_agents", "AgentError"]
