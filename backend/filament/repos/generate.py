"""Create missing readiness items inside Filament (graph + memory), not in the repo.

Each generator turns a repo analysis into a draft artifact linked to the repo's project, so the
team gets an AGENTS.md, an architecture overview or an API contract immediately, reviews it in
Filament, and exports it to the repo later. Conventions found along the way are proposed as
project memory (agent-written memory is always 'proposed' until a person confirms it).
"""

from __future__ import annotations

import json

import yaml

GENERATORS = {
    "agents_md": "Agent instructions (AGENTS.md)",
    "overview": "Project overview (README)",
    "architecture": "Architecture and components",
    "codeowners": "Suggested code owners",
    "api_contract": "API contract (OpenAPI)",
    "request_flows": "Request flows (sequence diagrams)",
    "user_flow": "User flow (screens)",
    "component_view": "Component view",
}
# Diagrams Filament keeps for every onboarded repo where they apply (refreshed on rescans).
DIAGRAMS = ["request_flows", "user_flow", "component_view"]
ARTIFACT_TYPE = {"api_contract": "api_contract", "request_flows": "flow", "user_flow": "flow",
                 "component_view": "component_view"}


def _kinds(a: dict) -> str:
    return ", ".join(a["profile"]["kind_names"]) or "unclassified"


def agents_md(a: dict) -> tuple[str, str, dict]:
    p, cmds = a["profile"], a["commands"]
    kinds = set(p["kind_names"])
    lines = [f"# Agent instructions: {a['name']}", "",
             f"Repo type: {_kinds(a)}. Frameworks: {', '.join(p['frameworks']) or '—'}. "
             f"Languages: {', '.join(p['languages']) or '—'}.", "", "## Commands"]
    lines += [f"- {k}: `{v}`" for k, v in cmds.items()] or ["- TODO: add build and test commands"]
    lines += ["", "## Layout"]
    lines += [f"- `{d['path']}/` ({d['files']} files)" for d in a["layout"]] or ["- (flat repo)"]
    lines += ["", "## Conventions"]
    checks = {c["id"]: c for c in a["readiness"]["checks"]}
    if checks.get("lint", {}).get("status") == "pass":
        lines.append(f"- Follow the lint and format config ({checks['lint']['detail']}).")
    if "api" in kinds and a["api"]["endpoints"]:
        lines.append(f"- The API has {len(a['api']['endpoints'])} endpoints; keep the contract "
                     "in sync with every route change.")
    if "frontend" in kinds:
        lines.append("- Reuse existing components before adding new ones; keep pages accessible.")
    if "infrastructure" in kinds:
        lines += ["- Never run `terraform apply` or change live infrastructure from an agent; "
                  "open a pull request with the plan output instead.",
                  "- Keep provider versions pinned and run validate before every commit."]
    if "config" in kinds:
        lines.append("- Change one environment at a time and validate against the schema.")
    lines.append("- Every change ships with tests or validation appropriate to this repo.")
    lines += ["", "## Do not touch", "- Lockfiles by hand, generated code, and secrets."]
    if "infrastructure" in kinds:
        lines.append("- State files and production workspaces.")
    lines += ["", "_Drafted by Filament from the repository. Review before relying on it._"]
    return f"AGENTS.md draft: {a['name']}", "\n".join(lines), {}


def overview(a: dict) -> tuple[str, str, dict]:
    p = a["profile"]
    c = a["components"]
    lines = [f"# {a['name']}", "",
             "_Purpose: TODO, one sentence on what this repo is for and who uses it._", "",
             f"**Type:** {_kinds(a)}  ", f"**Frameworks:** {', '.join(p['frameworks']) or '—'}  ",
             f"**Languages:** {', '.join(p['languages']) or '—'}", "", "## Getting started"]
    lines += [f"- {k}: `{v}`" for k, v in a["commands"].items()] or ["- TODO"]
    deps_out = [n for n in c["nodes"] if n["category"] != "self"]
    if deps_out:
        lines += ["", "## Connects to"] + [f"- {n['label']} ({n['category']})" for n in deps_out[:20]]
    if a["api"]["endpoints"]:
        lines += ["", "## API", f"{len(a['api']['endpoints'])} endpoints "
                  f"({'from the checked-in contract' if a['api']['source'] == 'spec' else 'inferred from code'})."]
    if a["ui"]["pages"]:
        lines += ["", "## Pages"] + [f"- `{pg['path']}` — {pg['component']}" for pg in a["ui"]["pages"][:20]]
    return f"Overview: {a['name']}", "\n".join(lines), {}


