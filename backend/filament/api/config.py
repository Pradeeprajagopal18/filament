"""Configuration API: MCP servers, runners, agent defaults, and config-as-code (YAML)."""

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from filament import schemas as s
from filament.agents import REGISTRY
from filament.api.graph import get_or_404
from filament.db import get_session
from filament.mcp_gateway import McpConfigError, test_server, validate_config
from filament.models import AgentSetting, McpServer, Project, Run, Runner, Workspace
from filament.runtime import resolve_config

router = APIRouter()


# -- MCP servers --------------------------------------------------------------------------------
def _validate(server: McpServer) -> None:
    try:
        validate_config(server.name, server.transport, server.command, server.url,
                        server.env or {}, server.headers or {}, server.min_autonomy_for_writes)
    except McpConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    if server.scope != "workspace" and not server.scope_ref:
        raise HTTPException(422, f"scope '{server.scope}' needs scope_ref (the team/project id)")


@router.get("/workspaces/{ws_id}/mcp", response_model=list[s.McpServerOut])
def list_mcp(ws_id: str, session: Session = Depends(get_session)):
    stmt = select(McpServer).where(McpServer.workspace_id == ws_id).order_by(McpServer.name)
    return session.scalars(stmt).all()


@router.post("/workspaces/{ws_id}/mcp", response_model=s.McpServerOut, status_code=201)
def add_mcp(ws_id: str, body: s.McpServerIn, test: bool = False,
            session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    server = McpServer(workspace_id=ws_id, **body.model_dump())
    _validate(server)
    # SQL treats NULL scope_refs as distinct, so check duplicates explicitly.
    exists = session.scalars(select(McpServer.id).where(
        McpServer.workspace_id == ws_id, McpServer.name == body.name,
        McpServer.scope == body.scope, McpServer.scope_ref.is_(body.scope_ref)
        if body.scope_ref is None else McpServer.scope_ref == body.scope_ref)).first()
    if exists:
        raise HTTPException(409, f"An MCP server named '{body.name}' already exists in this "
                                 "scope")
    session.add(server)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"An MCP server named '{body.name}' already exists in this "
                                 "scope") from exc
    return test_server(session, server) if test else server


@router.patch("/mcp/{server_id}", response_model=s.McpServerOut)
def update_mcp(server_id: str, body: s.McpServerPatch, session: Session = Depends(get_session)):
    server = get_or_404(session, McpServer, server_id)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(server, key, value)
    _validate(server)
    session.commit()
    return server


@router.post("/mcp/{server_id}/test", response_model=s.McpServerOut)
def test_mcp(server_id: str, session: Session = Depends(get_session)):
    """Connect to the server, list its tools and cache them (agents only see cached tools)."""
    return test_server(session, get_or_404(session, McpServer, server_id))


@router.delete("/mcp/{server_id}", status_code=204)
def delete_mcp(server_id: str, session: Session = Depends(get_session)):
    session.delete(get_or_404(session, McpServer, server_id))
    session.commit()


@router.get("/projects/{project_id}/mcp-tools")
def project_mcp_tools(project_id: str, session: Session = Depends(get_session)):
    """The MCP tools an agent working in this project will be offered."""
    from filament.mcp_gateway import tools_for

    project = get_or_404(session, Project, project_id)
    return [{"name": t.agent_name, "server": t.server.name, "scope": t.server.scope,
             "tool": t.tool["name"], "writes": t.writes,
             "min_autonomy": t.server.min_autonomy_for_writes if t.writes else None}
            for t in tools_for(session, project)]


# -- runners --------------------------------------------------------------------------------------
def _runner_out(session: Session, r: Runner) -> dict:
    queued = session.scalar(select(func.count(Run.id)).where(
        Run.runner_id == r.id, Run.status == "queued")) or 0
    return {**s.RunnerOut.model_validate(r).model_dump(), "queued": queued}


def _set_default(session: Session, runner: Runner) -> None:
    for other in session.scalars(select(Runner).where(Runner.workspace_id == runner.workspace_id,
                                                      Runner.id != runner.id)):
        other.is_default = False


