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

from igniteai.db import Base


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
    # SSO group whose members belong to this team (synced at each sign-in).
    sso_group: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sso_role: Mapped[str] = mapped_column(String(20), default="contributor")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    # Restricted projects are visible only to their members (team, explicit, admins); others
    # are open to every workspace member as a contributor.
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # Who signs off at this checkpoint: product_owner | tech_lead | engineer |
    # release_manager | on_call (from the artifact type; see delivery.service.ROLE_FOR).
    role: Mapped[str | None] = mapped_column(String(30), nullable=True)
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


class SloSample(Base):
    """One measurement for an SLO (an `slo` artifact): good events out of total in a period."""

    __tablename__ = "slo_samples"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    slo_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    good: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)


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
    # native: a real MCP server. generated: built by Ignite from a repo's API contract.
    kind: Mapped[str] = mapped_column(String(12), default="native")
    source_repo_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    redact: Mapped[bool] = mapped_column(Boolean, default=True)  # scrub PII/secrets in results
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Runner(Base):
    """Where agent runs execute. `inline` runs in the API process; other kinds are queued and
    picked up by a worker started with `ign runner start` wherever it should run."""

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


class RepoRecord(Base):
    """A repository onboarded through Launchpad, with its latest analysis.

    `generated` maps a generator id (agents_md, api_contract, ...) to the artifact Ignite
    created for it, so readiness items can be satisfied in the platform before they exist in
    the repo itself.
    """

    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("workspace_id", "ref"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    ref: Mapped[str] = mapped_column(String(500))  # local path or git URL
    name: Mapped[str] = mapped_column(String(200))
    kinds: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float, default=0)
    grade: Mapped[str] = mapped_column(String(2), default="F")
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    generated: Mapped[dict] = mapped_column(JSON, default=dict)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Periodic rescans keep Ignite's references current (0 = manual only; None = default).
    scan_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Where a running instance serves its generated spec (springdoc /v3/api-docs, FastAPI
    # /openapi.json, swaggo /swagger/doc.json...). Scans read it as the contract when set.
    spec_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Visibility on the git host (None = unknown, e.g. a local path). Private repos are shown
    # only to people who can read them there (or who hold a role on the repo's project).
    private: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RepoScan(Base):
    """One scan of a repo and what changed since the previous one."""

    __tablename__ = "repo_scans"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    repo_id: Mapped[str] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    # onboard | manual | schedule | webhook
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | failed
    grade: Mapped[str] = mapped_column(String(2), default="F")
    score: Mapped[float] = mapped_column(Float, default=0)
    commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class McpToolEntry(Base):
    """A tool in the gateway registry: the source of truth for what agents may call.

    Discovered and generated tools start disabled until an owner enables them. Changes found on
    re-sync (or description edits) wait in `pending` until approved; every applied change is
    kept in `history`, so an owner can roll back.
    """

    __tablename__ = "mcp_tools"
    __table_args__ = (UniqueConstraint("server_id", "name"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"),
                                           index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    write: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="new")  # new|active|changed|removed
    version: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list] = mapped_column(JSON, default=list)
    pending: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    allowed_agents: Mapped[list] = mapped_column(JSON, default=list)  # empty = any agent
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. method/path for generated tools
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class McpCallLog(Base):
    """Every tool call through the gateway, for observability and audit."""

    __tablename__ = "mcp_calls"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    server_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    server: Mapped[str] = mapped_column(String(60))
    tool: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(120))  # agent:<name> | cli | omni | api
    run_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok|error|denied|rate_limited|simulated
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    bytes_raw: Mapped[int] = mapped_column(Integer, default=0)
    bytes_returned: Mapped[int] = mapped_column(Integer, default=0)
    redactions: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class CodeGraph(Base):
    """A repo's symbol-level code graph (functions, classes, modules and their relations),
    kept apart from the analysis because it can be large."""

    __tablename__ = "code_graphs"

    repo_id: Mapped[str] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"),
                                         primary_key=True)
    engine: Mapped[str] = mapped_column(String(40), default="ignite")
    engine_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graph: Mapped[dict] = mapped_column(JSON, default=dict)  # {nodes, edges}
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# -- access control -------------------------------------------------------------------------------
class User(Base):
    """A person: a local account, or someone from the company directory (LDAP) or SSO (OIDC)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    # Stable id from where the account lives: "local:<username>", "ldap:<dn>" or the SSO subject.
    sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    auth_source: Mapped[str] = mapped_column(String(10), default="oidc")  # local | ldap | oidc
    # Local accounts only: scrypt hash; never the password itself.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                                 nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # GitHub username, from an SSO claim or set by an admin (never self-asserted), used to
    # mirror GitHub repo permissions.
    github_login: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # admin of every workspace
    groups: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Membership(Base):
    """A role for a user on a workspace (admin | member), team (maintainer | contributor |
    viewer) or project (owner | maintainer | contributor | viewer)."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "scope", "scope_id"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))  # workspace | team | project
    scope_id: Mapped[str] = mapped_column(String(12), index=True)
    role: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | sso
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CheckpointRole(Base):
    """Who may sign off a delivery checkpoint (product_owner, tech_lead, engineer,
    release_manager, on_call), for one project or (project_id None) the whole workspace."""

    __tablename__ = "checkpoint_roles"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id", "project_id", "role"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(30))


class ApiToken(Base):
    """A personal token for the CLI and MCP clients. Only a hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    """Who did what: sign-ins, role changes, approvals, releases, denied access."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    user_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(60), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    resource: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AuthSetting(Base):
    """Sign-in settings changed by an admin in the web app. One row; it overrides the matching
    IGNITE_* environment settings. Secrets are kept as ${env:NAME} references, never values."""

    __tablename__ = "auth_settings"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="auth")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now,
                                                 onupdate=now)


class FeatureAccess(Base):
    """Who may use one area of the app in a workspace (Launchpad, delivery, Ask, ...).
    No row means everyone. Admins always have every feature."""

    __tablename__ = "feature_access"
    __table_args__ = (UniqueConstraint("workspace_id", "feature"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    feature: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(10), default="everyone")  # everyone | some | off
    teams: Mapped[list] = mapped_column(JSON, default=list)    # team ids
    groups: Mapped[list] = mapped_column(JSON, default=list)   # directory / SSO group names
    users: Mapped[list] = mapped_column(JSON, default=list)    # user ids
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now,
                                                 onupdate=now)