def architecture(a: dict) -> tuple[str, str, dict]:
    c = a["components"]
    rows = ["| Component | Type | Evidence |", "| --- | --- | --- |"]
    for n in c["nodes"]:
        if n["category"] == "self":
            continue
        ev = n["evidence"][0] if n["evidence"] else {}
        where = f"{ev.get('file', '')}{':' + str(ev['line']) if ev.get('line') else ''}"
        rows.append(f"| {n['label']} | {n['category']} | {ev.get('detail', '')} ({where}) |")
    text = (f"# Architecture: {a['name']}\n\n```mermaid\n{c['mermaid']}\n```\n\n"
            + ("\n".join(rows) if len(rows) > 2 else "No external connections detected.")
            + "\n\n_Generated by Filament from dependencies, environment variables, URLs, "
              "docker-compose, Kubernetes and Terraform._")
    return f"Architecture: {a['name']}", text, {"components": c}


def codeowners(a: dict) -> tuple[str, str, dict]:
    lines = ["# Suggested CODEOWNERS (replace the placeholders with real teams)",
             "* @your-org/owners"]
    for d in a["layout"][:10]:
        lines.append(f"/{d['path']}/ @your-org/{d['path'].replace('_', '-')}-owners")
    return f"CODEOWNERS suggestion: {a['name']}", "```\n" + "\n".join(lines) + "\n```", {}


def api_contract(a: dict) -> tuple[str, str, dict] | None:
    spec = a["api"].get("openapi")
    if not spec:
        return None
    text = yaml.safe_dump(spec, sort_keys=False, width=100)
    origin = "inferred from code" if a["api"]["source"] == "inferred" else "collected from specs"
    return (f"API contract: {a['name']}",
            f"OpenAPI 3.1, {len(a['api']['endpoints'])} endpoints, {origin}.\n\n```yaml\n{text}```",
            {"openapi": spec})


def _mid(text: str) -> str:
    """A safe Mermaid identifier."""
    import re

    return re.sub(r"\W", "_", text) or "x"


def _mlabel(text: str) -> str:
    import re

    return re.sub(r'["<>{}|;#]', "", text)[:60]


