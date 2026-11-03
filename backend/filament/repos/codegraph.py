"""Symbol-level code graph for onboarded repos, built with Graphify (tree-sitter, no LLM).

Graphify parses ~40 languages into functions, classes, modules and their calls, imports,
inheritance and references, tags every edge EXTRACTED (in the source) or INFERRED (resolved),
clusters the graph into subsystems and turns NOTE/WHY comments and docstrings into rationale
nodes. Filament runs it in code-only mode into a per-repo output folder (never into the repo),
so its cache makes rescans incremental, and reads graph.json through this adapter.

Everything downstream (handler-level request flows, function -> endpoint -> consumer impact,
query/path/explain tools, PR impact) works on the normalized graph returned here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

KNOWN_SCHEMAS = {1}
CODE_KINDS = {"code"}
NON_SOURCE = (".yml", ".yaml", ".json", ".toml", ".lock", ".md", ".ini", ".cfg", ".txt", ".xml")
STRUCTURAL = {"contains", "rationale_for"}
DEP_RELATIONS = {"calls", "imports", "imports_from", "references", "uses", "inherits",
                 "mixes_in", "method"}


class CodeGraphError(RuntimeError):
    pass


# -- engine ------------------------------------------------------------------------------------
def engine_path() -> str | None:
    found = shutil.which("graphify")
    if found:
        return found
    sibling = Path(sys.executable).with_name("graphify")
    return str(sibling) if sibling.exists() else None


def engine_version(binary: str | None = None) -> str | None:
    try:
        from importlib.metadata import version

        return version("graphifyy")
    except Exception:  # noqa: BLE001 - not installed as a package
        return None


def build(root: Path, out_dir: Path, timeout: int = 600) -> Path:
    """Run Graphify code-only extraction into out_dir; returns the graph.json path.

    out_dir persists between scans of the same repo, so unchanged files come from the cache.
    """
    binary = engine_path()
    if not binary:
        raise CodeGraphError("Graphify is not installed (pip install graphifyy)")
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GRAPHIFY_BACKEND": "none", "GRAPHIFY_NO_TELEMETRY": "1"}
    env.pop("ANTHROPIC_API_KEY", None)  # code-only: never send code to a model
    try:
        proc = subprocess.run([binary, "extract", str(root), "--code-only", "--out", str(out_dir)],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise CodeGraphError(f"Graphify timed out after {timeout}s") from exc
    graph = out_dir / "graphify-out" / "graph.json"
    if proc.returncode != 0 or not graph.exists():
        raise CodeGraphError(f"Graphify failed: {(proc.stderr or proc.stdout)[-400:]}")
    return graph


# -- adapter -------------------------------------------------------------------------------------
def _line(loc) -> int | None:
    m = re.match(r"L?(\d+)", str(loc or ""))
    return int(m.group(1)) if m else None


def normalize(raw: dict) -> dict:
    """graph.json -> Filament's code graph. Unknown fields are dropped; a schema version we
    haven't seen is accepted best-effort and flagged."""
    meta = raw.get("graph") or {}
    schema = meta.get("schema_version")
    warnings = [] if schema in KNOWN_SCHEMAS else [
        f"graph.json schema_version {schema!r} is newer than this adapter knows; read best-effort"]
    nodes = []
    for n in raw.get("nodes") or []:
        if not isinstance(n, dict) or "id" not in n:
            continue
        kind = n.get("file_type") or n.get("type") or "code"
        nodes.append({"id": str(n["id"]), "label": str(n.get("label") or n["id"]), "kind": kind,
                      "file": n.get("source_file"), "line": _line(n.get("source_location")),
                      "community": n.get("community"),
                      "callable": bool(n.get("_callable")),
                      "is_class": bool(n.get("_callable_class"))})
    ids = {n["id"] for n in nodes}
    edges = []
    for e in raw.get("links") or raw.get("edges") or []:
        s, t = str(e.get("source")), str(e.get("target"))
        if s not in ids or t not in ids:
            continue
        edges.append({"source": s, "target": t, "relation": e.get("relation") or "related",
                      "confidence": (e.get("confidence") or "EXTRACTED").lower(),
                      "score": e.get("confidence_score", 1.0), "context": e.get("context"),
                      "file": e.get("source_file"), "line": _line(e.get("source_location"))})
    return {"nodes": nodes, "edges": edges, "schema_version": schema,
            "engine": "graphify", "engine_version": meta.get("graphify_version"),
            "directed": raw.get("directed", False), "warnings": warnings}


