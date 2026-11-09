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
repo_app = typer.Typer(help="Launchpad repos: type, readiness, API docs, UI, components",
                       no_args_is_help=True)
app.add_typer(repo_app, name="repo")
code_app = typer.Typer(help="Code graph: explain, path, query, change and PR impact",
                       no_args_is_help=True)
app.add_typer(code_app, name="code")

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
def ready_scan(repo: str, as_json: bool = typer.Option(False, "--json"),
               all_checks: bool = typer.Option(False, "--all", help="Include skipped checks")):
    """Grade a local path or git URL for agent readiness, using checks for its repo type."""
    from filament.readiness import scan

    result = scan(repo)
    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        return
    kinds = ", ".join(result.kinds) or "unknown"
    table = Table(title=f"{repo}  —  {kinds}  —  grade {result.grade} ({result.score}/100)")
    for col in ("", "Check", "Type", "Weight", "Detail"):
        table.add_column(col)
    for c in result.checks:
        if c.status == "skip" and not all_checks:
            continue
        mark = {"pass": "[green]✔[/green]", "fail": "[red]✘[/red]"}.get(c.status, "[dim]–[/dim]")
        detail = c.detail if c.status != "fail" else f"{c.detail} → {c.fix}"
        if c.generate:
            detail += " [dim](Filament can generate this)[/dim]"
        table.add_row(mark, c.title, c.kind, str(c.weight), detail)
    console.print(table)
    frameworks = ", ".join(result.profile.get("frameworks", []))
    if frameworks:
        console.print(f"Frameworks: {frameworks}")


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



# -- Launchpad repos -------------------------------------------------------------
def _repo_line(r: dict) -> str:
    kinds = ", ".join(r["kinds"])
    return f"[bold]{r['name']}[/bold] ({kinds}) grade {r['grade']} · {r['score']:.0f}"


@repo_app.command("add")
def repo_add(repo: str, ws: str | None = typer.Option(None),
             project: str | None = typer.Option(None, help="Attach to an existing project")):
    """Onboard a repo (local path or git URL): detect its type and analyze everything."""
    r = _call("POST", f"/workspaces/{_ws(ws)}/repos", json={"repo": repo, "project_id": project})
    console.print(f"{_repo_line(r)}  id={r['id']}")
    _repo_summary(r)


def _repo_summary(r: dict) -> None:
    a = r["analysis"]
    fails = [c for c in a["readiness"]["checks"] if c["status"] == "fail"]
    for c in fails:
        tag = "  [cyan](Filament can create this)[/cyan]" if c.get("generate") else ""
        console.print(f"  [red]✗[/red] {c['title']} — {c['detail']}{tag}")
    api = a["api"]
    if api["endpoints"]:
        console.print(f"  API: {len(api['endpoints'])} endpoints ({api['source']})")
    if a["ui"]["pages"]:
        console.print(f"  UI: {len(a['ui']['pages'])} pages")
    deps = [n["label"] for n in a["components"]["nodes"] if n["category"] != "self"]
    if deps:
        console.print(f"  Connects to: {', '.join(deps[:12])}")


@repo_app.command("list")
def repo_list(ws: str | None = typer.Option(None)):
    for r in _call("GET", f"/workspaces/{_ws(ws)}/repos"):
        console.print(f"{r['id']}  {_repo_line(r)}  {r['ref']}")


@repo_app.command("show")
def repo_show(repo_id: str, section: str = typer.Option(
        "summary", help="summary | api | ui | components")):
    r = _call("GET", f"/repos/{repo_id}")
    a = r["analysis"]
    console.print(_repo_line(r))
    if section == "api":
        table = Table(title=f"API ({a['api']['source']})")
        for col in ("Method", "Path", "Handler", "Where"):
            table.add_column(col)
        for e in a["api"]["endpoints"]:
            where = f"{e['file']}:{e['line']}" if e.get("line") else (e.get("file") or "")
            table.add_row(e["method"], e["path"], e.get("handler") or "", where)
        console.print(table)
    elif section == "ui":
        for pg in a["ui"]["pages"]:
            kinds = ", ".join(sorted({el["type"] for el in pg["elements"]}))
            console.print(f"  {pg['path']}  → {pg['component']}  [{kinds}]")
    elif section == "components":
        console.print(a["components"]["mermaid"])
    else:
        _repo_summary(r)


