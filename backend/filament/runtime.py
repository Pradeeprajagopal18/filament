"""Where and how runs execute: config resolution, the run queue and the worker loop.

Resolution order for every setting (first match wins):
  1. the run request itself (explicit override)
  2. the workspace's AgentSetting for that agent
  3. the workspace default runner / global settings
  4. built-in defaults (runner "inline" = inside the API process)
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.models import AgentSetting, Project, Run, Runner

INLINE = "inline"
RUNNER_KINDS = ["inline", "worker", "container", "kubernetes"]


def resolve_config(session: Session, project: Project, agent: str,
                   overrides: dict | None = None) -> dict:
    overrides = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}
    s = get_settings()
    setting = session.scalars(select(AgentSetting).where(
        AgentSetting.workspace_id == project.workspace_id, AgentSetting.agent == agent)).first()

    runner = None
    if overrides.get("runner") == INLINE:
        runner = None  # explicit request to run inside the API process
    elif overrides.get("runner"):
        runner = session.scalars(select(Runner).where(
            Runner.workspace_id == project.workspace_id,
            (Runner.name == overrides["runner"]) | (Runner.id == overrides["runner"]))).first()
        if runner is None:
            raise ValueError(f"Unknown runner '{overrides['runner']}'")
    elif setting and setting.runner_id:
        runner = session.get(Runner, setting.runner_id)
    else:
        runner = session.scalars(select(Runner).where(
            Runner.workspace_id == project.workspace_id, Runner.is_default.is_(True))).first()

    def pick(key: str, global_value):
        if key in overrides:
            return overrides[key]
        value = getattr(setting, key, None) if setting else None
        return value if value is not None else global_value

    return {
        "runner_id": runner.id if runner else None,
        "runner": runner.name if runner else INLINE,
        "runner_kind": runner.kind if runner else INLINE,
        "model": pick("model", s.model_default),
        "model_explicit": "model" in overrides or bool(setting and setting.model),
        "max_turns": int(pick("max_turns", s.max_agent_turns)),
        "token_budget": int(pick("token_budget", s.run_token_budget)),
        "context_budget": int(pick("context_budget", s.context_budget_tokens)),
    }


def claim_next(session: Session, runner: Runner, worker: str) -> Run | None:
    """Atomically claim the oldest queued run for this runner (safe with several workers)."""
    candidates = session.scalars(select(Run.id).where(
        Run.runner_id == runner.id, Run.status == "queued").order_by(Run.started_at)).all()
    for run_id in candidates:
        res = session.execute(update(Run).where(Run.id == run_id, Run.status == "queued")
                              .values(status="claimed", claimed_by=worker))
        session.commit()
        if res.rowcount == 1:
            return session.get(Run, run_id)
    return None


def heartbeat(session: Session, runner: Runner) -> None:
    runner.last_seen_at = datetime.now(UTC)
    session.commit()


def work_once(session: Session, runner: Runner, worker: str) -> Run | None:
    from filament.agents import execute_run

    heartbeat(session, runner)
    run = claim_next(session, runner, worker)
    if run is None:
        return None
    project = session.get(Project, run.project_id)
    return execute_run(session, project, run)


def work_forever(session_factory, runner_name: str, workspace_id: str, worker: str,
                 poll_s: float = 2.0, stop_after: int | None = None) -> None:
    """The worker loop behind `fil runner start`. Runs agents on *this* machine."""
    done = 0
    while stop_after is None or done < stop_after:
        with session_factory() as session:
            runner = session.scalars(select(Runner).where(
                Runner.workspace_id == workspace_id, Runner.name == runner_name)).first()
            if runner is None:
                raise SystemExit(f"Runner '{runner_name}' not found in workspace {workspace_id}")
            run = work_once(session, runner, worker)
        if run is None:
            time.sleep(poll_s)
        else:
            done += 1
            print(f"[{worker}] {run.agent} run {run.id}: {run.status}", flush=True)
