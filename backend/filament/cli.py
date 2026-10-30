"""`fil` — the Filament command line. Every capability in the UI is available here.

Server commands talk to the API at $FIL_API (default http://localhost:8000/api).
`fil ready` runs locally and needs no server.
"""

import json
import os
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Filament CLI", no_args_is_help=True)
ready_app = typer.Typer(help="Launchpad: repo readiness checks", no_args_is_help=True)
ws_app = typer.Typer(help="Workspaces", no_args_is_help=True)
project_app = typer.Typer(help="Projects", no_args_is_help=True)
memory_app = typer.Typer(help="Shared memory", no_args_is_help=True)
graph_app = typer.Typer(help="Project graph", no_args_is_help=True)
app.add_typer(ready_app, name="ready")
app.add_typer(ws_app, name="ws")
app.add_typer(project_app, name="project")
app.add_typer(memory_app, name="memory")
app.add_typer(graph_app, name="graph")
mcp_app = typer.Typer(help="MCP servers available to agents", no_args_is_help=True)
runner_app = typer.Typer(help="Where agents run", no_args_is_help=True)
agent_app = typer.Typer(help="Per-agent defaults (runner, model, budgets)", no_args_is_help=True)
config_app = typer.Typer(help="Config as code (YAML)", no_args_is_help=True)
app.add_typer(mcp_app, name="mcp")
app.add_typer(runner_app, name="runner")
app.add_typer(agent_app, name="agent")
app.add_typer(config_app, name="config")

console = Console()
API = os.environ.get("FIL_API", "http://localhost:8000/api")


def _ws(ws: str | None) -> str:
    ws = ws or os.environ.get("FIL_WORKSPACE")
    if not ws:
        raise typer.BadParameter("Pass --ws or set FIL_WORKSPACE")
    return ws


def _call(method: str, path: str, **kwargs):
    try:
        resp = httpx.request(method, f"{API}{path}", timeout=300, **kwargs)
    except httpx.ConnectError:
        console.print(f"[red]Cannot reach Filament API at {API}. Is the server running?[/red]")
        raise typer.Exit(1) from None
    if resp.status_code >= 400:
        console.print(f"[red]{resp.status_code}: {resp.text}[/red]")
        raise typer.Exit(1)
    return resp.json()


# -- Launchpad -------------------------------------------------------------------
@ready_app.command("scan")
def ready_scan(repo: str, as_json: bool = typer.Option(False, "--json")):
    """Grade a local path or git URL for agent readiness."""
    from filament.readiness import scan

    result = scan(repo)
    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        return
    table = Table(title=f"{repo}  —  grade {result.grade} ({result.score}/100)")
    for col in ("", "Check", "Weight", "Detail"):
        table.add_column(col)
    for c in result.checks:
        table.add_row("[green]✔[/green]" if c.passed else "[red]✘[/red]", c.title,
                      str(c.weight), c.detail if c.passed else f"{c.detail} → {c.fix}")
    console.print(table)
    if result.stack:
        console.print(f"Stack: {', '.join(result.stack)}")