@repo_app.command("generate")
def repo_generate(repo_id: str, item: list[str] = typer.Option(
        [], "--item", help="agents_md | overview | architecture | codeowners | api_contract")):
    """Create missing items as drafts in Filament (default: everything a failing check needs)."""
    r = _call("POST", f"/repos/{repo_id}/generate", json={"items": item})
    for gen_id, art in r["generated"].items():
        console.print(f"  [green]✓[/green] {gen_id} → artifact {art} (draft)")
    console.print("Conventions were proposed to project memory for review.")


@repo_app.command("openapi")
def repo_openapi(repo_id: str, out: str | None = typer.Option(None, "-o")):
    """Print or save the OpenAPI document (from the repo's spec or inferred from code)."""
    resp = httpx.get(f"{API}/repos/{repo_id}/openapi", timeout=60)
    if resp.status_code >= 400:
        console.print(f"[red]{resp.status_code}: {resp.text}[/red]")
        raise typer.Exit(1)
    if out:
        with open(out, "w") as fh:
            fh.write(resp.text)
        console.print(f"Wrote {out}")
    else:
        print(resp.text)


@repo_app.command("rescan")
def repo_rescan(repo_id: str | None = typer.Argument(None),
                due: bool = typer.Option(False, "--due", help="Rescan every repo that is due")):
    """Rescan a repo (or all due repos) and show what changed."""
    if due:
        for x in _call("POST", "/repos/scan-due")["scanned"]:
            console.print(f"{x['repo_id']}  {x['status']}  {x.get('summary') or ''}")
        return
    if not repo_id:
        raise typer.BadParameter("Pass a repo id or --due")
    r = _call("POST", f"/repos/{repo_id}/rescan")
    scans = _call("GET", f"/repos/{repo_id}/scans?limit=1")
    console.print(_repo_line(r))
    if scans:
        _print_changes(scans[0]["changes"])


def _print_changes(ch: dict) -> None:
    console.print(f"  {ch.get('summary', '')}")
    for b in ch.get("breaking") or []:
        users = f"  (used by {', '.join(b['consumers'])})" if b.get("consumers") else ""
        console.print(f"  [red]breaking[/red] {b['endpoint']}: {b['change']}{users}")
    for key, label in (("endpoints_added", "+"), ("endpoints_removed", "-")):
        for ep in ch.get(key) or []:
            console.print(f"  {label} {ep}")
    refs = ch.get("references") or {}
    if refs.get("refreshed"):
        console.print(f"  References refreshed: {', '.join(refs['refreshed'])}")
    if refs.get("pending_review"):
        console.print(f"  [yellow]Approved references with updates to review:[/yellow] "
                      f"{', '.join(refs['pending_review'])}")


@repo_app.command("history")
def repo_history(repo_id: str, limit: int = typer.Option(10)):
    for x in _call("GET", f"/repos/{repo_id}/scans?limit={limit}"):
        console.print(f"{x['scanned_at'][:16]}  {x['trigger']:<8} {x['status']:<6} "
                      f"{x['grade']}  {(x['changes'] or {}).get('summary', x.get('error') or '')}")


@repo_app.command("schedule")
def repo_schedule(repo_id: str, hours: int = typer.Argument(..., help="0 = manual only")):
    """How often Filament rescans this repo to keep its references current."""
    r = _call("PATCH", f"/repos/{repo_id}", json={"scan_interval_hours": hours})
    console.print(f"{r['name']}: every {hours}h, next scan {r.get('next_scan_at') or 'manual'}")


@repo_app.command("impact")
def repo_impact(repo_id: str):
    """Who depends on this repo, and through which endpoints."""
    imp = _call("GET", f"/repos/{repo_id}/impact")
    if not imp["dependents"]:
        console.print("No other onboarded repo depends on this one.")
    for d in imp["dependents"]:
        eps = f": {', '.join(d['endpoints'])}" if d["endpoints"] else ""
        console.print(f"  {d['repo']} {d['kind']} it{eps}")
    for d in imp["dependencies"]:
        console.print(f"  → {d['name']} ({d['kind']})")


@repo_app.command("map")
def repo_map(ws: str | None = typer.Option(None),
             mermaid: bool = typer.Option(False, help="Print the Mermaid diagram")):
    """How the workspace's repos connect to each other."""
    m = _call("GET", f"/workspaces/{_ws(ws)}/system-map")
    if mermaid:
        print(m["mermaid"])
        return
    names = {n["id"]: n["label"] for n in m["nodes"]}
    for e in m["edges"]:
        if e["kind"] == "uses":
            continue
        eps = f" ({len(e['endpoints'])} endpoints)" if e["endpoints"] else ""
        console.print(f"  {names[e['source']]} —{e['kind']}→ {names[e['target']]}{eps}")
    shared = [n for n in m["nodes"] if n["kind"] != "repo" and n.get("shared_by", 0) > 1]
    if shared:
        console.print("  Shared: " + ", ".join(f"{n['label']} ({n['shared_by']} repos)"
                                               for n in shared))


