"""Agent registry and runner."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from filament.agents.base import Agent, AgentError
from filament.agents.flow import FlowAgent
from filament.agents.orchestrator import OrchestratorAgent
from filament.agents.requirements import RequirementsAgent
from filament.context import build_context_pack, render_context
from filament.models import Project, Run, Signal

REGISTRY: dict[str, type[Agent]] = {
    RequirementsAgent.name: RequirementsAgent,
    FlowAgent.name: FlowAgent,
    OrchestratorAgent.name: OrchestratorAgent,
}


AUDIT_FIELDS = ("trace", "turns", "model", "input_tokens", "output_tokens", "cost_usd",
                "context_pack", "peak_context_tokens")


def list_agents() -> list[dict]:
    return [{"name": a.name, "description": a.description, "model_tier": a.model_tier,
             "tools": a.tools} for a in REGISTRY.values()]


def execute_run(session: Session, project: Project, run: Run, commit: bool = True) -> Run:
    """Run an agent's tool-use loop synchronously and persist results.

    The agent's work happens inside a savepoint: if the run fails, its partial writes are
    rolled back but the run record (status, error, tokens, trace) is kept. `commit=False` is
    used for child runs started by the orchestrator, which commit with their parent.
    """
    agent_cls = REGISTRY.get(run.agent)
    session.add(run)
    session.flush()
    if agent_cls is None:
        run.status, run.error = "failed", f"Unknown agent '{run.agent}'"
        return _finish(session, run, commit)

    agent = agent_cls(session, project, run)
    run.status = "running"
    savepoint = session.begin_nested()
    try:
        pack = build_context_pack(session, project, run.goal, agent.target_artifact_ids(),
                                  budget=(run.config or {}).get("context_budget"))
        run.context_pack = pack
        if not pack["items"]:
            # No prior context at all is worth knowing about: feed gap detection.
            session.add(Signal(workspace_id=project.workspace_id, project_id=project.id,
                               kind="context_miss", text=f"[{run.agent}] {run.goal}",
                               asker=f"agent:{run.agent}"))
        output = agent.execute(render_context(pack))
        savepoint.commit()
        run.output = output
        run.status = "succeeded" if output.get("stop_reason") in {"finished", "end_turn"} \
            else "stopped"
        if run.status == "stopped":
            run.error = f"Stopped by guardrail: {output.get('stop_reason')}"
    except Exception as exc:  # agent, model or network errors: record, never crash the API
        # Keep the audit record (tokens, trace) even though the agent's writes are undone.
        kept = {k: getattr(run, k) for k in AUDIT_FIELDS}
        if savepoint.is_active:
            savepoint.rollback()
        for key, value in kept.items():
            setattr(run, key, value)
        run.status = "failed"
        run.error = str(exc) if isinstance(exc, AgentError) else f"{type(exc).__name__}: {exc}"
    return _finish(session, run, commit)


def _finish(session: Session, run: Run, commit: bool) -> Run:
    run.finished_at = datetime.now(UTC)
    if commit:
        session.commit()
        session.refresh(run)
    else:
        session.flush()
    return run


__all__ = ["REGISTRY", "execute_run", "list_agents", "AgentError"]