def request_flows(a: dict) -> tuple[str, str, dict] | None:
    """Sequence diagrams per API resource, from the endpoints and the services each handler's
    file actually references."""
    eps = [e for e in a["api"]["endpoints"] if e["method"] in
           {"GET", "POST", "PUT", "PATCH", "DELETE", "ANY"}]
    if not eps:
        return None
    by_file: dict[str, list[dict]] = {}
    for n in a["components"]["nodes"]:
        if n["category"] == "self":
            continue
        for ev in n["evidence"]:
            if ev.get("file"):
                by_file.setdefault(ev["file"], [])
                if n not in by_file[ev["file"]]:
                    by_file[ev["file"]].append(n)
    groups: dict[str, list[dict]] = {}
    for e in eps:
        groups.setdefault(e.get("tag") or "root", []).append(e)
    name = a["name"]
    sections = [f"# Request flows: {name}",
                "Drawn from the code: each endpoint, its auth, the services its handler's file "
                "uses and the responses it can return. Refreshed on every scan."]
    for tag, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:8]:
        deps = []
        for e in items:
            for n in by_file.get(e.get("file") or "", []):
                if n not in deps:
                    deps.append(n)
        lines = ["sequenceDiagram", "  actor C as Client", f"  participant S as {_mlabel(name)}"]
        for n in deps[:5]:
            lines.append(f"  participant {_mid(n['id'])} as {_mlabel(n['label'])}")
        # Which handler calls a service isn't visible from module-level references, so the
        # diagram states what the module uses rather than drawing calls it can't attribute.
        for n in deps[:5]:
            ev = next((v for v in n["evidence"] if any(v.get("file") == e.get("file")
                                                        for e in items)), {})
            where = f"{ev.get('file')}:{ev.get('line')}" if ev.get("line") else ev.get("file", "")
            lines.append(f"  Note over S,{_mid(n['id'])}: {tag} module uses {_mlabel(n['label'])}"
                         f" ({_mlabel(where)})")
        for e in items[:8]:
            lines.append(f"  C->>S: {e['method']} {_mlabel(e['path'])}")
            if e.get("auth"):
                lines.append("  S->>S: check auth")
            codes = sorted((e.get("responses") or {"200": {}}).keys())
            ok = next((c for c in codes if c.startswith("2")), "200")
            schema = ((e.get("responses") or {}).get(ok) or {}).get("schema") or {}
            ref = schema.get("$ref") or (schema.get("items") or {}).get("$ref") or ""
            model = ref.rsplit("/", 1)[-1] + ("[]" if schema.get("type") == "array" else "") \
                if ref else ""
            lines.append(f"  S-->>C: {ok} {model}".rstrip())
            errors = [c for c in codes if not c.startswith("2")]
            if errors:
                lines.append(f"  Note over C,S: or {', '.join(errors)}")
        sections += [f"## {tag}", "```mermaid", *lines, "```"]
    return (f"Request flows: {name}", "\n".join(sections),
            {"diagram": "sequence", "groups": len(groups)})


