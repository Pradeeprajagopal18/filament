"""A tiny MCP server used by the tests (stdio transport)."""

import os

from mcp.server.mcpserver import MCPServer

srv = MCPServer("demo")


@srv.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@srv.tool()
def whoami() -> str:
    """Return the token the server received (proves secret references resolve)."""
    return os.environ.get("DEMO_TOKEN", "missing")


@srv.tool()
def create_ticket(title: str) -> str:
    """Create a ticket in the tracker (a write)."""
    return f"created: {title}"


@srv.tool()
def big_report() -> str:
    """Return a very large result."""
    return "x" * 50_000


if __name__ == "__main__":
    srv.run()