@repo_app.command("enrich")
def repo_enrich(repo_id: str):
    """Model pass over the API: summaries, descriptions and extra error codes."""
    r = _call("POST", f"/repos/{repo_id}/enrich-api")
    e = r["analysis"]["api"].get("enrichment", {})
    console.print(f"Enriched {e.get('applied', 0)} endpoints with {e.get('model')}")


# -- MCP gateway: Code Mode ----------------------------------------------------------------
@mcp_app.command("search")
def mcp_search(query: str = typer.Argument("", help="What you need, e.g. 'cart totals'"),
               ws: str | None = typer.Option(None)):
    """Find servers and tools in the gateway registry (disabled ones included, marked)."""
    for s in _call("GET", f"/workspaces/{_ws(ws)}/mcp/registry", params={"q": query}):
        console.print(f"[bold]{s['name']}[/bold] ({s['kind']}) {s['enabled_tools']}/{s['tools']} "
                      f"tools enabled")
        for t in s["matches"]:
            flag = "" if t["enabled"] else " [dim](disabled)[/dim]"
            w = " [yellow]write[/yellow]" if t["write"] else ""
            console.print(f"  {t['name']}{w}{flag} — {t['description'][:90]}")


@mcp_app.command("tools")
def mcp_tools(server: str, ws: str | None = typer.Option(None)):
    """List a server's tools with registry status."""
    srv = next((s for s in _call("GET", f"/workspaces/{_ws(ws)}/mcp/registry")
                if s["name"] == server), None)
    if srv is None:
        console.print(f"[red]No server {server}[/red]")
        raise typer.Exit(1)
    table = Table(title=f"{server} ({srv['kind']})")
    for col in ("Tool", "Status", "Enabled", "Write", "Version", "Limits"):
        table.add_column(col)
    for t in _call("GET", f"/mcp/{srv['id']}/tools"):
        limits = ", ".join(filter(None, [f"{t['rate_limit_per_min']}/min" if
                                         t["rate_limit_per_min"] else "",
                                         "agents: " + ",".join(t["allowed_agents"])
                                         if t["allowed_agents"] else ""]))
        table.add_row(t["name"], t["status"], "yes" if t["enabled"] else "no",
                      "yes" if t["write"] else "", str(t["version"]), limits or "—")
    console.print(table)


@mcp_app.command("schema")
def mcp_schema(server: str, tool: str, ws: str | None = typer.Option(None)):
    import json as _json

    print(_json.dumps(_call("GET", f"/workspaces/{_ws(ws)}/mcp/schema",
                            params={"server": server, "tool": tool}), indent=2))


@mcp_app.command("call")
def mcp_call_cmd(
    server: str, tool: str,
    arg: list[str] = typer.Option([], "--arg", help="key=value (repeatable; JSON values ok)"),
    body: str | None = typer.Option(None, help="JSON request body (generated API tools)"),
    fields: str | None = typer.Option(None, help="Comma-separated dotted fields to keep"),
    out: str | None = typer.Option(None, "-o", help="Write the result to a file"),
    autonomy: str = typer.Option("L1", help="Writes need the server's minimum autonomy"),
    ws: str | None = typer.Option(None),
):
    """Call a tool through the gateway (Code Mode): no MCP server installs, results can go to
    a file and be trimmed to the fields you need."""
    import json as _json

    args: dict = {}
    for a in arg:
        k, _, v = a.partition("=")
        try:
            args[k] = _json.loads(v)
        except ValueError:
            args[k] = v
    if body:
        args["body"] = _json.loads(body)
    res = _call("POST", f"/workspaces/{_ws(ws)}/mcp/invoke",
                json={"server": server, "tool": tool, "arguments": args, "autonomy": autonomy,
                      "fields": [f.strip() for f in fields.split(",")] if fields else None,
                      "actor": "cli"})
    text = _json.dumps(res.get("result", res.get("content")), indent=2, default=str)
    if out:
        with open(out, "w") as fh:
            fh.write(text)
        console.print(f"Wrote {len(text)} bytes to {out}" + (" (simulated)" if
                                                              res.get("simulated") else ""))
    else:
        print(text)


