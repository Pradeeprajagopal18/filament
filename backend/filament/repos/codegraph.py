"""Symbol-level code graph for onboarded repos: analysis over the graph `codeindex` builds.

The graph comes from Filament's own extractor (`codeparse` + `codeindex`: tree-sitter, no
model, cached per file). It holds modules, classes, functions and methods with exact line
ranges, and their calls, imports, inheritance and references, each tagged `extracted` (resolved
from the code) or `inferred` (name match only), plus notes from docstrings and NOTE/WHY
comments and Louvain subsystems.

Everything downstream (handler-level request flows, function -> endpoint -> consumer impact,
query/path/explain tools, PR impact) works on that graph through this module.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from pathlib import Path

CODE_KINDS = {"code"}
NON_SOURCE = (".yml", ".yaml", ".json", ".toml", ".lock", ".md", ".ini", ".cfg", ".txt", ".xml")
STRUCTURAL = {"contains", "rationale_for"}
DEP_RELATIONS = {"calls", "imports", "imports_from", "references", "uses", "inherits", "method"}


class CodeGraphError(RuntimeError):
    pass


def available() -> bool:
    from filament.repos import codeparse

    return codeparse.available()


def build(root: Path, files: list[str], cache_dir: Path | None = None) -> dict:
    """Parse the repo's source files and return the resolved code graph."""
    from filament.repos import codeindex

    if not available():
        raise CodeGraphError("tree-sitter grammars are not installed (pip install -e '.[code]')")
    return codeindex.build(root, files, cache_dir)


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
    """Subsystems (Louvain communities), named after their dominant file or directory."""
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
    break a cycle) and type-only imports (`import type`, `if TYPE_CHECKING:`) don't count."""
    idx = Index(g)
    graph: dict[str, set[str]] = defaultdict(set)
    module = {n["id"]: n["file"] for n in g["nodes"] if n.get("file")}
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for _, (f, start, end) in _ranges(idx).items():
        spans[f].append((start, end))
    for e in g["edges"]:
        if e["relation"] in {"imports", "imports_from"} and e.get("context") != "type":
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
    """Line ranges of callables. The extractor records exact end lines; graphs without them
    fall back to "until the next symbol in the same file"."""
    out = {}
    for file, ns in idx.by_file().items():
        starts = sorted((n["line"], n["id"]) for n in ns if n.get("line") and n["kind"] in CODE_KINDS
                        and n["callable"])
        for i, (line, nid) in enumerate(starts):
            end = idx.nodes[nid].get("end") or (
                starts[i + 1][0] - 1 if i + 1 < len(starts) else line + 400)
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
    nested: dict[str, list[tuple[int, int]]] = defaultdict(list)
    per_file: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for nid, (f, start, end) in ranges.items():
        per_file[f].append((nid, start, end))
    for items in per_file.values():
        for nid, start, end in items:
            nested[nid] = [(s2, e2) for oid, s2, e2 in items if oid != nid and start < s2 and e2 <= end]
    for nid, (f, start, end) in ranges.items():
        body_lines = text(f).splitlines()[start - 1:end]
        # a class's own lines, not its methods' (they get their own entries)
        body_lines = [ln if not any(a <= start + i <= b for a, b in nested[nid]) else ""
                      for i, ln in enumerate(body_lines)]
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
            "languages": g.get("languages", {}),
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