@ready_app.command("fix")
def ready_fix(path: Path, write: bool = typer.Option(False, help="Write files into the repo")):
    """Generate starter files for failing checks (AGENTS.md, CODEOWNERS, Makefile, ...)."""
    from filament.readiness import fix_files, scan

    result = scan(str(path))
    files = fix_files(result)
    if not files:
        console.print("[green]Nothing to generate — no fixable checks failing.[/green]")
        return
    for rel, content in files.items():
        target = path / rel
        if write:
            if target.exists():
                console.print(f"skip {rel} (exists)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            console.print(f"[green]wrote[/green] {rel}")
        else:
            console.rule(rel)
            console.print(content)
    if not write:
        console.print("\nRe-run with --write to create these files.")


# -- workspaces & projects ------------------------------------------------------------
@ws_app.command("create")
def ws_create(name: str):
    ws = _call("POST", "/workspaces", json={"name": name})
    console.print(f"Created workspace [bold]{ws['name']}[/bold] id={ws['id']}")
    console.print(f"Tip: export FIL_WORKSPACE={ws['id']}")


@ws_app.command("list")
def ws_list():
    for ws in _call("GET", "/workspaces"):
        console.print(f"{ws['id']}  {ws['name']}")


@project_app.command("create")
def project_create(name: str, description: str = "", ws: str | None = typer.Option(None)):
    p = _call("POST", f"/workspaces/{_ws(ws)}/projects",
              json={"name": name, "description": description})
    console.print(f"Created project [bold]{p['name']}[/bold] id={p['id']}")


@project_app.command("list")
def project_list(ws: str | None = typer.Option(None)):
    for p in _call("GET", f"/workspaces/{_ws(ws)}/projects"):
        console.print(f"{p['id']}  {p['name']}")


# -- agents ---------------------------------------------------------------------------
@app.command("run")
def run(
    agent: str = typer.Argument(..., help="orchestrator | requirements | flow"),
    project: str = typer.Option(..., "--project", "-p"),
    idea: str | None = typer.Option(None, help="Idea text (requirements agent)"),
    from_file: Path | None = typer.Option(None, "--from", help="Read the idea from a file"),
    autonomy: str = typer.Option("L1", help="L0 suggest .. L4 autonomous"),
    goal: str | None = typer.Option(None),
    trace: bool = typer.Option(False, help="Print every model turn and tool call"),
    runner: str | None = typer.Option(None, help="Override the agent's runner ('inline' = here)"),
    model: str | None = typer.Option(None, help="Override the agent's model"),
    max_turns: int | None = typer.Option(None, help="Override the agent's turn limit"),
):
    """Run an agent against a project."""
    payload: dict = {}
    if from_file:
        payload["idea"] = from_file.read_text()
    elif idea:
        payload["idea"] = idea
    body = {"agent": agent, "goal": goal or f"{agent} run", "input": payload,
            "autonomy": autonomy, "requested_by": os.environ.get("USER", "cli"),
            "runner": runner, "model": model, "max_turns": max_turns}
    r = _call("POST", f"/projects/{project}/runs", json=body)
    colour = "green" if r["status"] == "succeeded" else "red"
    console.print(f"Run {r['id']}: [{colour}]{r['status']}[/{colour}]  model={r['model']}  "
                  f"turns={r['turns']}  tokens in/out={r['input_tokens']}/{r['output_tokens']}")
    if r["status"] == "queued":
        rn = r["config"]["runner"]
        console.print(f"Queued on runner [bold]{rn}[/bold]. Start a worker with "
                      f"`fil runner start {rn}`; follow with `fil trace {r['id']}`.")
        return
    if r["error"]:
        console.print(f"[red]{r['error']}[/red]")
    if trace:
        _print_trace(r["trace"])
    out = r["output"]
    if out.get("summary"):
        console.print(f"\n[bold]Summary:[/bold] {out['summary']}")
    for a in out.get("created", []):
        console.print(f"  + {a['type']:<6} {a['id']}  {a['title']}")
    for child in out.get("child_run_ids", []):
        console.print(f"  ↳ child run {child}")
    for q in out.get("questions", []):
        console.print(f"  ? {q}")


def _print_trace(trace: list[dict]) -> None:
    for e in trace:
        if e["type"] == "model":
            tools = ", ".join(e["tool_calls"]) or "—"
            text = f"  {e['text'][:120]}" if e.get("text") else ""
            console.print(f"[dim]model[/dim] → {tools}{text}")
        elif e["type"] == "tool":
            mark = "[red]✘[/red]" if e["error"] else "[green]✔[/green]"
            console.print(f"  {mark} {e['tool']}  [dim]{json.dumps(e['input'])[:100]}[/dim]")
        else:
            console.print(f"[dim]stop: {e['reason']}[/dim]")


@app.command("trace")
def show_trace(run_id: str):
    """Show the step-by-step log of a run."""
    r = _call("GET", f"/runs/{run_id}")
    console.print(f"{r['agent']} · {r['status']} · {r['turns']} turns")
    _print_trace(r["trace"])


@app.command("approve")
def approve(artifact_id: str, reject: bool = False, comment: str = ""):
    """Approve (or --reject) an artifact waiting at a human gate."""
    a = _call("POST", f"/artifacts/{artifact_id}/approve",
              json={"decision": "rejected" if reject else "approved",
                    "decided_by": os.environ.get("USER", "cli"), "comment": comment})
    console.print(f"{a['type']} '{a['title']}' → {a['status']}")


@app.command("ask")
def ask(question: str, ws: str | None = typer.Option(None), project: str | None = None):
    """Ask Filament a question; answers cite memory and artifacts."""
    params = {"project_id": project} if project else None
    r = _call("POST", f"/workspaces/{_ws(ws)}/ask", json={"question": question}, params=params)
    console.print(r["answer"])
    if r["citations"]:
        console.print(f"[dim]Sources: {', '.join(r['citations'])}[/dim]")


@app.command("usage")
def usage(ws: str | None = typer.Option(None), by: str = "agent"):
    """Token and cost usage grouped by agent, project, model, person or status."""
    rows = _call("GET", f"/workspaces/{_ws(ws)}/usage", params={"by": by})
    table = Table(title=f"Usage by {by}")
    for col in (by, "Runs", "Input tokens", "Output tokens", "Cost (USD)"):
        table.add_column(col)
    for r in rows:
        cost = "—" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
        table.add_row(r["key"], str(r["runs"]), f"{r['input_tokens']:,}",
                      f"{r['output_tokens']:,}", cost)
    console.print(table)


@app.command("gaps")
def gaps(ws: str | None = typer.Option(None), top: int = 10):
    """What people and agents couldn't find: clustered unanswered questions and misses."""
    for g in _call("GET", f"/workspaces/{_ws(ws)}/gaps")[:top]:
        console.print(f"[bold]{g['topic']}[/bold] ×{g['count']}  e.g. {g['examples'][0]}")


@memory_app.command("add")
def memory_add(content: str, scope: str = "org", scope_ref: str | None = None,
               kind: str = "fact", ws: str | None = typer.Option(None)):
    m = _call("POST", f"/workspaces/{_ws(ws)}/memory",
              json={"scope": scope, "scope_ref": scope_ref, "kind": kind, "content": content})
    console.print(f"Saved {m['kind']} ({m['scope']}, {m['status']}) id={m['id']}")


@memory_app.command("list")
def memory_list(ws: str | None = typer.Option(None), status: str | None = None):
    params = {"status": status} if status else None
    for m in _call("GET", f"/workspaces/{_ws(ws)}/memory", params=params):
        console.print(f"{m['id']}  [{m['scope']}/{m['kind']}/{m['status']}]  {m['content']}")


@memory_app.command("confirm")
def memory_confirm(entry_id: str):
    m = _call("POST", f"/memory/{entry_id}/confirm")
    console.print(f"Confirmed: {m['content']}")


@graph_app.command("impact")
def graph_impact(artifact_id: str, hops: int = 3):
    """What could a change to this artifact affect?"""
    r = _call("GET", f"/artifacts/{artifact_id}/impact", params={"hops": hops})
    for a in r["affected"]:
        console.print(f"{a['id']}  {a['type']:<10} {a['status']:<10} {a['title']}")
    if not r["affected"]:
        console.print("No linked artifacts.")


# -- MCP servers ---------------------------------------------------------------------------
def _kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"Expected KEY=VALUE, got '{p}'")
        k, v = p.split("=", 1)
        out[k] = v
    return out