@router.get("/workspaces/{ws_id}/runners", response_model=list[s.RunnerOut])
def list_runners(ws_id: str, session: Session = Depends(get_session)):
    runners = session.scalars(select(Runner).where(Runner.workspace_id == ws_id)
                              .order_by(Runner.name)).all()
    return [_runner_out(session, r) for r in runners]


@router.post("/workspaces/{ws_id}/runners", response_model=s.RunnerOut, status_code=201)
def add_runner(ws_id: str, body: s.RunnerIn, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    runner = Runner(workspace_id=ws_id, **body.model_dump())
    session.add(runner)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"A runner named '{body.name}' already exists") from exc
    if runner.is_default:
        _set_default(session, runner)
    session.commit()
    return _runner_out(session, runner)


@router.patch("/runners/{runner_id}", response_model=s.RunnerOut)
def update_runner(runner_id: str, body: s.RunnerPatch, session: Session = Depends(get_session)):
    runner = get_or_404(session, Runner, runner_id)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(runner, key, value)
    if body.is_default:
        _set_default(session, runner)
    session.commit()
    return _runner_out(session, runner)


@router.delete("/runners/{runner_id}", status_code=204)
def delete_runner(runner_id: str, session: Session = Depends(get_session)):
    runner = get_or_404(session, Runner, runner_id)
    for st in session.scalars(select(AgentSetting).where(AgentSetting.runner_id == runner.id)):
        st.runner_id = None
    session.delete(runner)
    session.commit()


# -- agent settings -------------------------------------------------------------------------------
def _agent_out(session: Session, ws_id: str, agent: str) -> dict:
    probe = Project(id="probe", workspace_id=ws_id, name="probe")
    cfg = resolve_config(session, probe, agent)
    setting = session.scalars(select(AgentSetting).where(
        AgentSetting.workspace_id == ws_id, AgentSetting.agent == agent)).first()
    overridden = [k for k in ("runner_id", "model", "max_turns", "token_budget", "context_budget")
                  if setting is not None and getattr(setting, k) is not None]
    return {"agent": agent, "description": REGISTRY[agent].description, "runner": cfg["runner"],
            "model": cfg["model"], "max_turns": cfg["max_turns"],
            "token_budget": cfg["token_budget"], "context_budget": cfg["context_budget"],
            "overridden": [o.replace("runner_id", "runner") for o in overridden]}