# -- analysis ------------------------------------------------------------------------------------
class Index:
    """Lookups over a normalized graph."""

    def __init__(self, g: dict):
        self.g = g
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.out: dict[str, list[dict]] = defaultdict(list)
        self.inc: dict[str, list[dict]] = defaultdict(list)
        for e in g["edges"]:
            self.out[e["source"]].append(e)
            self.inc[e["target"]].append(e)

    def by_file(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for n in self.g["nodes"]:
            if n.get("file"):
                out[n["file"]].append(n)
        return out


def name_communities(g: dict) -> list[dict]:
    """Subsystems: Graphify's communities, named after their dominant directory or file."""
    members: dict[int, list[dict]] = defaultdict(list)
    for n in g["nodes"]:
        if n.get("community") is not None and n["kind"] in CODE_KINDS and not (
                n.get("file") or "").endswith(NON_SOURCE):
            members[n["community"]].append(n)
    idx = Index(g)
    out = []
    for cid, ns in members.items():
        files = Counter(n["file"] for n in ns if n.get("file"))
        dirs = Counter(str(Path(f).parent) for f in files.elements())
        top_file = files.most_common(1)[0][0] if files else ""
        top_dir = dirs.most_common(1)[0][0] if dirs else ""
        # Named after the file holding most of it (like "orders"), or its folder when spread out.
        top2 = [Path(f).stem for f, _ in files.most_common(2)]
        if files and files.most_common(1)[0][1] >= len(ns) * 0.5 or len(top2) == 1:
            name = Path(top_file).stem
        elif len(files) <= 3:
            name = " + ".join(top2)
        else:
            name = top_dir.rsplit("/", 1)[-1] if top_dir not in {".", ""} else " + ".join(top2)
        name = name or f"group {cid}"
        ids = {n["id"] for n in ns}
        internal = sum(1 for e in g["edges"] if e["source"] in ids and e["target"] in ids
                       and e["relation"] not in STRUCTURAL)
        boundary = sum(1 for e in g["edges"] if (e["source"] in ids) != (e["target"] in ids)
                       and e["relation"] not in STRUCTURAL)
        degree = {n["id"]: sum(1 for e in idx.out[n["id"]] + idx.inc[n["id"]]
                               if e["relation"] not in STRUCTURAL) for n in ns}
        key = sorted(ns, key=lambda n: -degree[n["id"]])[:5]
        out.append({"id": cid, "name": name, "size": len(ns), "files": len(files),
                    "cohesion": round(internal / max(1, internal + boundary), 2),
                    "key_symbols": [n["label"] for n in key]})
    # Disambiguate duplicate names.
    seen = Counter(c["name"] for c in out)
    dup_n: Counter = Counter()
    for c in sorted(out, key=lambda c: -c["size"]):
        if seen[c["name"]] > 1:
            dup_n[c["name"]] += 1
            if dup_n[c["name"]] > 1:
                c["name"] = f"{c['name']} ({c['key_symbols'][0] if c['key_symbols'] else c['id']})"
    return sorted(out, key=lambda c: -c["size"])


def hotspots(g: dict, top: int = 10) -> list[dict]:
    idx = Index(g)
    scored = []
    for n in g["nodes"]:
        if n["kind"] not in CODE_KINDS or not n["callable"] or not n.get("file") \
                or (n.get("file") or "").endswith(NON_SOURCE):
            continue  # functions and classes defined in the repo only
        inc = [e for e in idx.inc[n["id"]] if e["relation"] not in STRUCTURAL]
        out = [e for e in idx.out[n["id"]] if e["relation"] not in STRUCTURAL]
        if not inc and not out:
            continue
        scored.append({"id": n["id"], "label": n["label"], "file": n["file"], "line": n["line"],
                       "fan_in": len(inc), "fan_out": len(out), "degree": len(inc) + len(out)})
    return sorted(scored, key=lambda x: (-x["fan_in"], -x["degree"]))[:top]


def import_cycles(g: dict, limit: int = 10) -> list[list[str]]:
    """Module import cycles. Imports inside a function body (deferred on purpose, usually to
    break a cycle) don't count."""
    idx = Index(g)
    graph: dict[str, set[str]] = defaultdict(set)
    module = {n["id"]: n["file"] for n in g["nodes"] if n.get("file")}
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for _, (f, start, end) in _ranges(idx).items():
        spans[f].append((start, end))
    for e in g["edges"]:
        if e["relation"] in {"imports", "imports_from"}:
            if e.get("line") and any(s < e["line"] <= end for s, end in spans.get(e.get("file"), [])):
                continue
            a, b = module.get(e["source"]), module.get(e["target"])
            if a and b and a != b:
                graph[a].add(b)
    cycles, color, stack = [], {}, []

    def dfs(v):
        color[v] = 1
        stack.append(v)
        for w in sorted(graph[v]):
            if color.get(w) == 1 and len(cycles) < limit:
                cycles.append(stack[stack.index(w):] + [w])
            elif not color.get(w):
                dfs(w)
        stack.pop()
        color[v] = 2

    for v in sorted(graph):
        if not color.get(v):
            dfs(v)
    del idx
    return cycles


NOTE_RX = re.compile(r"^\s*(?:#|//|--|/\*+|\*)?\s*(NOTE|WHY|HACK|IMPORTANT|DECISION)\s*[:\-]\s*(.+?)\s*(?:\*/)?$",
                     re.I)
REF_RX = re.compile(r"\b(ADR[- ]?\d+|RFC[- ]?\d+)\b", re.I)


def rationale(g: dict) -> list[dict]:
    """Design notes: NOTE/WHY/DECISION comments (not plain docstrings), with ADR/RFC refs."""
    idx = Index(g)
    out = []
    for n in g["nodes"]:
        if n["kind"] != "rationale":
            continue
        m = NOTE_RX.match(n["label"])
        if not m:
            continue
        about = next((idx.nodes[e["target"]] for e in idx.out[n["id"]]
                      if e["relation"] == "rationale_for" and e["target"] in idx.nodes), None)
        out.append({"id": n["id"], "tag": m.group(1).upper(), "text": m.group(2).strip(),
                    "file": n["file"], "line": n["line"],
                    "about": about["label"] if about else None,
                    "about_id": about["id"] if about else None,
                    "refs": sorted({r.upper().replace(" ", "-") for r in REF_RX.findall(n["label"])})})
    return out


# -- handler -> service attribution ------------------------------------------------------------------
def _ranges(idx: Index) -> dict[str, tuple[str, int, int]]:
    """Approximate line ranges of callables: start line to the next symbol in the same file."""
    out = {}
    for file, ns in idx.by_file().items():
        starts = sorted((n["line"], n["id"]) for n in ns if n.get("line") and n["kind"] in CODE_KINDS
                        and n["callable"])
        for i, (line, nid) in enumerate(starts):
            end = starts[i + 1][0] - 1 if i + 1 < len(starts) else line + 400
            out[nid] = (file, line, end)
    return out


IMPORT_ALIAS = re.compile(r"^\s*(?:import\s+([\w.]+)(?:\s+as\s+(\w+))?|from\s+([\w.]+)\s+import\s+(.+))",
                          re.M)
JS_IMPORT = re.compile(r"""import\s+(?:(\w+)|\*\s+as\s+(\w+)|\{([^}]+)\})\s+from\s+['"]([^'"]+)['"]|"""
                       r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")


def _aliases(text: str) -> dict[str, str]:
    """name used in code -> imported package (top level)."""
    out = {}
    for m in IMPORT_ALIAS.finditer(text):
        if m.group(1):
            pkg = m.group(1)
            out[m.group(2) or pkg.split(".")[0]] = pkg.split(".")[0]
        else:
            pkg = m.group(3).split(".")[0]
            for part in m.group(4).replace("(", "").replace(")", "").split(","):
                name = part.strip().split(" as ")[-1].strip()
                if name and name != "*":
                    out[name] = pkg
    for m in JS_IMPORT.finditer(text):
        pkg = m.group(4) or m.group(6)
        if not pkg or pkg.startswith("."):
            continue
        for name in filter(None, [m.group(1), m.group(2), m.group(5)]):
            out[name] = pkg
        for part in (m.group(3) or "").split(","):
            name = part.strip().split(" as ")[-1].strip()
            if name:
                out[name] = pkg
    # Module-level clients built from imports: producer = KafkaProducer(), s3 = boto3.client(...)
    for m in re.finditer(r"^(?:const\s+|let\s+|var\s+)?(\w+)\s*(?::\s*[\w.\[\]]+)?\s*=\s*"
                         r"(?:new\s+)?(\w+)\s*[.(]", text, re.M):
        if m.group(2) in out and m.group(1) not in out:
            out[m.group(1)] = out[m.group(2)]
    return out


def lib_node(pkg: str):
    """Map an imported package to Filament's component catalogue (kafka -> Kafka, ...)."""
    from filament.repos.deps import LIBS

    for cand in (pkg, pkg.replace("_", "-"), pkg.lower(), pkg.split("/")[0],
                 {"kafka": "kafka-python", "sentry_sdk": "sentry-sdk"}.get(pkg, "")):
        if cand and cand in LIBS:
            return LIBS[cand]
    return None


def attribute(g: dict, read) -> dict[str, list[dict]]:
    """For every callable: the services it uses directly (its own body references an imported
    client) or through calls it makes (transitively), each with how Filament knows.

    `read(path) -> str` reads a repo file. Returns {node_id: [{service, label, category, via,
    confidence, file, line}]}.
    """
    idx = Index(g)
    ranges = _ranges(idx)
    texts: dict[str, str] = {}
    aliases: dict[str, dict[str, str]] = {}

    def text(f):
        if f not in texts:
            try:
                texts[f] = read(f) or ""
            except Exception:  # noqa: BLE001
                texts[f] = ""
            aliases[f] = _aliases(texts[f])
        return texts[f]

    direct: dict[str, list[dict]] = defaultdict(list)
    for nid, (f, start, end) in ranges.items():
        body_lines = text(f).splitlines()[start - 1:end]
        body = "\n".join(body_lines[1:]) if len(body_lines) > 1 else ""
        for alias, pkg in aliases[f].items():
            hit = lib_node(pkg)
            if not hit or not re.search(rf"(?<![\w.]){re.escape(alias)}\s*[.(]", body):
                continue
            ln = next((start + i for i, line in enumerate(body_lines) if re.search(
                rf"(?<![\w.]){re.escape(alias)}\s*[.(]", line) and i > 0), start)
            if not any(d["service"] == hit[0] for d in direct[nid]):
                direct[nid].append({"service": hit[0], "label": hit[1], "category": hit[2],
                                    "via": "direct", "confidence": "extracted", "file": f,
                                    "line": ln})
    calls: dict[str, set[str]] = defaultdict(set)
    for e in g["edges"]:
        if e["relation"] == "calls":
            calls[e["source"]].add(e["target"])
    result: dict[str, list[dict]] = {}
    for nid in ranges:
        found = {d["service"]: d for d in direct.get(nid, [])}
        seen, q = {nid}, deque([(nid, [])])
        while q:
            cur, path = q.popleft()
            for nxt in calls.get(cur, ()):
                if nxt in seen or len(path) > 6:
                    continue
                seen.add(nxt)
                for d in direct.get(nxt, []):
                    if d["service"] not in found:
                        found[d["service"]] = {**d, "via": " → ".join(
                            idx.nodes[p]["label"] for p in [*path, nxt] if p in idx.nodes),
                            "confidence": "extracted"}
                q.append((nxt, [*path, nxt]))
        if found:
            result[nid] = list(found.values())
    return result


def handler_node(idx: Index, ep: dict) -> str | None:
    """The graph node that implements an endpoint (same file, handler name, nearest line)."""
    if not ep.get("file") or not ep.get("handler"):
        return None
    cands = [n for n in idx.by_file().get(ep["file"], []) if n["callable"] and
             n["label"].rstrip("()").split(".")[-1] == ep["handler"]]
    if not cands:
        return None
    line = ep.get("line") or 0
    return min(cands, key=lambda n: abs((n.get("line") or 0) - line))["id"]


def summary(g: dict, endpoints: list[dict], read) -> dict:
    """What goes into the repo analysis (the full graph is stored separately)."""
    idx = Index(g)
    comms = name_communities(g)
    by_kind = Counter(n["kind"] for n in g["nodes"])
    rel = Counter(e["relation"] for e in g["edges"])
    conf = Counter(e["confidence"] for e in g["edges"])
    total = max(1, sum(conf.values()))
    services = attribute(g, read)
    ep_map = {}
    for ep in endpoints:
        nid = handler_node(idx, ep)
        if nid:
            ep_map[f"{ep['method']} {ep['path']}"] = {"node": nid, "services": services.get(nid, [])}
    return {"engine": g["engine"], "engine_version": g.get("engine_version"),
            "schema_version": g.get("schema_version"), "warnings": g.get("warnings", []),
            "nodes": len(g["nodes"]), "edges": len(g["edges"]),
            "symbols": sum(1 for n in g["nodes"] if n["kind"] in CODE_KINDS and n["callable"]),
            "files": len({n["file"] for n in g["nodes"] if n.get("file")}),
            "kinds": dict(by_kind), "relations": dict(rel),
            "extracted_pct": round(100 * conf.get("extracted", 0) / total),
            "inferred_pct": round(100 * conf.get("inferred", 0) / total),
            "communities": comms[:30], "hotspots": hotspots(g), "cycles": import_cycles(g),
            "rationale": rationale(g)[:50], "endpoints": ep_map}


def align_services(code: dict, components: dict) -> None:
    """Use the component view's names: a generic ORM 'database' becomes the concrete store."""
    nodes = {n["id"]: n for n in components.get("nodes") or []}
    stores = [n for n in nodes.values() if n["category"] == "datastore"]
    for ep in (code.get("endpoints") or {}).values():
        for s in ep["services"]:
            if s["service"] not in nodes and s["category"] == "datastore" and len(stores) == 1:
                s["service"], s["label"] = stores[0]["id"], stores[0]["label"]
            elif s["service"] in nodes:
                s["label"] = nodes[s["service"]]["label"]


def load_file(path: Path) -> dict:
    return normalize(json.loads(path.read_text()))