@mcp_app.command("list")
def mcp_list(ws: str | None = typer.Option(None)):
    table = Table(title="MCP servers")
    for col in ("Name", "Scope", "Transport", "Enabled", "Tools", "Writes need", "Last test"):
        table.add_column(col)
    for m in _call("GET", f"/workspaces/{_ws(ws)}/mcp"):
        tools = m["allowed_tools"] or [t["name"] for t in m["tools_cache"]]
        status = {True: "[green]ok[/green]", False: "[red]failed[/red]"}.get(m["last_test_ok"],
                                                                              "—")
        table.add_row(m["name"], m["scope"], m["transport"], "yes" if m["enabled"] else "no",
                      ", ".join(tools) or "—", m["min_autonomy_for_writes"], status)
    console.print(table)


@mcp_app.command("add")
def mcp_add(
    name: str,
    command: str | None = typer.Option(None, help="Executable for a stdio server"),
    arg: list[str] = typer.Option([], "--arg", help="Argument (repeatable)"),
    url: str | None = typer.Option(None, help="URL for an http server"),
    env: list[str] = typer.Option([], "--env", help="KEY=VALUE; secrets as ${env:NAME}"),
    header: list[str] = typer.Option([], "--header", help="KEY=VALUE; secrets as ${env:NAME}"),
    scope: str = typer.Option("workspace", help="workspace | team | project"),
    scope_ref: str | None = typer.Option(None, help="Team or project id for narrower scopes"),
    allow: list[str] = typer.Option([], "--allow", help="Only expose these tools (repeatable)"),
    write: list[str] = typer.Option([], "--write", help="Tools that change things (repeatable)"),
    min_autonomy: str = typer.Option("L2", help="Autonomy needed for write tools"),
    ws: str | None = typer.Option(None),
):
    """Add an MCP server and test it (lists its tools)."""
    body = {"name": name, "transport": "http" if url else "stdio", "command": command,
            "args": arg, "url": url, "env": _kv(env), "headers": _kv(header), "scope": scope,
            "scope_ref": scope_ref, "allowed_tools": allow, "write_tools": write,
            "min_autonomy_for_writes": min_autonomy}
    m = _call("POST", f"/workspaces/{_ws(ws)}/mcp", params={"test": True}, json=body)
    _print_test(m)


