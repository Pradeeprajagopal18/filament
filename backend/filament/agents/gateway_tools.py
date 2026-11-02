"""Progressive MCP discovery for agents (the Omni MCP pattern).

When a project can reach more tools than fit comfortably in context, agents get four small
tools instead of every tool definition: find a server, list its tools, read one schema, invoke.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from filament import mcp_registry as reg

if TYPE_CHECKING:
    from filament.agents.base import Agent

DISCOVERY_SPECS = [
    {"name": "gateway_discover_servers",
     "description": "Find MCP servers (internal APIs and third-party tools) relevant to what you "
                    "need. Returns server names, what they do and example tools.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]}},
    {"name": "gateway_list_tools",
     "description": "List the tools on one MCP server (names and one-line descriptions).",
     "input_schema": {"type": "object", "properties": {"server": {"type": "string"}},
                      "required": ["server"]}},
    {"name": "gateway_get_schema",
     "description": "Get the full input schema of one tool before calling it.",
     "input_schema": {"type": "object", "properties": {"server": {"type": "string"},
                                                       "tool": {"type": "string"}},
                      "required": ["server", "tool"]}},
    {"name": "gateway_invoke",
     "description": "Call a tool. Pass `fields` (dotted paths such as id or items.sku) to get "
                    "back only what you need. Write tools follow the run's autonomy level.",
     "input_schema": {"type": "object", "properties": {
         "server": {"type": "string"}, "tool": {"type": "string"},
         "arguments": {"type": "object"},
         "fields": {"type": "array", "items": {"type": "string"}}},
         "required": ["server", "tool"]}},
]


def handle(agent: Agent, name: str, inp: dict) -> dict:
    from filament.agents.tools import ToolError

    ws = agent.project.workspace_id
    try:
        if name == "gateway_discover_servers":
            return {"servers": reg.discover_servers(agent.session, ws, inp.get("query", ""),
                                                    agent.project, agent.name)}
        if name == "gateway_list_tools":
            server, _ = reg.find(agent.session, ws, inp["server"], project=agent.project)
            return {"server": server.name, "tools": reg.list_tools(agent.session, server,
                                                                   agent.name)}
        server, entry = reg.find(agent.session, ws, inp["server"], inp["tool"],
                                 project=agent.project)
        if not entry.enabled:
            raise ToolError(f"{entry.name} is not enabled in the gateway registry")
        if name == "gateway_get_schema":
            return {"server": server.name, "tool": entry.name, "description": entry.description,
                    "write": entry.write, "input_schema": entry.input_schema}
        if name == "gateway_invoke":
            if entry.write and not agent.persists:
                return {"saved": False, "reason": "Autonomy L0 (suggest only): external write "
                        "tools are not executed. Describe the change instead."}
            return agent.gateway_invoke(server, entry, inp.get("arguments") or {},
                                        inp.get("fields"))
    except reg.GatewayError as exc:
        raise ToolError(str(exc)) from exc
    raise ToolError(f"Unknown gateway tool {name}")