@mcp_app.command("enable")
def mcp_enable(server: str, tool: list[str] = typer.Argument(...),
               off: bool = typer.Option(False, "--off", help="Disable instead"),
               ws: str | None = typer.Option(None)):
    """Enable (or disable) tools in the registry. Discovered and generated tools start off."""
    srv = next((s for s in _call("GET", f"/workspaces/{_ws(ws)}/mcp/registry")
                if s["name"] == server), None)
    if srv is None:
        console.print(f"[red]No server {server}[/red]")
        raise typer.Exit(1)
    entries = {t["name"]: t for t in _call("GET", f"/mcp/{srv['id']}/tools")}
    for name in tool:
        if name not in entries:
            console.print(f"[red]No tool {name}[/red]")
            continue
        _call("PATCH", f"/mcp-tools/{entries[name]['id']}", json={"enabled": not off})
        console.print(f"{'Disabled' if off else 'Enabled'} {server}.{name}")


@mcp_app.command("metrics")
def mcp_metrics(hours: int = typer.Option(24), ws: str | None = typer.Option(None)):
    m = _call("GET", f"/workspaces/{_ws(ws)}/mcp/metrics", params={"hours": hours})
    console.print(f"{m['calls']} calls · {m['errors']} errors · {m['denied']} denied · "
                  f"{m['bytes_saved']:,} bytes saved by projection · {m['redactions']} redactions")
    table = Table()
    for col in ("Server", "Tool", "Calls", "Errors", "Denied", "p50 ms", "p95 ms", "Actors"):
        table.add_column(col)
    for t in m["tools"]:
        table.add_row(t["server"], t["tool"], str(t["calls"]), str(t["errors"]),
                      str(t["denied"]), str(t["p50_ms"]), str(t["p95_ms"]),
                      ", ".join(t["actors"]))
    console.print(table)


# -- code graph ---------------------------------------------------------------------------------
def _code_get(repo_id: str, path: str, **params):
    resp = httpx.get(f"{API}/repos/{repo_id}/code{path}", params=params, timeout=120)
    data = resp.json()
    if resp.status_code == 409:
        console.print(f"[yellow]{data['detail']}[/yellow]")
        for c in data["candidates"]:
            console.print(f"  {c['id']}  {c['label']}  {c.get('file')}:{c.get('line')}")
        raise typer.Exit(3)
    if resp.status_code == 404:
        console.print(f"[red]{data['detail']}[/red]")
        for s in data.get("suggestions") or []:
            console.print(f"  did you mean {s['label']} ({s['id']})?")
        raise typer.Exit(4)
    if resp.status_code >= 400:
        console.print(f"[red]{resp.status_code}: {data}[/red]")
        raise typer.Exit(1)
    return data


@code_app.command("summary")
def code_summary(repo_id: str):
    s = _call("GET", f"/repos/{repo_id}/code")
    if not s.get("engine") or s.get("error"):
        console.print(s.get("error") or s.get("reason") or "No code graph")
        return
    console.print(f"{s['symbols']} symbols in {s['files']} files · {s['edges']} links "
                  f"({s['extracted_pct']}% read from code, {s['inferred_pct']}% inferred)")
    subs = ", ".join(f"{c['name']} ({c['size']})" for c in s["communities"][:8])
    console.print(f"Subsystems: {subs}")
    console.print("Hotspots: " + ", ".join(f"{h['label']} (fan-in {h['fan_in']})"
                                           for h in s["hotspots"][:5]))
    for cyc in s.get("cycles") or []:
        console.print(f"[yellow]Import cycle:[/yellow] {' → '.join(cyc)}")


@code_app.command("explain")
def code_explain(repo_id: str, name: str):
    """What a symbol calls, who calls it, endpoints it serves and services it reaches."""
    d = _code_get(repo_id, "/explain", name=name)
    s = d["symbol"]
    console.print(f"[bold]{s['label']}[/bold]  {s.get('file')}:{s.get('line')}")
    if d.get("serves_endpoints"):
        console.print("Serves: " + ", ".join(d["serves_endpoints"]))
    for sv in d.get("uses_services") or []:
        how = "directly" if sv["via"] == "direct" else f"via {sv['via']}"
        console.print(f"Uses {sv['label']} ({how})")
    for c in d["calls"]:
        console.print(f"  → {c['node']['label']}  \\[{c['confidence']}]")
    for c in d["called_by"]:
        console.print(f"  ← {c['node']['label']}  \\[{c['confidence']}]")
    for n in d.get("notes") or []:
        console.print(f"  note: {n}")