def _print_test(m: dict) -> None:
    if m["last_test_ok"]:
        names = ", ".join(t["name"] for t in m["tools_cache"])
        console.print(f"[green]✔[/green] {m['name']}: {len(m['tools_cache'])} tools ({names})")
    else:
        console.print(f"[red]✘[/red] {m['name']}: {m['last_test_error']}")


def _mcp_id(name: str, ws: str | None) -> str:
    for m in _call("GET", f"/workspaces/{_ws(ws)}/mcp"):
        if m["name"] == name or m["id"] == name:
            return m["id"]
    console.print(f"[red]No MCP server '{name}'[/red]")
    raise typer.Exit(1)


@mcp_app.command("test")
def mcp_test(name: str, ws: str | None = typer.Option(None)):
    """Reconnect and refresh the tool list agents will see."""
    _print_test(_call("POST", f"/mcp/{_mcp_id(name, ws)}/test"))


@mcp_app.command("update")
def mcp_update(
    name: str,
    enable: bool | None = typer.Option(None, "--enable/--disable"),
    allow: list[str] = typer.Option([], "--allow"),
    write: list[str] = typer.Option([], "--write"),
    min_autonomy: str | None = typer.Option(None),
    ws: str | None = typer.Option(None),
):
    body = {"enabled": enable, "allowed_tools": allow or None, "write_tools": write or None,
            "min_autonomy_for_writes": min_autonomy}
    m = _call("PATCH", f"/mcp/{_mcp_id(name, ws)}", json={k: v for k, v in body.items()
                                                           if v is not None})
    console.print(f"Updated {m['name']} (enabled={m['enabled']})")


@mcp_app.command("remove")
def mcp_remove(name: str, ws: str | None = typer.Option(None)):
    sid = _mcp_id(name, ws)
    httpx.delete(f"{API}/mcp/{sid}", timeout=30).raise_for_status()
    console.print(f"Removed {name}")


