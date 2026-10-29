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
    agent: str = typer.Argument(..., help="requirements | flow"),
    project: str = typer.Option(..., "--project", "-p"),
    idea: str | None = typer.Option(None, help="Idea text (requirements agent)"),
    from_file: Path | None = typer.Option(None, "--from", help="Read the idea from a file"),
    autonomy: str = typer.Option("L1", help="L0 suggest .. L4 autonomous"),
    goal: str | None = typer.Option(None),
):
    """Run an agent against a project."""
    payload: dict = {}
    if from_file:
        payload["idea"] = from_file.read_text()
    elif idea:
        payload["idea"] = idea
    body = {"agent": agent, "goal": goal or f"{agent} run", "input": payload,
            "autonomy": autonomy, "requested_by": os.environ.get("USER", "cli")}
    r = _call("POST", f"/projects/{project}/runs", json=body)
    colour = "green" if r["status"] == "succeeded" else "red"
    console.print(f"Run {r['id']}: [{colour}]{r['status']}[/{colour}]  model={r['model']}  "
                  f"tokens in/out={r['input_tokens']}/{r['output_tokens']}")
    if r["error"]:
        console.print(f"[red]{r['error']}[/red]")
    out = {k: v for k, v in r["output"].items() if k != "spec"}
    if out:
        console.print_json(json.dumps(out))


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


if __name__ == "__main__":
    app()