@code_app.command("path")
def code_path(repo_id: str, a: str, b: str):
    d = _code_get(repo_id, "/path", a=a, b=b)
    if not d["found"]:
        console.print("No path found.")
        raise typer.Exit(4)
    for h in d["hops"]:
        arrow = "→" if h["forward"] else "←"
        hop = f"{h['from']} {arrow}{h['relation']}{arrow} {h['to']}"
        console.print(f"  {hop}  \\[{h['confidence']}]")


@code_app.command("query")
def code_query(repo_id: str, question: str):
    d = _code_get(repo_id, "/query", q=question)
    for n in d["seeds"]:
        console.print(f"  {n['label']}  {n.get('file')}:{n.get('line')}")


@code_app.command("impact")
def code_impact(
    repo_id: str,
    files: list[str] = typer.Argument(None, help="Changed files"),
    git_diff: str | None = typer.Option(None, "--git-diff", help="Diff against a ref, e.g. main"),
    pr: str | None = typer.Option(None, help="GitHub pull request URL"),
):
    """What a change affects: symbols, endpoints and the repos that consume them."""
    import subprocess

    body: dict = {"files": files or []}
    if git_diff:
        body["diff"] = subprocess.run(["git", "diff", "-U0", git_diff], capture_output=True,
                                      text=True, check=True).stdout
    if pr:
        body["pr"] = pr
    r = _call("POST", f"/repos/{repo_id}/code/changes", json=body)
    color = {"high": "red", "medium": "yellow", "low": "green"}[r["risk"]]
    console.print(f"[{color}]{r['risk']} risk[/{color}] · {r['summary']}")
    for ep in r["endpoints"]:
        console.print(f"  endpoint {ep}")
    for c in r["consumers"]:
        console.print(f"  [bold]{c['repo']}[/bold] uses {', '.join(c['endpoints'])}")
    if r["hotspots_touched"]:
        console.print("  touches hotspots: " + ", ".join(r["hotspots_touched"]))


@code_app.command("conflicts")
def code_conflicts(repo_id: str, change: list[str] = typer.Option(
        ..., "--change", help="name=file1,file2 (repeat for each PR or branch)")):
    sets = [{"name": c.split("=", 1)[0], "files": c.split("=", 1)[1].split(",")} for c in change]
    for x in _call("POST", f"/repos/{repo_id}/code/conflicts", json={"changes": sets}):
        what = ", ".join(x["same_symbols"] or x["adjacent_symbols"]) or "same subsystem"
        console.print(f"{x['severity']}: {x['a']} × {x['b']}  {what}")


# -- Delivery pipeline -------------------------------------------------------------------
delivery_app = typer.Typer(help="Delivery: work items through plan, design, build, test, "
                                "review, deploy and operate", no_args_is_help=True)
app.add_typer(delivery_app, name="delivery")
STATE_STYLE = {"approved": "green", "done": "green", "passed": "green", "watching": "green",
               "waiting": "yellow", "needs_info": "yellow", "draft": "yellow", "ready": "cyan",
               "handed_off": "cyan", "running": "cyan", "rolling_out": "cyan",
               "failing": "red", "blocked": "red", "incident": "red", "rolled_back": "red"}


def _stage_cell(s: dict) -> str:
    style = STATE_STYLE.get(s["state"], "dim")
    return f"[{style}]{s['state'].replace('_', ' ')}[/{style}]"


@delivery_app.command("board")
def delivery_board(project_id: str):
    """Work items and where each stage stands."""
    b = _call("GET", f"/projects/{project_id}/delivery")
    table = Table(title="Delivery")
    table.add_column("Work item")
    for s in b["stages"]:
        table.add_column(s["label"])
    for i in b["items"]:
        stages = {s["key"]: s for s in i["stages"]}
        table.add_row(f"{i['title'][:40]}\n[dim]{i['id']}[/dim]",
                      *[_stage_cell(stages[s["key"]]) for s in b["stages"]])
    console.print(table)
    for s in b["slos"]:
        console.print(f"SLO {s['title']} ({s['objective']}%): {s['state']} · {s['reason']}")


@delivery_app.command("add")
def delivery_add(project_id: str, title: str, body: str = typer.Option("", "--body"),
                 repo: str | None = typer.Option(None, "--repo")):
    """Add a work item by hand."""
    i = _call("POST", f"/projects/{project_id}/issues",
              json={"title": title, "body": body, "repo": repo})
    console.print(f"added {i['id']}  {i['title']}")


