"""MCP gateway control and data plane.

Control plane (registry):
- native MCP servers are synced by listing their tools; repo APIs Filament analyzed become
  *generated* servers with one tool per endpoint (no code needed in the service).
- discovered tools start disabled; owners enable them. Changes found on re-sync, or edits to a
  description, wait as a pending change until approved; history allows rollback.

Data plane (invoke): one path for every caller (agents, the CLI, the Omni MCP endpoint, the
API) that enforces enablement, per-tool agent policy, rate limits and autonomy for writes,
trims responses to requested fields (projection), redacts PII and secrets, caps size and logs
the call for metrics.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from filament.config import get_settings
from filament.models import McpCallLog, McpServer, McpToolEntry, Project, RepoRecord

AUTONOMY_ORDER = ["L0", "L1", "L2", "L3", "L4"]
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
WRITE_NAME = re.compile(r"(?i)^(create|update|delete|remove|drop|insert|execute|run|post|put|"
                        r"patch|set|add|send|merge|close|transition|cancel|refund|write|upload|"
                        r"publish|deploy|restart|grant|revoke)")


class GatewayError(RuntimeError):
    """A call the gateway refused or that failed; safe to show to an agent."""

    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status


# -- redaction ------------------------------------------------------------------------------------
REDACTORS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?"
                               r"-----END [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("api_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}\b|"
                           r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
                           r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}\b|"
                           r"\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"(?<![\w-])\+?\d{1,3}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?![\w-])")),
]


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits if d.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def redact_text(text: str) -> tuple[str, int]:
    count = 0
    for label, rx in REDACTORS:
        def sub(m, label=label):
            nonlocal count
            if label == "card" and not _luhn(m.group(0)):
                return m.group(0)
            count += 1
            return f"[REDACTED:{label}]"
        text = rx.sub(sub, text)
    return text, count


def redact(value):
    """Redact strings anywhere inside a JSON-like value; returns (value, count)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        total, out = 0, []
        for v in value:
            r, n = redact(v)
            out.append(r)
            total += n
        return out, total
    if isinstance(value, dict):
        total, out = 0, {}
        for k, v in value.items():
            r, n = redact(v)
            out[k] = r
            total += n
        return out, total
    return value, 0


# -- projection -----------------------------------------------------------------------------------
def project(value, fields: list[str] | None):
    """Keep only the dotted field paths asked for (lists are mapped element-wise)."""
    if not fields:
        return value
    tree: dict = {}
    for f in fields:
        node = tree
        for part in [p for p in f.split(".") if p]:
            node = node.setdefault(part, {})

    def keep(v, t):
        if not t:
            return v
        if isinstance(v, list):
            return [keep(x, t) for x in v]
        if isinstance(v, dict):
            return {k: keep(v[k], sub) for k, sub in t.items() if k in v}
        return v

    return keep(value, tree)


# -- rate limiting --------------------------------------------------------------------------------
_windows: dict[str, deque] = defaultdict(deque)


