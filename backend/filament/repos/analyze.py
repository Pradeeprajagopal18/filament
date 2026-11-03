"""One pass over a repository: profile, kind-aware readiness, API, UI and component views."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from filament.readiness.scanner import Repo, checkout, grade_for
from filament.repos import api_extract, consumers, deps, ui_extract
from filament.repos.checks import run_checks, score
from filament.repos.manifests import parse
from filament.repos.profile import detect


def repo_name(repo: str) -> str:
    name = PurePosixPath(repo.rstrip("/").removesuffix(".git")).name
    return name or "repo"


def commands(repo: Repo) -> dict[str, str]:
    """Build/test/run commands a human or agent would use, from what the repo declares."""
    out: dict[str, str] = {}
    if repo.exists("Makefile"):
        targets = re.findall(r"^([a-zA-Z][\w-]*):", repo.read("Makefile"), re.M)
        for t in ("setup", "install", "build", "test", "lint", "run", "dev", "deploy", "plan"):
            if t in targets:
                out[t] = f"make {t}"
    if repo.exists("package.json"):
        try:
            scripts = json.loads(repo.read("package.json")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for t in ("install", "build", "test", "lint", "dev", "start"):
            if t in scripts and t not in out:
                out[t] = f"npm run {t}" if t not in {"install", "test", "start"} else f"npm {t}"
        out.setdefault("install", "npm ci")
    if repo.exists("pyproject.toml") or repo.exists("requirements.txt"):
        out.setdefault("install", "pip install -e ." if repo.exists("pyproject.toml")
                       else "pip install -r requirements.txt")
        if "pytest" in repo.read("pyproject.toml") or any(p.name.startswith("test_")
                                                         for p in repo.files):
            out.setdefault("test", "pytest")
    if repo.exists("go.mod"):
        out.setdefault("build", "go build ./...")
        out.setdefault("test", "go test ./...")
    if any(p.suffix == ".tf" for p in repo.files):
        out.setdefault("validate", "terraform init -backend=false && terraform validate")
        out.setdefault("plan", "terraform plan")
    if any(p.name == "Chart.yaml" for p in repo.files):
        out.setdefault("lint", "helm lint <chart>")
    return out


def layout(repo: Repo) -> list[dict]:
    counts: dict[str, int] = {}
    for p in repo.files:
        if len(p.parts) > 1 and not p.parts[0].startswith("."):
            counts[p.parts[0]] = counts.get(p.parts[0], 0) + 1
    return [{"path": k, "files": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])][:15]


def code_dir(repo_ref: str):
    """Per-repo folder for Graphify's output and incremental cache (outside the repo)."""
    import hashlib
    from pathlib import Path

    from filament.config import get_settings

    key = hashlib.sha1(repo_ref.encode()).hexdigest()[:16]
    return Path(get_settings().data_dir).resolve() / "codegraph" / key


def _code_graph(indexed, root, repo_ref: str, endpoints: list[dict], build: bool) -> tuple[dict, dict | None]:
    from filament.config import get_settings
    from filament.repos import codegraph

    settings = get_settings()
    if not build or not settings.code_graph:
        return {"engine": None, "reason": "disabled"}, None
    if not codegraph.engine_path():
        return {"engine": None, "reason": "Graphify is not installed (pip install graphifyy)"}, None
    try:
        path = codegraph.build(root, code_dir(repo_ref), settings.code_graph_timeout_s)
        graph = codegraph.load_file(path)
        return codegraph.summary(graph, endpoints, indexed.read), graph
    except codegraph.CodeGraphError as exc:
        return {"engine": "graphify", "error": str(exc)}, None


def analyze_path(root, repo_ref: str, code_graph: bool = True) -> dict:
    indexed = Repo(root)
    dependencies = parse(indexed)
    profile = detect(indexed, dependencies)
    checks = run_checks(indexed, profile)
    name = repo_name(repo_ref)
    api = api_extract.extract(indexed, name) if (
        profile.has("api") or profile.has("backend")) else {
        "source": "none", "specs": [], "endpoints": [], "openapi": None, "schemas": {},
        "drift": None, "auth": []}
    if api["endpoints"] and not profile.has("api"):
        from filament.repos.profile import Kind

        profile.kinds.append(Kind("api", 0.7, [f"{len(api['endpoints'])} HTTP endpoints in code"]))
    ui = ui_extract.extract(indexed) if (profile.has("frontend") or profile.has("mobile")) else {
        "pages": [], "components": []}
    components = deps.extract(indexed, name, dependencies)
    calls = consumers.extract(indexed) if not profile.kind_names == ["infrastructure"] else []
    config_files = [str(p) for p in indexed.files
                    if p.suffix in {".yaml", ".yml", ".json", ".toml", ".env", ".properties",
                                    ".ini", ".conf"} and not p.name.startswith(".")
                    and p.name not in {"package.json", "package-lock.json", "tsconfig.json"}
                    ][:300] if profile.has("config") else []
    from filament.repos import datamodel

    code, graph = _code_graph(indexed, root, repo_ref, api["endpoints"], code_graph)
    if graph is not None:
        from filament.repos.codegraph import align_services

        align_services(code, components)
    data_model = datamodel.extract(indexed)
    s = score(checks)
    return {
        "repo": repo_ref,
        "name": name,
        "profile": profile.to_dict(),
        "readiness": {"score": s, "grade": grade_for(s),
                      "checks": [c.__dict__ for c in checks]},
        "commands": commands(indexed),
        "layout": layout(indexed),
        "api": api,
        "ui": ui,
        "components": components,
        "client_calls": calls,
        "code": code,
        "_code_graph": graph,
        "data_model": data_model,
        "config_files": config_files,
        "dependencies": [{"ecosystem": d.ecosystem, "name": d.name, "file": d.file, "dev": d.dev}
                         for d in dependencies][:400],
    }


def head_commit(root) -> str | None:
    import subprocess

    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def analyze(repo_ref: str) -> dict:
    with checkout(repo_ref) as root:
        result = analyze_path(root, repo_ref)
        result["commit"] = head_commit(root)
        return result