@delivery_app.command("import")
def delivery_import(project_id: str, repo: str, labels: str = "", limit: int = 20):
    """Import open GitHub issues (owner/repo) as work items."""
    r = _call("POST", f"/projects/{project_id}/issues/import",
              json={"repo": repo, "labels": labels, "limit": limit})
    for i in r["imported"]:
        console.print(f"{i['id']}  {i['title']}")


@delivery_app.command("advance")
def delivery_advance(item_id: str, stage: str | None = typer.Option(None, "--stage"),
                     autonomy: str = typer.Option("L2", "--autonomy"),
                     target: str = typer.Option("claude_code", "--target",
                                                help="claude_code | codex | cursor"),
                     pr_url: str | None = typer.Option(None, "--pr"),
                     post: bool = typer.Option(False, "--post", help="Post the review (L3+)")):
    """Run the agent for a work item's next stage (or --stage). Checkpoints must be signed off."""
    r = _call("POST", f"/work-items/{item_id}/advance",
              json={"stage": stage, "autonomy": autonomy, "target": target, "pr_url": pr_url,
                    "post": post})
    run = r["run"]
    console.print(f"[bold]{run['agent']}[/bold] {run['status']}: "
                  f"{(run.get('output') or {}).get('summary', '')}")
    for s in r["item"]["stages"]:
        wait = " · waiting for " + s["role_label"] if s["pending_approvals"] else ""
        console.print(f"  {s['label']:<8} {_stage_cell(s)} {s['detail']}{wait}")


@delivery_app.command("handoff")
def delivery_handoff(task_id: str, prompt: bool = typer.Option(
        False, "--prompt", help="Print only the prompt (pipe it into your coding agent)")):
    """The brief for Claude Code, Codex or Cursor: claude "$(fil delivery handoff ID --prompt)"."""
    h = _call("GET", f"/tasks/{task_id}/handoff")
    if prompt:
        print(h["prompt"])
        return
    console.print(h["brief"])
    for name, cmd in (h.get("commands") or {}).items():
        console.print(f"[dim]{name}:[/dim] {cmd}")


@delivery_app.command("link-pr")
def delivery_link_pr(task_id: str, url: str):
    """Attach the pull request opened for a task (the webhook does this automatically)."""
    r = _call("POST", f"/tasks/{task_id}/pull-request", json={"url": url})
    console.print(f"linked {r['id']}")


slo_app = typer.Typer(help="SLOs the SRE agent watches", no_args_is_help=True)
delivery_app.add_typer(slo_app, name="slo")


@slo_app.command("add")
def slo_add(project_id: str, title: str, objective: float = 99.9,
            service: str | None = typer.Option(None, "--service")):
    r = _call("POST", f"/projects/{project_id}/slos",
              json={"title": title, "objective": objective, "service": service})
    console.print(f"added SLO {r['id']}  {r['title']} ({r['objective']}%)")


@slo_app.command("sample")
def slo_sample(slo_id: str, good: int, total: int):
    """Record a measurement: good events out of total."""
    _call("POST", f"/slos/{slo_id}/samples", json=[{"good": good, "total": total}])
    console.print("recorded")


@slo_app.command("check")
def slo_check(project_id: str):
    """Evaluate SLOs now: auto-rollback, incidents and follow-up issues."""
    r = _call("POST", f"/projects/{project_id}/slo-check")
    for s in r["slos"]:
        console.print(f"{s['title']}: {s['state']} · {s['reason']}")
    for a in r["actions"]:
        console.print(f"[red]incident {a['incident']}[/red] follow-up {a['follow_up_issue']}"
                      + (f" · rolled back {a['rolled_back']}" if a["rolled_back"] else ""))


@delivery_app.command("release")
def delivery_release(release_id: str, action: str = typer.Argument(
        ..., help="promote | rollback"), by: str = "human", reason: str = ""):
    """Roll an approved release to its next step, or roll it back."""
    r = _call("POST", f"/releases/{release_id}/{action}", json={"by": by, "reason": reason})
    live = [s["percent"] for s in r["steps"] if s["status"] == "done"]
    console.print(f"{r['stage']}" + (f" · {live[-1]}% live" if live else ""))


@delivery_app.command("resolve")
def delivery_resolve(incident_id: str, by: str = "human", note: str = ""):
    """Close an incident (on-call)."""
    _call("POST", f"/incidents/{incident_id}/resolve", json={"by": by, "reason": note})
    console.print("resolved")


if __name__ == "__main__":
    app()
