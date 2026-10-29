"""API request/response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ArtifactType = Literal[
    "idea", "opportunity", "prd", "story", "flow", "adr", "api_contract",
    "task", "pr", "test_plan", "release", "incident", "doc", "feature_request",
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
