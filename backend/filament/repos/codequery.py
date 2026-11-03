"""Questions over a repo's code graph: explain, path, query, affected and change impact.

Names are resolved strictly: an ambiguous name returns the candidates instead of a guess, and
an unknown name returns close matches (agents can then ask again precisely).
"""

from __future__ import annotations

import re
from collections import deque

from filament.repos.codegraph import STRUCTURAL, Index


class Ambiguous(Exception):
    def __init__(self, name: str, candidates: list[dict]):
        super().__init__(f"'{name}' matches {len(candidates)} symbols; pick one by id")
        self.candidates = candidates


class NotFound(Exception):
    def __init__(self, name: str, suggestions: list[dict]):
        super().__init__(f"No symbol matches '{name}'")
        self.suggestions = suggestions


def _brief(n: dict) -> dict:
    return {"id": n["id"], "label": n["label"], "kind": n["kind"], "file": n.get("file"),
            "line": n.get("line")}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9_.]", "", s.lower().replace("()", ""))


def resolve(idx: Index, name: str) -> dict:
    name = name.strip()
    if name in idx.nodes:
        return idx.nodes[name]
    key = _norm(name)
    file_part, _, sym = name.rpartition(":") if ":" in name else ("", "", name)
    exact = [n for n in idx.nodes.values() if _norm(n["label"]) == _norm(sym)
             and (not file_part or (n.get("file") or "").endswith(file_part))]
    if not exact:  # dotted: module.func or Class.method
        tail = key.split(".")[-1]
        exact = [n for n in idx.nodes.values() if _norm(n["label"]) == tail and key.split(".")[0]
                 in _norm(n.get("file") or "") + _norm(n["id"])] if "." in key else []
    if not exact:
        exact = [n for n in idx.nodes.values() if n.get("file") == name and n["kind"] == "code"
                 and not n["callable"]]
    code = [n for n in exact if n["kind"] == "code"]
    exact = code or exact
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise Ambiguous(name, [_brief(n) for n in exact[:20]])
    words = set(re.findall(r"[a-z0-9]{3,}", key))
    sugg = sorted((n for n in idx.nodes.values() if n["kind"] == "code"
                   and (key in _norm(n["label"]) or words & set(re.findall(r"[a-z0-9]{3,}",
                                                                          _norm(n["label"]))))),
                  key=lambda n: len(n["label"]))
    raise NotFound(name, [_brief(n) for n in sugg[:10]])


def _edge(e: dict, other: dict, direction: str) -> dict:
    return {"relation": e["relation"], "direction": direction, "confidence": e["confidence"],
            "node": _brief(other), "at": f"{e.get('file')}:{e.get('line')}" if e.get("line") else e.get("file")}


def explain(g: dict, name: str, extra: dict | None = None, limit: int = 30) -> dict:
    idx = Index(g)
    n = resolve(idx, name)
    out_e = [_edge(e, idx.nodes[e["target"]], "out") for e in idx.out[n["id"]]
             if e["target"] in idx.nodes and e["relation"] != "rationale_for"]
    in_e = [_edge(e, idx.nodes[e["source"]], "in") for e in idx.inc[n["id"]]
            if e["source"] in idx.nodes and e["relation"] != "rationale_for"]
    notes = [idx.nodes[e["source"]]["label"] for e in idx.inc[n["id"]]
             if e["relation"] == "rationale_for" and e["source"] in idx.nodes]
    calls = [x for x in out_e if x["relation"] == "calls"]
    callers = [x for x in in_e if x["relation"] == "calls"]
    res = {"symbol": _brief(n), "community": n.get("community"),
           "calls": calls[:limit], "called_by": callers[:limit],
           "other": [x for x in out_e + in_e if x["relation"] not in {"calls"}][:limit],
           "notes": notes[:10]}
    if extra:
        res.update(extra)
    return res