# -- runners and agents ------------------------------------------------------------------------
@runner_app.command("list")
def runner_list(ws: str | None = typer.Option(None)):
    table = Table(title="Runners")
    for col in ("Name", "Kind", "Default", "Queued", "Last seen", "Labels"):
        table.add_column(col)
    for r in _call("GET", f"/workspaces/{_ws(ws)}/runners"):
        table.add_row(r["name"], r["kind"], "★" if r["is_default"] else "", str(r["queued"]),
                      r["last_seen_at"] or "never", ", ".join(r["labels"]))
    console.print(table)
    console.print("[dim]Runs with no runner configured execute inline in the API process.[/dim]")


@runner_app.command("add")
def runner_add(name: str, kind: str = typer.Option("worker"),
               default: bool = typer.Option(False, "--default"),
               label: list[str] = typer.Option([], "--label"),
               ws: str | None = typer.Option(None)):
    r = _call("POST", f"/workspaces/{_ws(ws)}/runners",
              json={"name": name, "kind": kind, "is_default": default, "labels": label})
    console.print(f"Added runner {r['name']} ({r['kind']}){' as default' if default else ''}")


@runner_app.command("start")
def runner_start(name: str, ws: str | None = typer.Option(None),
                 worker: str | None = typer.Option(None, help="Worker name (default: hostname)"),
                 once: bool = typer.Option(False, help="Process one run, then exit")):
    """Run queued agent runs for this runner on THIS machine (needs FILAMENT_DATABASE_URL)."""
    import socket

    from filament.db import SessionLocal, init_db
    from filament.runtime import work_forever

    init_db()
    worker = worker or socket.gethostname()
    console.print(f"Worker [bold]{worker}[/bold] serving runner [bold]{name}[/bold]. "
                  "Ctrl+C to stop.")
    work_forever(SessionLocal, name, _ws(ws), worker, stop_after=1 if once else None)


@agent_app.command("list")
def agent_list(ws: str | None = typer.Option(None)):
    table = Table(title="Agent defaults (★ = set for this workspace)")
    for col in ("Agent", "Runner", "Model", "Max turns", "Token budget", "Context budget"):
        table.add_column(col)
    for a in _call("GET", f"/workspaces/{_ws(ws)}/agent-settings"):
        cells = [f"{a[k]} ★" if k in a["overridden"] else str(a[k])
                 for k in ("runner", "model", "max_turns", "token_budget", "context_budget")]
        table.add_row(a["agent"], *cells)
    console.print(table)


@agent_app.command("set")
def agent_set(agent: str, runner: str | None = None, model: str | None = None,
              max_turns: int | None = None, token_budget: int | None = None,
              context_budget: int | None = None, ws: str | None = typer.Option(None)):
    """Set an agent's defaults. Options you leave out fall back to workspace defaults."""
    a = _call("PUT", f"/workspaces/{_ws(ws)}/agent-settings/{agent}", json={
        "runner": runner, "model": model, "max_turns": max_turns,
        "token_budget": token_budget, "context_budget": context_budget})
    console.print(f"{a['agent']}: runner={a['runner']} model={a['model']} "
                  f"max_turns={a['max_turns']} token_budget={a['token_budget']}")


@config_app.command("export")
def config_export(ws: str | None = typer.Option(None)):
    """Print MCP servers, runners and agent defaults as YAML."""
    resp = httpx.get(f"{API}/workspaces/{_ws(ws)}/config", timeout=30)
    resp.raise_for_status()
    print(resp.text, end="")


@config_app.command("apply")
def config_apply(file: Path = typer.Option(..., "-f", "--file"),
                 ws: str | None = typer.Option(None)):
    """Create or update everything in a YAML file (all-or-nothing)."""
    r = _call("POST", f"/workspaces/{_ws(ws)}/config", content=file.read_text(),
              headers={"Content-Type": "text/plain"})
    console.print(f"Applied: {r['applied']}")


if __name__ == "__main__":
    app()