def user_flow(a: dict) -> tuple[str, str, dict] | None:
    """Screens and how they connect: navigation links and buttons that name another screen."""
    import re

    pages = a["ui"]["pages"]
    if not pages:
        return None
    paths = [p["path"] for p in pages]

    def walk(elements, out, via=None):
        for el in elements:
            out.append((el, via))
            if el.get("children"):
                walk(el["children"], out, via or el["label"])
        return out

    nav_button = re.compile(r"^(go to|view|open|back to|continue to|proceed to|see)\b", re.I)

    def target(el: dict) -> str | None:
        lab = (el.get("label") or "").strip()
        low = lab.lower()
        if el["type"] == "link" and low.startswith("/"):
            for p in paths:
                pattern = re.sub(r"\\\{[^}]+\\\}|:\w+", "[^/]+", re.escape(p))
                if re.fullmatch(pattern, low):
                    return p
            return None
        if el["type"] == "button" and not nav_button.match(lab):
            return None  # actions like "Add to cart" don't navigate
        words = set(re.findall(r"[a-z]{3,}", low)) - {"view", "open", "back", "continue",
                                                       "proceed", "see"}
        return next((p for p in paths if p != "/" and
                     set(re.findall(r"[a-z]{3,}", p.lower())) & words), None)

    # Components that appear on two or more screens (headers, navbars) are global navigation.
    uses: dict[str, int] = {}
    for p in pages:
        for name in {v for _, v in walk(p["elements"], []) if v}:
            uses[name] = uses.get(name, 0) + 1
    shared = {n for n, c in uses.items() if c >= 2 and c >= len(pages) // 2}

    lines = ["flowchart LR", '  start(["Open app"])']
    ids = {p: f"P{i}" for i, p in enumerate(paths)}
    for p in pages:
        heading = next((el["label"] for el, _ in walk(p["elements"], [])
                        if el["type"] == "heading" and el["label"]), p["component"])
        lines.append(f'  {ids[p["path"]]}["{_mlabel(p["path"])}<br/><small>{_mlabel(heading)}</small>"]')
    lines.append(f"  start --> {ids['/' if '/' in paths else paths[0]]}")
    edges: set[tuple[str, str]] = set()
    nav_targets: dict[str, set[str]] = {}
    for p in pages:
        for el, via in walk(p["elements"], []):
            if el["type"] not in {"link", "button"}:
                continue
            dst = target(el)
            if not dst:
                continue
            if via in shared:
                nav_targets.setdefault(via, set()).add(dst)
                continue
            if dst != p["path"] and (p["path"], dst) not in edges:
                edges.add((p["path"], dst))
                lab = el.get("label") or ""
                via_lbl = "" if lab.startswith("/") else f"|{_mlabel(lab)}|"
                lines.append(f"  {ids[p['path']]} -->{via_lbl} {ids[dst]}")
    for comp, dsts in nav_targets.items():
        nid = f"nav_{_mid(comp)}"
        lines.append(f'  {nid}[["{_mlabel(comp)}<br/><small>on every screen</small>"]]')
        lines += [f"  {nid} -.-> {ids[d]}" for d in sorted(dsts)]
    lines.append("  style start fill:#fbf1df,stroke:#c47f17")
    body = [f"# User flow: {a['name']}",
            "Screens Filament found and the links and navigation buttons that lead between "
            "them. Navigation shared by every screen (like a header) is shown once. Refreshed on "
            "every scan.", "```mermaid", *lines, "```"]
    if not edges and not nav_targets:
        body.append("No navigation between screens was found in the markup.")
    return f"User flow: {a['name']}", "\n".join(body), {
        "diagram": "user_flow", "screens": len(pages),
        "transitions": len(edges) + sum(len(v) for v in nav_targets.values())}


def component_view(a: dict) -> tuple[str, str, dict] | None:
    c = a["components"]
    if len(c["nodes"]) < 2:
        return None
    body = [f"# Component view: {a['name']}",
            "What this repo connects to, from manifests, env vars, connection strings, compose, "
            "Kubernetes and Terraform. Refreshed on every scan.",
            "```mermaid", c["mermaid"], "```"]
    return f"Component view: {a['name']}", "\n".join(body), {"diagram": "components"}


def diagrams_for(a: dict) -> list[str]:
    """The diagrams that apply to this repo."""
    return [g for g in DIAGRAMS if BUILDERS[g](a) is not None]


BUILDERS = {"agents_md": agents_md, "overview": overview, "architecture": architecture,
            "codeowners": codeowners, "api_contract": api_contract,
            "request_flows": request_flows, "user_flow": user_flow,
            "component_view": component_view}


def conventions(a: dict) -> list[str]:
    """Durable facts worth proposing as project memory."""
    facts = []
    if a["commands"].get("test"):
        facts.append(f"{a['name']}: run tests with `{a['commands']['test']}`.")
    if a["commands"].get("build"):
        facts.append(f"{a['name']}: build with `{a['commands']['build']}`.")
    lint = next((c for c in a["readiness"]["checks"] if c["id"] == "lint" and c["status"] == "pass"),
                None)
    if lint:
        facts.append(f"{a['name']}: lint/format config is {lint['detail'].replace('Found ', '')}.")
    if a["profile"]["frameworks"]:
        facts.append(f"{a['name']} is a {_kinds(a)} repo using {', '.join(a['profile']['frameworks'])}.")
    stores = [n["label"] for n in a["components"]["nodes"] if n["category"] in {"datastore", "cache",
                                                                               "queue"}]
    if stores:
        facts.append(f"{a['name']} depends on {', '.join(sorted(set(stores)))}.")
    return facts


def build(generator: str, analysis: dict) -> tuple[str, str, dict] | None:
    fn = BUILDERS.get(generator)
    return fn(analysis) if fn else None


def export_files(generated: dict[str, dict]) -> dict[str, str]:
    """Map generated drafts to the files they would become in the repo (for a later PR)."""
    names = {"agents_md": "AGENTS.md", "overview": "README.md", "architecture": "ARCHITECTURE.md",
             "codeowners": ".github/CODEOWNERS", "api_contract": "openapi.yaml"}
    out = {}
    for gen, art in generated.items():
        if gen == "api_contract" and art.get("data", {}).get("openapi"):
            out[names[gen]] = yaml.safe_dump(art["data"]["openapi"], sort_keys=False)
        elif gen in names:
            out[names[gen]] = art["content"].strip("`\n") if gen == "codeowners" else art["content"]
    return out


__all__ = ["GENERATORS", "build", "conventions", "export_files", "json"]