def path(g: dict, a: str, b: str, max_hops: int = 8) -> dict:
    idx = Index(g)
    src, dst = resolve(idx, a), resolve(idx, b)
    adj: dict[str, list[tuple[str, dict]]] = {}
    for e in g["edges"]:
        if e["relation"] in STRUCTURAL and e["relation"] != "contains":
            continue
        adj.setdefault(e["source"], []).append((e["target"], e))
        adj.setdefault(e["target"], []).append((e["source"], e))  # traverse both ways
    prev: dict[str, tuple[str, dict]] = {}
    q, seen = deque([(src["id"], 0)]), {src["id"]}
    while q:
        cur, d = q.popleft()
        if cur == dst["id"] or d >= max_hops:
            if cur == dst["id"]:
                break
            continue
        for nxt, e in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                prev[nxt] = (cur, e)
                q.append((nxt, d + 1))
    if dst["id"] not in prev and src["id"] != dst["id"]:
        return {"from": _brief(src), "to": _brief(dst), "found": False, "hops": []}
    hops, cur = [], dst["id"]
    while cur != src["id"]:
        p, e = prev[cur]
        hops.append({"from": idx.nodes[p]["label"], "relation": e["relation"],
                     "to": idx.nodes[cur]["label"], "confidence": e["confidence"],
                     "forward": e["source"] == p, "at": f"{e.get('file')}:{e.get('line')}"})
        cur = p
    hops.reverse()
    weakest = min(hops, key=lambda h: 0 if h["confidence"] == "inferred" else 1) if hops else None
    return {"from": _brief(src), "to": _brief(dst), "found": True, "hops": hops,
            "weakest": weakest, "confidence": "inferred" if any(
                h["confidence"] == "inferred" for h in hops) else "extracted"}


def query(g: dict, question: str, limit: int = 25) -> dict:
    """Seed on symbols matching the question's words, then expand one hop (BFS)."""
    idx = Index(g)
    words = {w for w in re.findall(r"[a-z0-9_]{3,}", question.lower())} - {
        "the", "and", "what", "how", "does", "where", "which", "who", "for", "with", "from"}
    if not words:
        return {"seeds": [], "nodes": [], "edges": []}

    def score(n):
        text = f"{n['label']} {n.get('file') or ''}".lower().replace("_", " ")
        return sum(1 for w in words if w in text or w.replace("_", " ") in text)

    seeds = sorted((n for n in idx.nodes.values() if n["kind"] in {"code", "rationale"} and score(n)),
                   key=lambda n: (-score(n), n["kind"] != "code"))[:8]
    keep = {n["id"] for n in seeds}
    for s in list(keep):
        for e in idx.out[s] + idx.inc[s]:
            if len(keep) >= limit:
                break
            if e["relation"] not in {"contains"}:
                keep.add(e["target"] if e["source"] == s else e["source"])
    edges = [{"source": e["source"], "target": e["target"], "relation": e["relation"],
              "confidence": e["confidence"]} for e in g["edges"]
             if e["source"] in keep and e["target"] in keep and e["relation"] != "contains"]
    return {"seeds": [_brief(n) for n in seeds], "nodes": [_brief(idx.nodes[i]) for i in keep],
            "edges": edges[:limit * 3]}


def affected(g: dict, ids: list[str], depth: int = 3) -> dict[str, int]:
    """Reverse traversal: everything that calls, imports, references or inherits from these
    symbols within `depth` hops (id -> distance)."""
    idx = Index(g)
    seeds = set(ids)
    dist = {i: 0 for i in ids}
    q = deque(ids)
    while q:
        cur = q.popleft()
        if dist[cur] >= depth:
            continue
        for e in idx.inc[cur]:
            if e["relation"] in STRUCTURAL:
                continue
            if e["source"] not in dist:
                dist[e["source"]] = dist[cur] + 1
                q.append(e["source"])
        # A *changed* module (a seed) affects everything it contains; a module that merely
        # imports a changed symbol does not make all of its functions affected.
        n = idx.nodes.get(cur)
        if n and dist[cur] == 0 and cur in seeds and not n["callable"] and n["kind"] == "code":
            for e in idx.out[cur]:
                if e["relation"] == "contains" and e["target"] not in dist:
                    dist[e["target"]] = dist[cur]
                    q.append(e["target"])
    return dist


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


