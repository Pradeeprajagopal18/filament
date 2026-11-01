"""Compact, budgeted views of a repo for agents (context packs and the repo tools).

Agents get facts Filament read from the code: what the repo is, its endpoints, screens,
connections and who depends on it. Lists are capped so a big repo can't flood the context.
"""

from __future__ import annotations

import re

from filament.context import keywords


def _ep_line(ep: dict) -> str:
    bits = [f"{ep['method']} {ep['path']}"]
    if ep.get("summary"):
        bits.append(f"— {ep['summary'][:80]}")
    if ep.get("auth"):
        bits.append("[auth]")
    req = (ep.get("request") or {}).get("schema") or {}
    if "$ref" in req:
        bits.append(f"body {req['$ref'].rsplit('/', 1)[-1]}")
    return " ".join(bits)


def summary(record, system: dict | None = None, *, endpoints: int = 25, pages: int = 12,
            query: str = "") -> dict:
    a = record.analysis or {}
    api = a.get("api") or {}
    eps = api.get("endpoints") or []
    if query:  # most relevant endpoints first
        q = keywords(query)
        eps = sorted(eps, key=lambda e: -len(q & keywords(f"{e['path']} {e.get('summary', '')} "
                                                          f"{e.get('handler', '')}")))
    comps = [f"{n['label']} ({n['category']})" for n in (a.get("components") or {}).get("nodes", [])
             if n["category"] != "self"]
    ui_pages = []
    for p in (a.get("ui") or {}).get("pages", [])[:pages]:
        heads = [e["label"] for e in p["elements"] if e["type"] in {"heading", "subheading"}][:3]
        acts = [e["label"] for e in p["elements"] if e["type"] in {"button", "input"}
                and e["label"]][:5]
        ui_pages.append({"path": p["path"], "component": p["component"], "headings": heads,
                         "actions": acts})
    out = {
        "id": record.id, "name": record.name, "ref": record.ref, "kinds": record.kinds,
        "grade": record.grade, "frameworks": (a.get("profile") or {}).get("frameworks", []),
        "commands": a.get("commands", {}),
        "api": {"source": api.get("source"), "count": len(api.get("endpoints") or []),
                "auth": api.get("auth", []),
                "endpoints": [_ep_line(e) for e in eps[:endpoints]],
                "models": sorted((api.get("schemas") or {}).keys())[:30]},
        "pages": ui_pages,
        "connects_to": comps[:25],
        "failing_checks": [c["title"] for c in (a.get("readiness") or {}).get("checks", [])
                           if c["status"] == "fail"],
        "scanned_at": str(record.scanned_at)[:19],
    }
    if system:
        from filament.repos.system import impact

        imp = impact(system, record.id)
        out["used_by"] = [f"{d['repo']} ({d['kind']}"
                          + (f": {', '.join(d['endpoints'][:4])}" if d["endpoints"] else "") + ")"
                          for d in imp["dependents"]]
        out["depends_on"] = [f"{d['name']} ({d['kind']})" for d in imp["dependencies"]]
    return out


def render(s: dict) -> str:
    lines = [f"Repo {s['name']} ({', '.join(s['kinds'])}; grade {s['grade']}) — {s['ref']}"]
    if s["frameworks"]:
        lines.append("Frameworks: " + ", ".join(s["frameworks"]))
    if s["api"]["count"]:
        lines.append(f"API ({s['api']['source']}, {s['api']['count']} endpoints"
                     + (f", auth: {', '.join(s['api']['auth'])}" if s["api"]["auth"] else "")
                     + "):")
        lines += [f"  {e}" for e in s["api"]["endpoints"]]
        if s["api"]["count"] > len(s["api"]["endpoints"]):
            lines.append(f"  … {s['api']['count'] - len(s['api']['endpoints'])} more "
                         "(use get_repo to see them)")
    if s["pages"]:
        lines.append("Screens: " + "; ".join(
            f"{p['path']} ({', '.join(p['headings'][:2]) or p['component']})" for p in s["pages"]))
    if s["connects_to"]:
        lines.append("Connects to: " + ", ".join(s["connects_to"]))
    if s.get("used_by"):
        lines.append("Used by: " + "; ".join(s["used_by"]))
    if s.get("depends_on"):
        lines.append("Depends on: " + "; ".join(s["depends_on"]))
    return "\n".join(lines)


def relevance(record, query: str) -> int:
    a = record.analysis or {}
    text = " ".join([record.name, " ".join(record.kinds or []),
                     " ".join(e["path"] + " " + (e.get("summary") or "")
                              for e in (a.get("api") or {}).get("endpoints", [])[:200]),
                     " ".join(p["path"] + " " + p["component"]
                              for p in (a.get("ui") or {}).get("pages", [])),
                     " ".join(n["label"] for n in (a.get("components") or {}).get("nodes", []))])
    words = keywords(re.sub(r"[/{}_\-]", " ", text))
    return len(keywords(query) & words)
