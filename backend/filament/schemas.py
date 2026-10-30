"""API request/response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ArtifactType = Literal[
    "idea", "opportunity", "prd", "story", "flow", "adr", "api_contract",
    "task", "pr", "test_plan", "release", "incident", "doc", "feature_request",
    "ui_mock", "component_view",
]
ArtifactStatus = Literal["draft", "in_review", "approved", "done", "rejected"]
MemoryScope = Literal["org", "team", "project", "run"]
MemoryKind = Literal["fact", "decision", "standard", "procedure"]
Autonomy = Literal["L0", "L1", "L2", "L3", "L4"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WorkspaceIn(BaseModel):
    name: str


class WorkspaceOut(ORM):
    id: str
    name: str
    created_at: datetime


class TeamIn(BaseModel):
    name: str


class TeamOut(ORM):
    id: str
    workspace_id: str
    name: str


class ProjectIn(BaseModel):
    name: str
    description: str = ""
    team_id: str | None = None


class ProjectOut(ORM):
    id: str
    workspace_id: str
    team_id: str | None
    name: str
    description: str
    created_at: datetime


class ArtifactIn(BaseModel):
    type: ArtifactType
    title: str
    content: str = ""
    data: dict = Field(default_factory=dict)
    status: ArtifactStatus = "draft"
    author: str = "human"


class ArtifactPatch(BaseModel):
    title: str | None = None
    content: str | None = None
    data: dict | None = None
    status: ArtifactStatus | None = None


class ArtifactOut(ORM):
    id: str
    project_id: str
    type: str
    title: str
    content: str
    data: dict
    status: str
    version: int
    author: str
    run_id: str | None
    created_at: datetime
    updated_at: datetime


class LinkIn(BaseModel):
    source_id: str
    target_id: str
    kind: str


class LinkOut(ORM):
    id: str
    project_id: str
    source_id: str
    target_id: str
    kind: str


class GraphOut(BaseModel):
    nodes: list[ArtifactOut]
    edges: list[LinkOut]


class ImpactOut(BaseModel):
    artifact_id: str
    affected: list[ArtifactOut]


class MemoryIn(BaseModel):
    scope: MemoryScope
    scope_ref: str | None = None
    kind: MemoryKind = "fact"
    content: str
    author: str = "human"
    source_artifact_ids: list[str] = Field(default_factory=list)
    confidence: float = 1.0


class MemoryOut(ORM):
    id: str
    workspace_id: str
    scope: str
    scope_ref: str | None
    kind: str
    content: str
    status: str
    author: str
    run_id: str | None
    source_artifact_ids: list
    confidence: float
    created_at: datetime


class RunIn(BaseModel):
    agent: str
    goal: str
    input: dict = Field(default_factory=dict)
    autonomy: Autonomy = "L1"
    requested_by: str = "human"
    # Optional per-run overrides of the agent's configured defaults.
    runner: str | None = None
    model: str | None = None
    max_turns: int | None = Field(default=None, ge=1, le=100)
    token_budget: int | None = Field(default=None, ge=1_000)


class RunOut(ORM):
    id: str
    project_id: str
    parent_run_id: str | None
    agent: str
    goal: str
    status: str
    autonomy: str
    input: dict
    output: dict
    context_pack: dict
    trace: list
    turns: int
    config: dict
    runner_id: str | None
    claimed_by: str | None
    peak_context_tokens: int
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    error: str | None
    requested_by: str
    started_at: datetime
    finished_at: datetime | None


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = "human"
    comment: str = ""


class ApprovalOut(ORM):
    id: str
    artifact_id: str
    status: str
    decided_by: str | None
    comment: str
    created_at: datetime
    decided_at: datetime | None


class AskIn(BaseModel):
    question: str
    asker: str = "human"


class AskOut(BaseModel):
    answer: str
    citations: list[str]
    answered: bool


class UsageRow(BaseModel):
    key: str
    runs: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class ReadinessIn(BaseModel):
    repo: str = Field(description="Local path or git URL")
    project_id: str | None = None


class ReadinessCheckOut(BaseModel):
    id: str
    title: str
    passed: bool
    weight: int
    detail: str
    fix: str
    kind: str = "common"
    status: str = ""
    generate: str | None = None


class ReadinessOut(ORM):
    id: str
    repo: str
    project_id: str | None
    score: float
    grade: str
    checks: list[ReadinessCheckOut]
    created_at: datetime


class GapOut(BaseModel):
    topic: str
    count: int
    examples: list[str]


# -- MCP servers ------------------------------------------------------------------------------
McpScope = Literal["workspace", "team", "project"]


class McpServerIn(BaseModel):
    name: str
    description: str = ""
    scope: McpScope = "workspace"
    scope_ref: str | None = None
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    allowed_tools: list[str] = Field(default_factory=list)
    write_tools: list[str] = Field(default_factory=list)
    min_autonomy_for_writes: Autonomy = "L2"
    timeout_s: int = Field(default=30, ge=1, le=600)


class McpServerPatch(BaseModel):
    description: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    allowed_tools: list[str] | None = None
    write_tools: list[str] | None = None
    min_autonomy_for_writes: Autonomy | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=600)


class McpServerOut(ORM):
    id: str
    workspace_id: str
    name: str
    description: str
    scope: str
    scope_ref: str | None
    transport: str
    command: str | None
    args: list
    url: str | None
    env: dict
    headers: dict
    enabled: bool
    allowed_tools: list
    write_tools: list
    min_autonomy_for_writes: str
    timeout_s: int
    tools_cache: list
    last_test_ok: bool | None
    last_test_error: str | None
    last_tested_at: datetime | None


# -- runners and agent settings ----------------------------------------------------------------
RunnerKind = Literal["inline", "worker", "container", "kubernetes"]


class RunnerIn(BaseModel):
    name: str
    kind: RunnerKind = "worker"
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    is_default: bool = False
    max_concurrency: int = Field(default=4, ge=1, le=256)
    timeout_s: int = Field(default=900, ge=10, le=86_400)


class RunnerPatch(BaseModel):
    kind: RunnerKind | None = None
    description: str | None = None
    labels: list[str] | None = None
    config: dict | None = None
    is_default: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=256)
    timeout_s: int | None = Field(default=None, ge=10, le=86_400)


class RunnerOut(ORM):
    id: str
    workspace_id: str
    name: str
    kind: str
    description: str
    labels: list
    config: dict
    is_default: bool
    max_concurrency: int
    timeout_s: int
    last_seen_at: datetime | None
    queued: int = 0


class AgentSettingIn(BaseModel):
    runner: str | None = Field(default=None, description="Runner name or id; empty = default")
    model: str | None = None
    max_turns: int | None = Field(default=None, ge=1, le=100)
    token_budget: int | None = Field(default=None, ge=1_000)
    context_budget: int | None = Field(default=None, ge=500)


class AgentSettingOut(BaseModel):
    agent: str
    description: str
    runner: str
    model: str
    max_turns: int
    token_budget: int
    context_budget: int
    overridden: list[str]


# -- repos (Launchpad) ----------------------------------------------------------------------
class RepoIn(BaseModel):
    repo: str = Field(description="Local path or git URL")
    project_id: str | None = Field(default=None, description="Attach to an existing project")


class RepoSummary(ORM):
    id: str
    workspace_id: str
    project_id: str
    ref: str
    name: str
    kinds: list
    score: float
    grade: str
    generated: dict
    scanned_at: datetime


class RepoDetail(RepoSummary):
    analysis: dict


class GenerateIn(BaseModel):
    items: list[str] = Field(default_factory=list, description="Generator ids; empty = all missing")