def parse_diff(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Unified diff -> {file: [(start, end), ...]} of changed lines in the new version."""
    out: dict[str, list[tuple[int, int]]] = {}
    chunks = re.split(r"(?=^diff --git )", diff, flags=re.M)
    for chunk in chunks:
        m = DIFF_FILE.search(chunk)
        if not m:
            continue
        ranges = [(int(h.group(1)), int(h.group(1)) + max(int(h.group(2) or 1), 1) - 1)
                  for h in HUNK.finditer(chunk)]
        out[m.group(1)] = ranges or [(1, 10**9)]
    return out


def changed_symbols(g: dict, changes: dict[str, list[tuple[int, int]] | None]) -> list[str]:
    """Symbols whose code a change touches (whole file when no line ranges are given)."""
    from filament.repos.codegraph import _ranges

    idx = Index(g)
    ranges = _ranges(idx)
    out = []
    for f, spans in changes.items():
        file_nodes = [n for n in idx.by_file().get(f, []) if n["kind"] == "code"]
        if not file_nodes:
            continue
        if not spans:  # whole file: the module and every symbol in it
            out += [n["id"] for n in sorted(file_nodes, key=lambda n: n["callable"])]
            continue
        hit = []
        in_file = {nid: r for nid, r in ranges.items() if r[0] == f}
        for s, e in spans:
            span_hits = [nid for nid, (_, start, end) in in_file.items() if s <= end and e >= start]
            # innermost only: a change in a method changes the method, not its whole class
            hit += [a for a in span_hits if not any(
                b != a and ranges[a][1] <= ranges[b][1] and ranges[b][2] <= ranges[a][2]
                and ranges[b][1] <= e and s <= ranges[b][2] for b in span_hits)]
        out += hit or [n["id"] for n in file_nodes if not n["callable"]]
    return list(dict.fromkeys(out))


def change_impact(g: dict, endpoints_map: dict, changes: dict, consumers: dict,
                  hotspot_ids: set[str], depth: int = 3) -> dict:
    """Changed files/lines -> changed symbols -> affected symbols -> endpoints -> consumers."""
    idx = Index(g)
    changed = changed_symbols(g, changes)
    dist = affected(g, changed, depth)
    node_to_ep = {v["node"]: k for k, v in endpoints_map.items()}
    eps = sorted({node_to_ep[i] for i in dist if i in node_to_ep})
    users: dict[str, list[str]] = {}
    for ep in eps:
        for u in consumers.get(ep, []):
            users.setdefault(u["repo"], [])
            if ep not in users[u["repo"]]:
                users[u["repo"]].append(ep)
    comms = sorted({idx.nodes[i].get("community") for i in dist if i in idx.nodes
                    and idx.nodes[i].get("community") is not None})
    risky = [idx.nodes[i]["label"] for i in changed if i in hotspot_ids]
    affected_syms = [{**_brief(idx.nodes[i]), "distance": d} for i, d in
                     sorted(dist.items(), key=lambda x: x[1]) if i in idx.nodes
                     and idx.nodes[i]["kind"] == "code" and idx.nodes[i]["callable"]]
    level = "high" if users or risky else ("medium" if eps else "low")
    return {"files": sorted(changes), "changed": [_brief(idx.nodes[i]) for i in changed
                                                   if i in idx.nodes][:100],
            "affected": affected_syms[:200], "endpoints": eps,
            "consumers": [{"repo": r, "endpoints": e} for r, e in sorted(users.items())],
            "communities": comms, "hotspots_touched": risky, "risk": level,
            "summary": ", ".join([_n(len(changed), "symbol") + " changed",
                                  f"{len(affected_syms)} affected", _n(len(eps), "endpoint"),
                                  _n(len(users), "consuming repo")])}


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'' if k == 1 else 's'}"


def conflicts(g: dict, changesets: list[dict]) -> list[dict]:
    """Pairs of change sets (e.g. open PRs) that touch the same symbols or subsystems."""
    idx = Index(g)
    touched = []
    for cs in changesets:
        ch = {f: None for f in cs.get("files", [])}
        syms = set(changed_symbols(g, ch))
        aff = set(affected(g, list(syms), 1))
        comms = {idx.nodes[i].get("community") for i in syms if i in idx.nodes} - {None}
        touched.append((cs.get("name", "?"), syms, aff, comms))
    out = []
    for i in range(len(touched)):
        for j in range(i + 1, len(touched)):
            a, b = touched[i], touched[j]
            same = a[1] & b[1]
            near = (a[2] & b[1]) | (b[2] & a[1])
            shared_comm = a[3] & b[3]
            if same or near or shared_comm:
                out.append({"a": a[0], "b": b[0],
                            "same_symbols": sorted(idx.nodes[x]["label"] for x in same)[:20],
                            "adjacent_symbols": sorted(idx.nodes[x]["label"] for x in near - same)[:20],
                            "shared_subsystems": sorted(shared_comm),
                            "severity": "conflict" if same else ("likely" if near else "watch")})
    return out
