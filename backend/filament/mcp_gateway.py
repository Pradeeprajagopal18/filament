"""MCP tool gateway: discover and call tools on configured MCP servers.

Agents never talk to MCP servers directly. The gateway resolves secret references from the
runner's environment, filters tools to the configured allow-list, enforces write rules by
autonomy level, and caps result size so one tool call cannot flood an agent's context.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from filament.models import McpServer, Project

SECRET_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
SECRET_KEY = re.compile(r"token|secret|password|passwd|api[_-]?key|authorization|credential",
                        re.IGNORECASE)
AUTONOMY_ORDER = ["L0", "L1", "L2", "L3", "L4"]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class McpConfigError(ValueError):
    pass


class McpCallError(RuntimeError):
    pass


def validate_config(name: str, transport: str, command: str | None, url: str | None,
                    env: dict, headers: dict, min_autonomy: str) -> None:
    if not NAME_RE.match(name):
        raise McpConfigError("name must be lowercase letters, digits, '-' or '_' (max 40)")
    if transport == "stdio" and not command:
        raise McpConfigError("stdio servers need a command")
    if transport == "http" and not (url or "").startswith(("http://", "https://")):
        raise McpConfigError("http servers need an http(s) url")
    if transport not in {"stdio", "http"}:
        raise McpConfigError("transport must be 'stdio' or 'http'")
    if min_autonomy not in AUTONOMY_ORDER:
        raise McpConfigError(f"min_autonomy_for_writes must be one of {AUTONOMY_ORDER}")
    for kind, values in (("env", env), ("headers", headers)):
        for key, value in values.items():
            if SECRET_KEY.search(key) and not SECRET_REF.match(str(value)):
                raise McpConfigError(
                    f"{kind}.{key} looks like a secret: store it as a reference such as "
                    f"${{env:{re.sub(r'[^A-Za-z0-9_]', '_', key).upper()}}}, not the value")


def _resolve(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        m = SECRET_REF.match(str(value))
        if m:
            if m.group(1) not in os.environ:
                raise McpCallError(f"Secret {m.group(1)} is not set in this runner's environment")
            out[key] = os.environ[m.group(1)]
        else:
            out[key] = str(value)
    return out


# -- transport ------------------------------------------------------------------------------
async def _with_session(server: McpServer, fn):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if server.transport == "stdio":
        env = {**os.environ, **_resolve(server.env or {})}
        params = StdioServerParameters(command=server.command, args=list(server.args or []),
                                       env=env)
        transport = stdio_client(params)
    else:
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        client = create_mcp_http_client(headers=_resolve(server.headers or {}))
        transport = streamable_http_client(server.url, http_client=client)
    with anyio.fail_after(server.timeout_s):
        async with transport as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def discover(server: McpServer) -> list[dict]:
    async def run(session):
        result = await session.list_tools()
        return [{"name": t.name, "description": t.description or "",
                 "input_schema": t.input_schema or {"type": "object", "properties": {}}}
                for t in result.tools]

    return anyio.run(_with_session, server, run)


def call(server: McpServer, tool: str, arguments: dict, max_chars: int) -> dict:
    async def run(session):
        return await session.call_tool(tool, arguments)

    try:
        result = anyio.run(_with_session, server, run)
    except McpCallError:
        raise
    except Exception as exc:  # transport/timeouts: report to the agent, don't crash the run
        raise McpCallError(f"{type(exc).__name__}: {exc}") from exc
    texts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    text = "\n".join(t for t in texts if t)
    truncated = len(text) > max_chars
    out = {"server": server.name, "tool": tool,
           "is_error": bool(getattr(result, "is_error", False)), "content": text[:max_chars]}
    structured = getattr(result, "structured_content", None)
    if structured is not None and len(str(structured)) <= max_chars:
        out["structured"] = structured
    if truncated:
        out["truncated"] = f"{len(text) - max_chars} characters omitted"
    return out


def test_server(session: Session, server: McpServer) -> McpServer:
    """Connect, list tools and cache them on the server record."""
    server.last_tested_at = datetime.now(UTC)
    try:
        server.tools_cache = discover(server)
        server.last_test_ok, server.last_test_error = True, None
    except BaseException as exc:  # noqa: BLE001 - include ExceptionGroup from task groups
        server.last_test_ok = False
        server.last_test_error = f"{type(exc).__name__}: {exc}"[:2000]
    session.commit()
    return server


# -- what an agent sees ------------------------------------------------------------------------
@dataclass
class McpTool:
    server: McpServer
    tool: dict
    agent_name: str  # the name exposed to the model

    @property
    def writes(self) -> bool:
        return self.tool["name"] in (self.server.write_tools or [])


def tool_name(server: str, tool: str) -> str:
    """Anthropic tool names allow [a-zA-Z0-9_-]{1,64}."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def visible_servers(session: Session, project: Project) -> list[McpServer]:
    scopes = [McpServer.scope == "workspace",
              (McpServer.scope == "project") & (McpServer.scope_ref == project.id)]
    if project.team_id:
        scopes.append((McpServer.scope == "team") & (McpServer.scope_ref == project.team_id))
    stmt = select(McpServer).where(McpServer.workspace_id == project.workspace_id,
                                   McpServer.enabled.is_(True), or_(*scopes))
    servers = session.scalars(stmt).all()
    # The narrowest scope wins when two servers share a name.
    rank = {"project": 0, "team": 1, "workspace": 2}
    chosen: dict[str, McpServer] = {}
    for s in sorted(servers, key=lambda s: rank.get(s.scope, 3)):
        chosen.setdefault(s.name, s)
    return list(chosen.values())


def tools_for(session: Session, project: Project) -> list[McpTool]:
    out = []
    for server in visible_servers(session, project):
        allowed = set(server.allowed_tools or [])
        for t in server.tools_cache or []:
            if allowed and t["name"] not in allowed:
                continue
            out.append(McpTool(server, t, tool_name(server.name, t["name"])))
    return out


def autonomy_allows_write(autonomy: str, required: str) -> bool:
    return AUTONOMY_ORDER.index(autonomy) >= AUTONOMY_ORDER.index(required)
