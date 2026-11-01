"""The system map: how onboarded repos connect to each other.

Links come from evidence in the analyses, never from names alone when better evidence exists:
- calls: a repo's HTTP calls match another repo's endpoints (strongest), or a repo reads a
  service URL / talks to a host named after another repo
- configures: a config repo holds files named after another repo
- provisions: an infrastructure repo declares resources named after another repo
- shared: external services (Stripe, Sentry, ...) used by more than one repo
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SHARED_CATEGORIES = {"external", "observability"}


@dataclass
class RepoView:
    id: str
    name: str
    kinds: list[str]
    grade: str
    analysis: dict


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    evidence: list[dict] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)


def stem(name: str) -> str:
    """checkout-api -> checkout; web-storefront -> storefront; payments_service -> payments."""
    parts = [p for p in re.split(r"[-_.]", name.lower()) if p]
    drop = {"api", "service", "svc", "server", "backend", "frontend", "web", "app", "ui",
            "client", "config", "configs", "infra", "infrastructure", "repo", "core"}
    kept = [p for p in parts if p not in drop]
    return "-".join(kept or parts)


def _aliases(name: str) -> set[str]:
    n = name.lower()
    return {n, n.replace("_", "-"), stem(n), n.replace("-", "_"), n.replace("-", "")}


def _segs(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _matches(call: dict, ep: dict) -> bool:
    """Same method, and the paths agree segment by segment from the end (params match
    anything). A differing prefix such as /api or /v1 on one side is allowed."""
    if ep["method"] not in {call["method"], "ANY"}:
        return False
    c, e = _segs(call["path"]), _segs(ep["path"])
    n = min(len(c), len(e))
    if n == 0 or abs(len(c) - len(e)) > 2:
        return False
    literal = 0
    for x, y in zip(c[-n:], e[-n:], strict=True):
        if x == "{}" or y.startswith("{"):
            continue
        if x != y:
            return False
        literal += 1
    extra = (c[:-n] if len(c) > n else e[:-n])
    return literal > 0 and all(not s.startswith("{") and s != "{}" for s in extra)


def _base_points_to(base: str | None, repo: RepoView) -> bool:
    if not base:
        return False
    b = base.lower().replace("_", "-")
    b = re.sub(r"^(react-app|vite|next-public|nuxt-public|expo-public|vue-app)-", "", b)
    b = re.sub(r"-(url|uri|host|base-url|api-url|endpoint|addr)$", "", b)
    return any(a and (a == b or a in b.split(".")[0] or b.startswith(a)) for a in
               _aliases(repo.name) - {""}) or stem(repo.name) in b


def build(repos: list[RepoView]) -> dict:
    by_id = {r.id: r for r in repos}
    edges: dict[tuple[str, str, str], Edge] = {}
    consumers: dict[str, dict[str, list[dict]]] = {r.id: {} for r in repos}
    unresolved: list[dict] = []

    def edge(src: str, dst: str, kind: str) -> Edge:
        return edges.setdefault((src, dst, kind), Edge(src, dst, kind))

    for a in repos:
        calls = a.analysis.get("client_calls") or []
        for b in repos:
            if a.id == b.id:
                continue
            eps = b.analysis.get("api", {}).get("endpoints") or []
            # 1) HTTP calls that hit b's endpoints (base must agree when we know it).
            for call in calls:
                if call.get("base") and not _base_points_to(call["base"], b) and \
                        any(_base_points_to(call["base"], o) for o in repos if o.id != b.id):
                    continue
                hit = next((ep for ep in eps if _matches(call, ep)), None)
                if hit:
                    e = edge(a.id, b.id, "calls")
                    key = f"{hit['method']} {hit['path']}"
                    if key not in e.endpoints:
                        e.endpoints.append(key)
                    e.evidence.append({"file": call["file"], "line": call["line"],
                                       "detail": f"{call['method']} {call['path']}"})
                    consumers[b.id].setdefault(key, []).append(
                        {"repo_id": a.id, "repo": a.name, "file": call["file"],
                         "line": call["line"]})
            # Calls aimed at b (by base URL) that match none of its endpoints: likely broken.
            if eps:
                for call in calls:
                    if _base_points_to(call.get("base"), b) and \
                            not any(_matches(call, ep) for ep in eps):
                        unresolved.append({"repo_id": a.id, "repo": a.name, "target_id": b.id,
                                           "target": b.name, "method": call["method"],
                                           "path": call["path"], "file": call["file"],
                                           "line": call["line"]})
            # 2) Service references in a's component view named after b.
            for n in (a.analysis.get("components") or {}).get("nodes", []):
                if n["category"] in {"service"} and (
                        _aliases(n["label"]) & _aliases(b.name) or stem(n["label"]) == stem(b.name)):
                    e = edge(a.id, b.id, "calls")
                    e.evidence += n["evidence"][:2]
            # 3) Config repos with files named after b.
            if "config" in a.kinds:
                files = [c for c in _config_files(a) if _aliases(b.name) & {_file_stem(c)}]
                if files:
                    edge(a.id, b.id, "configures").evidence += [
                        {"file": f, "line": None, "detail": "configuration"} for f in files[:6]]
            # 4) Infrastructure resources named after b.
            if "infrastructure" in a.kinds:
                s = stem(b.name)
                for n in (a.analysis.get("components") or {}).get("nodes", []):
                    label = n["label"].lower()
                    resource = label.split(" (")[0]
                    if n["category"] != "self" and s and len(s) > 2 and (
                            resource == s or resource.startswith(s + "-") or
                            resource.startswith(s + "_") or resource in _aliases(b.name)):
                        e = edge(a.id, b.id, "provisions")
                        e.evidence.append({"file": (n["evidence"] or [{}])[0].get("file"),
                                           "line": (n["evidence"] or [{}])[0].get("line"),
                                           "detail": n["label"]})

    # Shared external services.
    shared: dict[str, dict] = {}
    for r in repos:
        for n in (r.analysis.get("components") or {}).get("nodes", []):
            if n["category"] in SHARED_CATEGORIES:
                s = shared.setdefault(n["id"], {"id": f"x:{n['id']}", "label": n["label"],
                                                "category": n["category"], "repos": []})
                if r.id not in s["repos"]:
                    s["repos"].append(r.id)

    nodes = [{"id": r.id, "label": r.name, "kind": "repo", "kinds": r.kinds, "grade": r.grade,
              "endpoints": len(r.analysis.get("api", {}).get("endpoints") or []),
              "pages": len(r.analysis.get("ui", {}).get("pages") or [])} for r in repos]
    ext_edges = []
    for s in shared.values():
        nodes.append({"id": s["id"], "label": s["label"], "kind": s["category"],
                      "shared_by": len(s["repos"])})
        ext_edges += [{"source": rid, "target": s["id"], "kind": "uses", "evidence": [],
                       "endpoints": []} for rid in s["repos"]]
    out_edges = [{"source": e.source, "target": e.target, "kind": e.kind,
                  "evidence": _dedupe(e.evidence)[:8], "endpoints": e.endpoints[:30]}
                 for e in edges.values()] + ext_edges
    return {"nodes": nodes, "edges": out_edges, "consumers": consumers, "unresolved": unresolved,
            "mermaid": mermaid(nodes, out_edges, by_id)}


def _dedupe(ev: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in ev:
        k = (e.get("file"), e.get("line"), e.get("detail"))
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _config_files(r: RepoView) -> list[str]:
    return list(dict.fromkeys(r.analysis.get("config_files") or []))


def _file_stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return re.sub(r"\.(ya?ml|json|toml|env|properties|ini|conf)$", "", name.lower())


def impact(system: dict, repo_id: str) -> dict:
    """Who depends on this repo (and how), and what it depends on."""
    names = {n["id"]: n["label"] for n in system["nodes"]}
    dependents = [{"repo_id": e["source"], "repo": names.get(e["source"]), "kind": e["kind"],
                   "endpoints": e["endpoints"], "evidence": e["evidence"]}
                  for e in system["edges"] if e["target"] == repo_id and
                  not e["source"].startswith("x:")]
    dependencies = [{"id": e["target"], "name": names.get(e["target"]), "kind": e["kind"],
                     "endpoints": e["endpoints"]}
                    for e in system["edges"] if e["source"] == repo_id]
    return {"dependents": dependents, "dependencies": dependencies,
            "endpoint_consumers": system["consumers"].get(repo_id, {}),
            "unresolved_calls": [u for u in system.get("unresolved", [])
                                 if repo_id in (u["repo_id"], u["target_id"])]}


def mermaid(nodes: list[dict], edges: list[dict], by_id: dict) -> str:
    def nid(x: str) -> str:
        return "n_" + re.sub(r"\W", "_", x)

    lines = ["flowchart LR"]
    for n in nodes:
        label = n["label"].replace('"', "'")
        if n["kind"] == "repo":
            kinds = "/".join(k for k in n["kinds"][:2])
            lines.append(f'  {nid(n["id"])}(["{label}<br/><small>{kinds}</small>"])')
        else:
            lines.append(f'  {nid(n["id"])}["{label}"]')
    verbs = {"calls": "calls", "configures": "configures", "provisions": "provisions",
             "uses": "uses"}
    for e in edges:
        n_eps = len(e["endpoints"])
        lbl = verbs.get(e["kind"], e["kind"]) + (f" · {n_eps} endpoint{'s' * (n_eps > 1)}"
                                                if n_eps else "")
        arrow = "-.->" if e["kind"] == "uses" else "-->"
        lines.append(f'  {nid(e["source"])} {arrow}|{lbl}| {nid(e["target"])}')
    styles = {"frontend": "#f3e8ff", "api": "#e6f0ff", "backend": "#e6f7ef",
              "infrastructure": "#fff1e0", "config": "#fdf6d8"}
    for n in nodes:
        if n["kind"] == "repo":
            fill = next((styles[k] for k in n["kinds"] if k in styles), "#eef1f5")
            lines.append(f"  style {nid(n['id'])} fill:{fill},stroke:#888")
        else:
            lines.append(f"  style {nid(n['id'])} fill:#f9ece7,stroke:#c4825b")
    return "\n".join(lines)