@router.get("/workspaces/{ws_id}/agent-settings", response_model=list[s.AgentSettingOut])
def list_agent_settings(ws_id: str, session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    return [_agent_out(session, ws_id, a) for a in REGISTRY]


@router.put("/workspaces/{ws_id}/agent-settings/{agent}", response_model=s.AgentSettingOut)
def set_agent_setting(ws_id: str, agent: str, body: s.AgentSettingIn,
                      session: Session = Depends(get_session)):
    get_or_404(session, Workspace, ws_id)
    _upsert_agent_setting(session, ws_id, agent, body)
    session.commit()
    return _agent_out(session, ws_id, agent)


def _upsert_agent_setting(session: Session, ws_id: str, agent: str,
                          body: s.AgentSettingIn) -> None:
    if agent not in REGISTRY:
        raise HTTPException(404, f"Unknown agent '{agent}'")
    setting = session.scalars(select(AgentSetting).where(
        AgentSetting.workspace_id == ws_id, AgentSetting.agent == agent)).first()
    if setting is None:
        setting = AgentSetting(workspace_id=ws_id, agent=agent)
        session.add(setting)
    runner_id = None
    if body.runner:
        runner = session.scalars(select(Runner).where(
            Runner.workspace_id == ws_id,
            (Runner.name == body.runner) | (Runner.id == body.runner))).first()
        if runner is None:
            raise HTTPException(422, f"Unknown runner '{body.runner}'")
        runner_id = runner.id
    # PUT replaces: unset fields fall back to the workspace defaults.
    setting.runner_id, setting.model = runner_id, body.model or None
    setting.max_turns, setting.token_budget = body.max_turns, body.token_budget
    setting.context_budget = body.context_budget


# -- config as code -------------------------------------------------------------------------------
@router.get("/workspaces/{ws_id}/config", response_class=PlainTextResponse)
def export_config(ws_id: str, session: Session = Depends(get_session)):
    """Export MCP servers, runners and agent settings as YAML (secrets stay as references)."""
    get_or_404(session, Workspace, ws_id)
    runners = session.scalars(select(Runner).where(Runner.workspace_id == ws_id)).all()
    names = {r.id: r.name for r in runners}
    doc = {
        "runners": [{k: v for k, v in {
            "name": r.name, "kind": r.kind, "description": r.description or None,
            "labels": r.labels or None, "config": r.config or None,
            "default": r.is_default or None, "max_concurrency": r.max_concurrency,
            "timeout_s": r.timeout_s}.items() if v is not None} for r in runners],
        "mcp_servers": [{k: v for k, v in {
            "name": m.name, "description": m.description or None, "scope": m.scope,
            "scope_ref": m.scope_ref, "transport": m.transport, "command": m.command,
            "args": m.args or None, "url": m.url, "env": m.env or None,
            "headers": m.headers or None, "enabled": m.enabled,
            "allowed_tools": m.allowed_tools or None, "write_tools": m.write_tools or None,
            "min_autonomy_for_writes": m.min_autonomy_for_writes,
            "timeout_s": m.timeout_s}.items() if v is not None}
            for m in session.scalars(select(McpServer).where(McpServer.workspace_id == ws_id))],
        "agents": {a.agent: {k: v for k, v in {
            "runner": names.get(a.runner_id), "model": a.model, "max_turns": a.max_turns,
            "token_budget": a.token_budget, "context_budget": a.context_budget}.items()
            if v is not None}
            for a in session.scalars(select(AgentSetting).where(
                AgentSetting.workspace_id == ws_id))},
    }
    return yaml.safe_dump(doc, sort_keys=False)


@router.post("/workspaces/{ws_id}/config")
def apply_config(ws_id: str, body: str = Body(..., media_type="text/plain"),
                 session: Session = Depends(get_session)):
    """Apply a YAML config: create or update runners, MCP servers and agent settings by name.

    All-or-nothing: if any entry is invalid, nothing is changed.
    """
    get_or_404(session, Workspace, ws_id)
    try:
        doc = yaml.safe_load(body) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(422, f"Invalid YAML: {exc}") from exc
    changed = {"runners": 0, "mcp_servers": 0, "agents": 0}
    try:
        for item in doc.get("runners", []) or []:
            item = dict(item)
            item["is_default"] = bool(item.pop("default", False))
            spec = s.RunnerIn(**item)
            runner = session.scalars(select(Runner).where(
                Runner.workspace_id == ws_id, Runner.name == spec.name)).first()
            if runner is None:
                runner = Runner(workspace_id=ws_id)
                session.add(runner)
            for k, v in spec.model_dump().items():
                setattr(runner, k, v)
            session.flush()
            if runner.is_default:
                _set_default(session, runner)
            changed["runners"] += 1
        for item in doc.get("mcp_servers", []) or []:
            spec = s.McpServerIn(**item)
            server = session.scalars(select(McpServer).where(
                McpServer.workspace_id == ws_id, McpServer.name == spec.name,
                McpServer.scope == spec.scope, McpServer.scope_ref == spec.scope_ref)).first()
            if server is None:
                server = McpServer(workspace_id=ws_id)
                session.add(server)
            for k, v in spec.model_dump().items():
                setattr(server, k, v)
            _validate(server)
            changed["mcp_servers"] += 1
        session.flush()
        for agent, item in (doc.get("agents", {}) or {}).items():
            _upsert_agent_setting(session, ws_id, agent, s.AgentSettingIn(**(item or {})))
            changed["agents"] += 1
    except HTTPException:
        session.rollback()
        raise
    except (ValueError, TypeError) as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"applied": changed}
