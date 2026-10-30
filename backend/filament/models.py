"""Core data model: the project graph, shared memory, agent runs and Launchpad reports."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from filament.db import Base


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(UTC)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(200))


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="project")


class Artifact(Base):
    """A typed, versioned node in the project graph (idea, prd, story, flow, adr, task, ...)."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    author: Mapped[str] = mapped_column(String(120), default="human")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    project: Mapped[Project] = relationship(back_populates="artifacts")


class Link(Base):
    """A typed edge between artifacts, e.g. story -implements-> prd."""

    __tablename__ = "links"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))


class MemoryEntry(Base):
    """A fact, decision, standard or procedure in shared memory, with provenance."""

    __tablename__ = "memory"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))  # org | team | project | run
    scope_ref: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="fact")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    author: Mapped[str] = mapped_column(String(120), default="human")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    source_artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    """One execution of an agent against a goal, fully logged including tokens and context."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True,
                                                      index=True)
    agent: Mapped[str] = mapped_column(String(60), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    autonomy: Mapped[str] = mapped_column(String(4), default="L1")
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    context_pack: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[list] = mapped_column(JSON, default=list)  # every model turn and tool call
    turns: Mapped[int] = mapped_column(Integer, default=0)
    # Resolved configuration this run used (runner, model, budgets): kept for audit.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    runner_id: Mapped[str | None] = mapped_column(ForeignKey("runners.id"), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    peak_context_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(120), default="human")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Approval(Base):
    """A human gate on an artifact moving forward."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Signal(Base):
    """A demand signal for gap detection: a question asked, a search with no answer, a miss."""

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))  # question | no_answer | context_miss
    text: Mapped[str] = mapped_column(Text)
    asker: Mapped[str] = mapped_column(String(120), default="human")
    answered: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReadinessReport(Base):
    """Launchpad repo readiness result."""

    __tablename__ = "readiness_reports"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    repo: Mapped[str] = mapped_column(String(500), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    score: Mapped[float] = mapped_column(Float)
    grade: Mapped[str] = mapped_column(String(2))
    checks: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class McpServer(Base):
    """An MCP server teams make available to agents, scoped to a workspace, team or project.

    Secrets are never stored: env and header values that hold credentials must be references
    like ${env:GITHUB_TOKEN}, resolved from the runner's environment at call time.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("workspace_id", "scope", "scope_ref", "name"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20), default="workspace")  # workspace|team|project
    scope_ref: Mapped[str | None] = mapped_column(String(12), nullable=True)
    name: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(10), default="stdio")  # stdio | http
    command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    args: Mapped[list] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)  # empty = all discovered
    write_tools: Mapped[list] = mapped_column(JSON, default=list)  # tools that change things
    min_autonomy_for_writes: Mapped[str] = mapped_column(String(4), default="L2")
    timeout_s: Mapped[int] = mapped_column(Integer, default=30)
    tools_cache: Mapped[list] = mapped_column(JSON, default=list)  # discovered on test/refresh
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Runner(Base):
    """Where agent runs execute. `inline` runs in the API process; other kinds are queued and
    picked up by a worker started with `fil runner start` wherever it should run."""

    __tablename__ = "runners"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    # inline | worker | container | kubernetes
    kind: Mapped[str] = mapped_column(String(20), default="inline")
    description: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[list] = mapped_column(JSON, default=list)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. image, namespace, network
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4)
    timeout_s: Mapped[int] = mapped_column(Integer, default=900)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentSetting(Base):
    """Per-workspace defaults for an agent: where it runs, which model, and its budgets."""

    __tablename__ = "agent_settings"
    __table_args__ = (UniqueConstraint("workspace_id", "agent"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    agent: Mapped[str] = mapped_column(String(60))
    runner_id: Mapped[str | None] = mapped_column(ForeignKey("runners.id"), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
