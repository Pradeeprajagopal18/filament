"""MCP gateway API: registry (control plane), invoke/discover (data plane), metrics, and the
Omni MCP endpoint that exposes the whole gateway to external agents as one MCP server."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from filament import __version__
from filament import mcp_registry as reg
from filament import schemas as s
from filament.api.graph import get_or_404
from filament.db import get_session
from filament.models import McpCallLog, McpServer, McpToolEntry, Workspace

router = APIRouter()


def _err(exc: reg.GatewayError) -> HTTPException:
    code = {"denied": 403, "rate_limited": 429, "not_found": 404, "conflict": 409}.get(
        exc.status, 400)
    return HTTPException(code, str(exc))


# -- registry ----------------------------------------------------------------------------------
@router.get("/workspaces/{ws_id}/mcp/registry")
def registry(ws_id: str, q: str = "", session: Session = Depends(get_session)):
    """Every server and its tools with status, for the registry view (and `fil mcp search`)."""
    get_or_404(session, Workspace, ws_id)
    words = reg._words(q)
    out = []
    for server in session.scalars(select(McpServer).where(McpServer.workspace_id == ws_id)
                                  .order_by(McpServer.kind, McpServer.name)):
        entries = session.scalars(select(McpToolEntry).where(
            McpToolEntry.server_id == server.id).order_by(McpToolEntry.name)).all()
        if words:
            entries = [e for e in entries if words & reg._words(f"{server.name} {e.name} "
                                                                 f"{e.description}")]
            if not entries:
                continue
        counts = {st: sum(e.status == st for e in entries) for st in
                  ("new", "active", "changed", "removed")}
        out.append({"id": server.id, "name": server.name, "kind": server.kind,
                    "description": server.description, "enabled": server.enabled,
                    "url": server.url, "transport": server.transport, "redact": server.redact,
                    "source_repo_id": server.source_repo_id,
                    "min_autonomy_for_writes": server.min_autonomy_for_writes,
                    "last_synced_at": server.last_synced_at,
                    "tools": len(entries), "enabled_tools": sum(e.enabled for e in entries),
                    "counts": counts,
                    "matches": [{"name": e.name, "description": e.description[:160],
                                 "enabled": e.enabled, "write": e.write}
                                for e in entries[:20]] if words else []})
    return out


@router.get("/mcp/{server_id}/tools", response_model=list[s.McpToolOut])
def server_tools(server_id: str, session: Session = Depends(get_session)):
    get_or_404(session, McpServer, server_id)
    return session.scalars(select(McpToolEntry).where(McpToolEntry.server_id == server_id)
                           .order_by(McpToolEntry.status != "changed", McpToolEntry.name)).all()


@router.post("/mcp/{server_id}/sync")
def sync_server(server_id: str, session: Session = Depends(get_session)):
    """Re-read a server's tools (native) or its repo's API (generated) into the registry."""
    server = get_or_404(session, McpServer, server_id)
    try:
        if server.kind == "generated":
            from filament.models import RepoRecord

            record = session.get(RepoRecord, server.source_repo_id or "")
            if record is None:
                raise HTTPException(404, "The repo this server was generated from is gone")
            reg.sync_generated(session, record)
            summary = {"synced": True}
        else:
            summary = reg.sync_native(session, server)
            server.last_test_ok, server.last_test_error = True, None
    except HTTPException:
        raise
    except BaseException as exc:  # noqa: BLE001
        raise HTTPException(502, f"Sync failed: {type(exc).__name__}: {exc}") from exc
    session.commit()
    return summary


@router.patch("/mcp-tools/{tool_id}", response_model=s.McpToolOut)
def update_tool(tool_id: str, body: s.McpToolPatch, session: Session = Depends(get_session)):
    """Owner settings for one tool. A new description is proposed and needs approval."""
    entry = get_or_404(session, McpToolEntry, tool_id)
    data = body.model_dump(exclude_none=True)
    if "description" in data and data["description"] != entry.description:
        reg.propose_description(entry, data.pop("description"), "owner")
    data.pop("description", None)
    if "rate_limit_per_min" in data:
        entry.rate_limit_per_min = data.pop("rate_limit_per_min") or None
    if data.get("enabled") and entry.status == "removed":
        raise HTTPException(409, "This tool no longer exists on the server")
    for k, v in data.items():
        setattr(entry, k, v)
    if "enabled" in data and entry.status in {"new", "active"}:
        entry.status = "active" if entry.enabled else "new"
    session.commit()
    return entry


@router.post("/mcp-tools/{tool_id}/approve", response_model=s.McpToolOut)
def approve_tool(tool_id: str, session: Session = Depends(get_session)):
    entry = get_or_404(session, McpToolEntry, tool_id)
    try:
        reg.approve(entry)
    except reg.GatewayError as exc:
        raise _err(exc) from exc
    session.commit()
    return entry


@router.post("/mcp-tools/{tool_id}/reject", response_model=s.McpToolOut)
def reject_tool(tool_id: str, session: Session = Depends(get_session)):
    entry = get_or_404(session, McpToolEntry, tool_id)
    reg.reject(entry)
    session.commit()
    return entry


@router.post("/mcp-tools/{tool_id}/rollback", response_model=s.McpToolOut)
def rollback_tool(tool_id: str, body: s.RollbackIn, session: Session = Depends(get_session)):
    entry = get_or_404(session, McpToolEntry, tool_id)
    try:
        reg.rollback(entry, body.version)
    except reg.GatewayError as exc:
        raise _err(exc) from exc
    session.commit()
    return entry


# -- data plane ----------------------------------------------------------------------------------
@router.get("/workspaces/{ws_id}/mcp/discover")
def discover(ws_id: str, q: str = "", session: Session = Depends(get_session)):
    return reg.discover_servers(session, ws_id, q)


@router.get("/workspaces/{ws_id}/mcp/schema")
def schema(ws_id: str, server: str, tool: str, session: Session = Depends(get_session)):
    try:
        srv, entry = reg.find(session, ws_id, server, tool)
    except reg.GatewayError as exc:
        raise _err(exc) from exc
    return {"server": srv.name, "tool": entry.name, "description": entry.description,
            "write": entry.write, "enabled": entry.enabled, "input_schema": entry.input_schema}


@router.post("/workspaces/{ws_id}/mcp/invoke")
def invoke(ws_id: str, body: s.InvokeIn, session: Session = Depends(get_session)):
    """Call a tool through the gateway (policy, projection, redaction and logging apply)."""
    try:
        srv, entry = reg.find(session, ws_id, body.server, body.tool)
        out = reg.invoke(session, ws_id, srv, entry, body.arguments, actor=body.actor,
                         autonomy=body.autonomy, fields=body.fields, max_chars=200_000)
    except reg.GatewayError as exc:
        session.commit()  # keep the denied/failed call in the log
        raise _err(exc) from exc
    session.commit()
    return out


@router.get("/workspaces/{ws_id}/mcp/metrics")
def gateway_metrics(ws_id: str, hours: int = 24, session: Session = Depends(get_session)):
    return reg.metrics(session, ws_id, max(1, min(hours, 24 * 30)))


@router.get("/workspaces/{ws_id}/mcp/calls")
def gateway_calls(ws_id: str, limit: int = 50, session: Session = Depends(get_session)):
    rows = session.scalars(select(McpCallLog).where(McpCallLog.workspace_id == ws_id)
                           .order_by(McpCallLog.at.desc()).limit(min(limit, 500))).all()
    return [{"server": r.server, "tool": r.tool, "actor": r.actor, "status": r.status,
             "latency_ms": r.latency_ms, "bytes_raw": r.bytes_raw,
             "bytes_returned": r.bytes_returned, "redactions": r.redactions, "error": r.error,
             "run_id": r.run_id, "at": r.at} for r in rows]


# -- Omni MCP: the whole gateway as one MCP server -------------------------------------------------
OMNI_TOOLS = [
    {"name": "discover_servers", "description": "Find MCP servers in this Filament workspace "
     "(internal APIs generated from repos and third-party tools) by what you need.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "list_tools", "description": "List the enabled tools on one server.",
     "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}},
                     "required": ["server"]}},
    {"name": "get_tool_schema", "description": "Input schema of one tool.",
     "inputSchema": {"type": "object", "properties": {"server": {"type": "string"},
                                                      "tool": {"type": "string"}},
                     "required": ["server", "tool"]}},
    {"name": "invoke_tool", "description": "Call a tool. `fields` keeps only those dotted paths "
     "of the result.",
     "inputSchema": {"type": "object", "properties": {
         "server": {"type": "string"}, "tool": {"type": "string"},
         "arguments": {"type": "object"},
         "fields": {"type": "array", "items": {"type": "string"}}},
         "required": ["server", "tool"]}},
]


def _omni_tools() -> list[dict]:
    from filament.repos.code_tools import SPECS

    return OMNI_TOOLS + [{"name": s["name"], "description": s["description"],
                          "inputSchema": s["input_schema"]} for s in SPECS]


def _rpc(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


@router.post("/workspaces/{ws_id}/mcp/omni")
async def omni(ws_id: str, request: Request, session: Session = Depends(get_session)):
    """Streamable-HTTP MCP endpoint (JSON responses). Point Claude Code or any MCP client at
    /api/workspaces/<id>/mcp/omni. Writes need the X-Filament-Autonomy header (default L1)."""
    import json

    get_or_404(session, Workspace, ws_id)
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse(_rpc(None, error={"code": -32700, "message": "Parse error"}))
    autonomy = request.headers.get("X-Filament-Autonomy", "L1").upper()
    if autonomy not in reg.AUTONOMY_ORDER:
        autonomy = "L1"
    batch = payload if isinstance(payload, list) else [payload]
    replies = []
    for msg in batch:
        method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if id_ is None:  # notifications (e.g. notifications/initialized) need no reply
            continue
        if method == "initialize":
            replies.append(_rpc(id_, {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "filament-gateway", "version": __version__},
                "instructions": "Use discover_servers, then list_tools, get_tool_schema and "
                                "invoke_tool. Ask for only the fields you need. For questions "
                                "about code, use code_query / code_explain / code_path / "
                                "code_impact before reading or grepping files."}))
        elif method == "ping":
            replies.append(_rpc(id_, {}))
        elif method == "tools/list":
            replies.append(_rpc(id_, {"tools": _omni_tools()}))
        elif method == "tools/call":
            replies.append(_rpc(id_, _omni_call(session, ws_id, params.get("name"),
                                                params.get("arguments") or {}, autonomy)))
        else:
            replies.append(_rpc(id_, error={"code": -32601, "message": f"Unknown method {method}"}))
    session.commit()
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(payload, list) else replies[0])


def _omni_call(session: Session, ws_id: str, name: str | None, args: dict, autonomy: str) -> dict:
    import json

    def ok(data):
        return {"content": [{"type": "text", "text": json.dumps(data, default=str)}],
                "structuredContent": data if isinstance(data, dict) else {"result": data},
                "isError": False}

    try:
        if name == "discover_servers":
            return ok({"servers": reg.discover_servers(session, ws_id, args.get("query", ""))})
        if name == "list_tools":
            srv, _ = reg.find(session, ws_id, args.get("server", ""))
            return ok({"server": srv.name, "tools": reg.list_tools(session, srv)})
        if name == "get_tool_schema":
            srv, entry = reg.find(session, ws_id, args.get("server", ""), args.get("tool", ""))
            return ok({"server": srv.name, "tool": entry.name, "description": entry.description,
                       "write": entry.write, "input_schema": entry.input_schema})
        if name == "invoke_tool":
            srv, entry = reg.find(session, ws_id, args.get("server", ""), args.get("tool", ""))
            return ok(reg.invoke(session, ws_id, srv, entry, args.get("arguments") or {},
                                 actor="omni", autonomy=autonomy, fields=args.get("fields")))
        from filament.repos import code_tools

        if name in code_tools.NAMES:
            try:
                return ok(code_tools.run(session, ws_id, name, args))
            except code_tools.CodeToolError as exc:
                raise reg.GatewayError(str(exc)) from exc
        raise reg.GatewayError(f"Unknown tool {name}")
    except reg.GatewayError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