def _rate_ok(entry: McpToolEntry) -> bool:
    limit = entry.rate_limit_per_min
    if not limit:
        return True
    now = time.monotonic()
    q = _windows[entry.id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


# -- registry sync --------------------------------------------------------------------------------
def _snapshot(entry: McpToolEntry, by: str) -> dict:
    return {"version": entry.version, "description": entry.description,
            "input_schema": entry.input_schema, "at": datetime.now(UTC).isoformat(), "by": by}


def _upsert(session: Session, server: McpServer, tools: list[dict], *, enable_new,
            source: str) -> dict:
    """enable_new: bool, or a callable(name) -> bool deciding per tool."""
    existing = {e.name: e for e in session.scalars(
        select(McpToolEntry).where(McpToolEntry.server_id == server.id))}
    seen, summary = set(), {"new": [], "changed": [], "removed": [], "unchanged": 0}
    for t in tools:
        name = t["name"]
        seen.add(name)
        desc = t.get("description") or ""
        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        entry = existing.get(name)
        if entry is None:
            # Writes come from the contract (HTTP method) or the owner's list; a name that only
            # looks like a write (create_*, delete_*) is flagged for review in meta.
            write = bool(t.get("write")) or name in (server.write_tools or [])
            on = enable_new(name) if callable(enable_new) else bool(enable_new)
            meta = {**(t.get("meta") or {}),
                    **({"looks_like_write": True} if not write and WRITE_NAME.match(name) else {})}
            entry = McpToolEntry(server_id=server.id, name=name, description=desc,
                                 input_schema=schema, write=write, meta=meta, version=1,
                                 enabled=on, status="active" if on else "new")
            entry.history = [_snapshot(entry, source)]
            session.add(entry)
            summary["new"].append(name)
            continue
        entry.meta = t.get("meta") or entry.meta
        if entry.status == "removed":
            entry.status = "new"  # came back: needs an owner to re-enable
        if desc != entry.description or schema != entry.input_schema:
            proposed = {"description": desc, "input_schema": schema, "source": source,
                        "at": datetime.now(UTC).isoformat()}
            if entry.pending != proposed:
                entry.pending = proposed
                entry.status = "changed"
                summary["changed"].append(name)
        else:
            summary["unchanged"] += 1
    for name, entry in existing.items():
        if name not in seen and entry.status != "removed":
            entry.status, entry.enabled = "removed", False
            summary["removed"].append(name)
    server.last_synced_at = datetime.now(UTC)
    return summary


def sync_native(session: Session, server: McpServer) -> dict:
    """List a native server's tools and reconcile them with the registry.

    On the first sync the server was just added by an owner, so its tools are enabled
    (respecting allowed_tools); tools that appear later start disabled for review.
    """
    from filament.mcp_gateway import discover

    tools = discover(server)
    server.tools_cache = tools
    first = not session.scalars(select(McpToolEntry.id).where(
        McpToolEntry.server_id == server.id).limit(1)).first()
    allowed = set(server.allowed_tools or [])
    summary = _upsert(session, server, tools, source="sync",
                      enable_new=lambda n: first and (not allowed or n in allowed))
    session.flush()
    return summary


def _tool_name(ep: dict) -> str:
    base = ep.get("handler") or f"{ep['method']}_{ep['path']}"
    return re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_").lower()[:60] or "call"


def _inline(schema: dict | None, components: dict, depth: int = 0) -> dict:
    if not schema or depth > 3:
        return {"type": "object"}
    if "$ref" in schema:
        return _inline(components.get(schema["$ref"].rsplit("/", 1)[-1]), components, depth + 1)
    out = {k: v for k, v in schema.items() if k not in {"x-source"}}
    if schema.get("type") == "object" and schema.get("properties"):
        out["properties"] = {k: _inline(v, components, depth + 1)
                             for k, v in schema["properties"].items()}
    if schema.get("type") == "array" and schema.get("items"):
        out["items"] = _inline(schema["items"], components, depth + 1)
    return out


def endpoint_tool(ep: dict, components: dict) -> dict:
    props: dict = {}
    required: list[str] = []
    for p in ep.get("params") or []:
        props[p["name"]] = {**(p.get("schema") or {"type": "string"}),
                            "description": f"{p.get('in') or 'path'} parameter"}
        if (p.get("in") or "path") == "path" or p.get("required"):
            required.append(p["name"])
    if ep.get("request"):
        props["body"] = {**_inline(ep["request"].get("schema"), components),
                         "description": "JSON request body"}
        required.append("body")
    summary = ep.get("summary") or ep.get("description") or ""
    auth = " Requires authentication." if ep.get("auth") else ""
    desc = f"{summary.rstrip('.')}. {ep['method']} {ep['path']}.{auth}".lstrip(". ")
    if ep.get("description") and ep["description"] != summary:
        desc += f" {ep['description']}"
    ok = next((c for c in sorted(ep.get("responses") or {}) if c.startswith("2")), "200")
    return {"name": _tool_name(ep), "description": desc[:600],
            "input_schema": {"type": "object", "properties": props, "required": required},
            "write": ep["method"] in WRITE_METHODS,
            "meta": {"method": ep["method"], "path": ep["path"], "ok": ok,
                     "errors": sorted(c for c in (ep.get("responses") or {}) if c[0] in "45"),
                     "example_response": ep.get("example_response"),
                     "file": ep.get("file"), "line": ep.get("line")}}


def generated_name(repo_name: str) -> str:
    return ("api-" + re.sub(r"[^a-z0-9_-]+", "-", repo_name.lower())).strip("-")[:40]


def sync_generated(session: Session, record: RepoRecord) -> McpServer | None:
    """Create or update the generated MCP server for a repo's API (tools disabled by default)."""
    api = (record.analysis or {}).get("api") or {}
    eps = [e for e in api.get("endpoints") or []
           if e["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}]
    server = session.scalars(select(McpServer).where(
        McpServer.kind == "generated", McpServer.source_repo_id == record.id)).first()
    if not eps and server is None:
        return None
    if server is None:
        server = McpServer(workspace_id=record.workspace_id, scope="workspace", scope_ref=None,
                           name=generated_name(record.name), kind="generated",
                           source_repo_id=record.id, transport="openapi", url=None,
                           description=f"Generated by Filament from {record.name}'s API "
                                       f"({api.get('source')}). Set a base URL to call the real "
                                       "service; without one, calls are simulated from the "
                                       "contract's examples.",
                           enabled=True, min_autonomy_for_writes="L3", redact=True)
        session.add(server)
        session.flush()
    components = api.get("schemas") or ((api.get("openapi") or {}).get("components") or {}).get(
        "schemas") or {}
    tools, names = [], set()
    for ep in eps:
        t = endpoint_tool(ep, components)
        if t["name"] in names:
            t["name"] = f"{t['name']}_{ep['method'].lower()}"[:60]
        names.add(t["name"])
        tools.append(t)
    server.tools_cache = [{k: t[k] for k in ("name", "description", "input_schema")}
                          for t in tools]
    _upsert(session, server, tools, enable_new=False, source=f"repo scan {record.name}")
    session.flush()
    return server


def apply_server_lists(session: Session, server: McpServer) -> None:
    """Server-level allowed_tools / write_tools edits update the registry entries."""
    allowed = set(server.allowed_tools or [])
    writes = set(server.write_tools or [])
    for e in session.scalars(select(McpToolEntry).where(McpToolEntry.server_id == server.id)):
        if e.status == "removed":
            continue
        if allowed:
            e.enabled = e.name in allowed
            e.status = "active" if e.enabled else ("changed" if e.pending else "new")
        if server.kind != "generated":
            e.write = e.name in writes


# -- owner actions --------------------------------------------------------------------------------
def approve(entry: McpToolEntry, by: str = "owner") -> McpToolEntry:
    if not entry.pending:
        raise GatewayError("Nothing pending for this tool", "conflict")
    entry.description = entry.pending["description"]
    entry.input_schema = entry.pending["input_schema"]
    entry.version += 1
    entry.history = [*(entry.history or []), _snapshot(entry, by)][-20:]
    entry.pending = None
    entry.status = "active" if entry.enabled else "new"
    entry.updated_at = datetime.now(UTC)
    return entry


def reject(entry: McpToolEntry) -> McpToolEntry:
    entry.pending = None
    entry.status = "active" if entry.enabled else "new"
    return entry


def propose_description(entry: McpToolEntry, description: str, by: str) -> McpToolEntry:
    entry.pending = {"description": description, "input_schema": entry.input_schema,
                     "source": f"edit by {by}", "at": datetime.now(UTC).isoformat()}
    entry.status = "changed"
    return entry


def rollback(entry: McpToolEntry, version: int, by: str = "owner") -> McpToolEntry:
    snap = next((h for h in entry.history or [] if h["version"] == version), None)
    if not snap:
        raise GatewayError(f"No version {version} in this tool's history", "not_found")
    entry.description, entry.input_schema = snap["description"], snap["input_schema"]
    entry.version += 1
    entry.history = [*(entry.history or []),
                     {**_snapshot(entry, by), "rolled_back_to": version}][-20:]
    entry.pending = None
    entry.status = "active" if entry.enabled else "new"
    return entry


# -- discovery ------------------------------------------------------------------------------------
def _servers(session: Session, workspace_id: str, project: Project | None) -> list[McpServer]:
    if project is not None:
        from filament.mcp_gateway import visible_servers

        return visible_servers(session, project)
    return session.scalars(select(McpServer).where(McpServer.workspace_id == workspace_id,
                                                   McpServer.enabled.is_(True))).all()


def enabled_entries(session: Session, server: McpServer) -> list[McpToolEntry]:
    return session.scalars(select(McpToolEntry).where(
        McpToolEntry.server_id == server.id, McpToolEntry.enabled.is_(True),
        McpToolEntry.status != "removed").order_by(McpToolEntry.name)).all()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower().replace("_", " ")))


def discover_servers(session: Session, workspace_id: str, query: str = "",
                     project: Project | None = None, agent: str | None = None) -> list[dict]:
    q = _words(query)
    out = []
    for s in _servers(session, workspace_id, project):
        tools = [e for e in enabled_entries(session, s)
                 if not e.allowed_agents or (agent and agent in e.allowed_agents)]
        if not tools:
            continue
        text = " ".join([s.name, s.description] + [f"{e.name} {e.description}" for e in tools])
        score = len(q & _words(text)) if q else 1
        if q and not score:
            continue
        out.append({"server": s.name, "description": s.description[:200], "kind": s.kind,
                    "tools": len(tools), "score": score,
                    "examples": [e.name for e in tools[:5]]})
    return sorted(out, key=lambda x: -x["score"])[:10]


def find(session: Session, workspace_id: str, server_name: str, tool: str | None = None,
         project: Project | None = None):
    server = next((s for s in _servers(session, workspace_id, project) if s.name == server_name),
                  None)
    if server is None:
        raise GatewayError(f"No server '{server_name}'. Use discover_servers to find one.",
                           "not_found")
    if tool is None:
        return server, None
    entry = session.scalars(select(McpToolEntry).where(McpToolEntry.server_id == server.id,
                                                       McpToolEntry.name == tool)).first()
    if entry is None:
        raise GatewayError(f"No tool '{tool}' on {server_name}. Use list_tools.", "not_found")
    return server, entry


def list_tools(session: Session, server: McpServer, agent: str | None = None) -> list[dict]:
    return [{"name": e.name, "description": e.description[:200], "write": e.write}
            for e in enabled_entries(session, server)
            if not e.allowed_agents or (agent and agent in e.allowed_agents)]


# -- invoke ---------------------------------------------------------------------------------------
def _http_call(server: McpServer, entry: McpToolEntry, args: dict) -> tuple[object, bool]:
    """Call a generated tool's endpoint, or simulate it from the contract when no base URL."""
    import httpx

    from filament.mcp_gateway import _resolve

    meta = entry.meta or {}
    path = meta.get("path", "/")
    params, body = {}, args.get("body")
    for name, value in args.items():
        if name == "body":
            continue
        if "{" + name + "}" in path:
            path = path.replace("{" + name + "}", str(value))
        else:
            params[name] = value
    missing = re.findall(r"\{(\w+)\}", path)
    if missing:
        raise GatewayError(f"Missing path parameter(s): {', '.join(missing)}", "error")
    if not server.url:
        return (meta.get("example_response") if meta.get("example_response") is not None
                else {"ok": True}), True
    resp = httpx.request(meta.get("method", "GET"), server.url.rstrip("/") + path, params=params,
                         json=body, headers=_resolve(server.headers or {}),
                         timeout=server.timeout_s)
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    if resp.status_code >= 400:
        raise GatewayError(f"HTTP {resp.status_code}: {str(data)[:500]}", "error")
    return data, False


def invoke(session: Session, workspace_id: str, server: McpServer, entry: McpToolEntry,
           arguments: dict, *, actor: str, autonomy: str = "L1", run_id: str | None = None,
           fields: list[str] | None = None, max_chars: int | None = None) -> dict:
    max_chars = max_chars or get_settings().tool_result_max_chars
    log = McpCallLog(workspace_id=workspace_id, server_id=server.id, server=server.name,
                     tool=entry.name, actor=actor, run_id=run_id, status="ok")
    started = time.monotonic()

    def deny(msg: str, status: str = "denied"):
        log.status, log.error = status, msg
        session.add(log)
        raise GatewayError(msg, status)

    agent = actor.split(":", 1)[1] if actor.startswith("agent:") else None
    if not entry.enabled or entry.status == "removed":
        deny(f"{server.name}.{entry.name} is not enabled in the gateway registry")
    if entry.allowed_agents and agent not in entry.allowed_agents:
        deny(f"{entry.name} is limited to agents: {', '.join(entry.allowed_agents)}")
    if entry.write:
        required = server.min_autonomy_for_writes or "L2"
        if AUTONOMY_ORDER.index(autonomy) < AUTONOMY_ORDER.index(required):
            deny(f"{entry.name} changes external systems and needs autonomy {required} or "
                 f"higher; this call is {autonomy}.")
    if not _rate_ok(entry):
        deny(f"Rate limit for {entry.name} ({entry.rate_limit_per_min}/min) reached; retry "
             "shortly.", "rate_limited")
    simulated = False
    try:
        if server.kind == "generated":
            data, simulated = _http_call(server, entry, arguments)
            raw = json.dumps(data, default=str)
            out: dict = {"server": server.name, "tool": entry.name, "is_error": False,
                         "result": data}
        else:
            from filament.mcp_gateway import McpCallError
            from filament.mcp_gateway import call as mcp_call

            try:
                res = mcp_call(server, entry.name, arguments, 10**7)
            except McpCallError as exc:
                raise GatewayError(str(exc)) from exc
            raw = res.get("content", "")
            data = res.get("structured")
            content = (res.get("content") or "").strip()
            if data is None and content[:1] in "{[" and content:
                try:  # JSON text content becomes a result that projection can trim
                    data = json.loads(content)
                except ValueError:
                    data = None
            out = {"server": server.name, "tool": entry.name, "is_error": res["is_error"],
                   "content": res.get("content", "")}
            if data is not None:
                out["result"] = data
                if fields:  # the projected result replaces the full text
                    out.pop("content")
    except GatewayError as exc:
        log.status, log.error = "error", str(exc)[:1000]
        log.latency_ms = int((time.monotonic() - started) * 1000)
        session.add(log)
        raise
    except Exception as exc:  # unexpected transport errors: report, don't crash the caller
        log.status, log.error = "error", f"{type(exc).__name__}: {exc}"[:1000]
        log.latency_ms = int((time.monotonic() - started) * 1000)
        session.add(log)
        raise GatewayError(log.error) from exc

    if fields and "result" in out:
        out["result"] = project(out["result"], fields)
        out["projected"] = fields
    redactions = 0
    if server.redact:
        out, redactions = redact(out)
    text = json.dumps(out, default=str)
    if len(text) > max_chars:
        budget = max_chars - 300
        if "content" in out and "result" in out:
            out.pop("result")  # keep one copy
        key = "content" if "content" in out else "result"
        body = out[key] if isinstance(out.get(key), str) else json.dumps(out.get(key), default=str)
        out = {**{k: v for k, v in out.items() if k != key}, key: body[:budget],
               "truncated": f"{len(body) - budget} characters omitted; pass `fields` to keep "
                            "only what you need"}
        text = json.dumps(out, default=str)
    if simulated:
        out["simulated"] = True
        out["note"] = ("No base URL is configured for this generated server, so this is the "
                       "example response from the API contract.")
    log.status = "simulated" if simulated else ("error" if out.get("is_error") else "ok")
    log.latency_ms = int((time.monotonic() - started) * 1000)
    payload = out.get("result", out.get("content", ""))
    returned = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    log.bytes_raw, log.bytes_returned, log.redactions = len(raw), len(returned), redactions
    session.add(log)
    return out


# -- metrics --------------------------------------------------------------------------------------
def metrics(session: Session, workspace_id: str, hours: int = 24) -> dict:
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = session.scalars(select(McpCallLog).where(McpCallLog.workspace_id == workspace_id,
                                                    McpCallLog.at >= since)).all()
    by: dict[tuple[str, str], list[McpCallLog]] = defaultdict(list)
    for r in rows:
        by[(r.server, r.tool)].append(r)

    def pct(vals: list[int], p: float) -> int:
        if not vals:
            return 0
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]

    tools = []
    for (server, tool), calls in sorted(by.items(), key=lambda kv: -len(kv[1])):
        lat = [c.latency_ms for c in calls if c.status in {"ok", "simulated", "error"}]
        tools.append({"server": server, "tool": tool, "calls": len(calls),
                      "errors": sum(c.status == "error" for c in calls),
                      "denied": sum(c.status in {"denied", "rate_limited"} for c in calls),
                      "simulated": sum(c.status == "simulated" for c in calls),
                      "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95),
                      "bytes_saved": sum(max(0, c.bytes_raw - c.bytes_returned) for c in calls),
                      "redactions": sum(c.redactions for c in calls),
                      "actors": sorted({c.actor for c in calls})})
    return {"hours": hours, "calls": len(rows),
            "errors": sum(r.status == "error" for r in rows),
            "denied": sum(r.status in {"denied", "rate_limited"} for r in rows),
            "bytes_saved": sum(max(0, r.bytes_raw - r.bytes_returned) for r in rows),
            "redactions": sum(r.redactions for r in rows), "tools": tools}


def sync_due(session: Session, limit: int = 5) -> int:
    """Re-sync native servers whose tool lists haven't been checked recently (AutoCrawler)."""
    hours = get_settings().mcp_sync_interval_hours
    if hours <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    n = 0
    for s in session.scalars(select(McpServer).where(McpServer.kind == "native",
                                                     McpServer.enabled.is_(True))).all():
        last = s.last_synced_at
        if last is not None and (last if last.tzinfo else last.replace(tzinfo=UTC)) > cutoff:
            continue
        try:
            sync_native(session, s)
            s.last_test_ok, s.last_test_error = True, None
        except BaseException as exc:  # noqa: BLE001
            s.last_test_ok, s.last_test_error = False, f"{type(exc).__name__}: {exc}"[:2000]
            s.last_synced_at = datetime.now(UTC)
        session.commit()
        n += 1
        if n >= limit:
            break
    return n
