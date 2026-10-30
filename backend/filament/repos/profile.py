"""Work out what kind of repository this is. A repo can be several kinds at once.

Kinds: frontend, mobile, backend, api, infrastructure, config, library, data, docs, monorepo.
Every kind comes with the evidence that triggered it, so the result can be explained.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from filament.readiness.scanner import Repo
from filament.repos.manifests import Dependency, manifest_files, names, parse

FRONTEND_LIBS = {"react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt",
                 "@angular/core": "Angular", "svelte": "Svelte", "@sveltejs/kit": "SvelteKit",
                 "solid-js": "Solid", "preact": "Preact", "@remix-run/react": "Remix"}
MOBILE_LIBS = {"react-native": "React Native", "expo": "Expo", "@ionic/core": "Ionic"}
BACKEND_LIBS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette",
    "aiohttp": "aiohttp", "tornado": "Tornado", "express": "Express", "fastify": "Fastify",
    "@nestjs/core": "NestJS", "koa": "Koa", "hono": "Hono", "@hapi/hapi": "hapi",
    "github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo/v4": "Echo",
    "github.com/go-chi/chi/v5": "chi", "github.com/gofiber/fiber/v2": "Fiber",
    "github.com/gorilla/mux": "gorilla/mux", "spring-boot-starter-web": "Spring Boot",
    "spring-boot-starter-webflux": "Spring WebFlux", "quarkus-resteasy": "Quarkus",
    "micronaut-http-server-netty": "Micronaut", "actix-web": "Actix", "axum": "Axum",
    "rails": "Rails", "sinatra": "Sinatra", "laravel/framework": "Laravel",
}
GRPC_LIBS = {"grpcio", "@grpc/grpc-js", "google.golang.org/grpc", "grpc-netty", "tonic"}
DATA_LIBS = {"apache-airflow": "Airflow", "dbt-core": "dbt", "pyspark": "Spark",
             "pandas": "pandas", "dagster": "Dagster", "prefect": "Prefect"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rs", ".rb", ".php",
            ".cs", ".swift", ".dart", ".scala", ".vue", ".svelte"}
CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".env", ".properties", ".conf",
              ".hcl", ".cfg", ".xml"}
ENV_DIRS = {"dev", "development", "staging", "stage", "qa", "uat", "prod", "production",
            "environments", "envs", "overlays"}


@dataclass
class Kind:
    kind: str
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class Profile:
    kinds: list[Kind]
    frameworks: list[str]
    languages: list[str]
    packages: list[dict]  # monorepo sub-packages: {"path", "kinds"}
    manifests: list[str]
    file_count: int

    @property
    def kind_names(self) -> list[str]:
        return [k.kind for k in self.kinds]

    def has(self, kind: str) -> bool:
        return kind in self.kind_names

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_names"] = self.kind_names
        return d


LANG_BY_EXT = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
               ".jsx": "JavaScript", ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".rs": "Rust",
               ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift", ".dart": "Dart",
               ".tf": "HCL", ".vue": "Vue", ".scala": "Scala"}


def _languages(repo: Repo) -> list[str]:
    counts: dict[str, int] = {}
    for p in repo.files:
        lang = LANG_BY_EXT.get(p.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda x: -x[1])][:5]


def _k8s_manifests(repo: Repo) -> list[str]:
    hits = []
    for p in repo.files:
        if p.suffix.lower() in {".yaml", ".yml"} and len(hits) < 20:
            head = repo.read(str(p), limit=4000)
            if re.search(r"^apiVersion:", head, re.M) and re.search(
                    r"^kind:\s*(Deployment|Service|StatefulSet|Ingress|ConfigMap|CronJob|"
                    r"DaemonSet|Job|HelmRelease|Kustomization)\b", head, re.M):
                hits.append(str(p))
    return hits


def _ansible(repo: Repo) -> list[str]:
    hits = [str(p) for p in repo.files if p.parts and p.parts[0] in {"roles", "playbooks"}
            and p.suffix in {".yml", ".yaml"}]
    for p in repo.files:
        if p.suffix in {".yml", ".yaml"} and p.name not in {"docker-compose.yml"}:
            head = repo.read(str(p), limit=2000)
            if re.search(r"^\s*-?\s*hosts:\s*\S+", head, re.M) and "tasks:" in head:
                hits.append(str(p))
    return hits[:10]


def detect(repo: Repo, deps: list[Dependency] | None = None) -> Profile:
    deps = parse(repo) if deps is None else deps
    dep_names = names(deps)
    kinds: dict[str, Kind] = {}
    frameworks: list[str] = []

    def add(kind: str, confidence: float, evidence: str) -> None:
        k = kinds.setdefault(kind, Kind(kind, 0.0))
        k.confidence = max(k.confidence, confidence)
        if evidence not in k.evidence and len(k.evidence) < 6:
            k.evidence.append(evidence)

    def framework_hits(table: dict[str, str]) -> list[tuple[str, str]]:
        return [(dep, label) for dep, label in table.items() if dep in dep_names]

    for dep, label in framework_hits(FRONTEND_LIBS):
        frameworks.append(label)
        add("frontend", 0.9, f"depends on {dep}")
    for dep, label in framework_hits(MOBILE_LIBS):
        frameworks.append(label)
        add("mobile", 0.9, f"depends on {dep}")
    if repo.exists_anywhere("pubspec.yaml") and "flutter" in dep_names:
        frameworks.append("Flutter")
        add("mobile", 0.9, "Flutter pubspec.yaml")
    for dep, label in framework_hits(BACKEND_LIBS):
        frameworks.append(label)
        add("backend", 0.9, f"depends on {dep}")
    if repo.exists("go.mod") and any(p.name == "main.go" for p in repo.files):
        if any("net/http" in repo.read(str(p), 50_000) for p in repo.files if p.suffix == ".go"):
            add("backend", 0.7, "Go net/http server")
    for dep, label in framework_hits(DATA_LIBS):
        frameworks.append(label)
        add("data", 0.7, f"depends on {dep}")
    if repo.exists_anywhere("dbt_project.yml"):
        add("data", 0.9, "dbt_project.yml")
    notebooks = [str(p) for p in repo.files if p.suffix == ".ipynb"]
    if len(notebooks) >= 2:
        add("data", 0.6, f"{len(notebooks)} notebooks")

    # API: a contract, gRPC, or an HTTP backend (routes are confirmed later by extraction).
    specs = [str(p) for p in repo.files if re.match(r"(openapi|swagger|asyncapi)[\w.-]*\.(ya?ml|json)$",
                                                     p.name.lower())]
    protos = [str(p) for p in repo.files if p.suffix == ".proto"]
    graphql = [str(p) for p in repo.files if p.suffix in {".graphql", ".gql"}]
    if specs:
        add("api", 0.95, f"API spec {specs[0]}")
    if protos:
        add("api", 0.85, f"{len(protos)} .proto files")
        if dep_names & GRPC_LIBS:
            frameworks.append("gRPC")
    if graphql:
        add("api", 0.7, f"GraphQL schema {graphql[0]}")
    if "backend" in kinds:
        add("api", 0.6, "HTTP server framework")

    # Infrastructure.
    tf = [str(p) for p in repo.files if p.suffix == ".tf"]
    if tf:
        frameworks.append("Terraform")
        add("infrastructure", 0.95, f"{len(tf)} Terraform files")
    charts = [str(p) for p in repo.files if p.name == "Chart.yaml"]
    if charts:
        frameworks.append("Helm")
        add("infrastructure", 0.9, f"Helm chart {charts[0]}")
    if repo.exists_anywhere("kustomization.yaml", "kustomization.yml"):
        frameworks.append("Kustomize")
        add("infrastructure", 0.85, "kustomization.yaml")
    k8s = _k8s_manifests(repo)
    if k8s:
        frameworks.append("Kubernetes")
        add("infrastructure", 0.8, f"{len(k8s)} Kubernetes manifests")
    ansible = _ansible(repo)
    if ansible:
        frameworks.append("Ansible")
        add("infrastructure", 0.8, f"Ansible {ansible[0]}")
    if repo.exists_anywhere("Pulumi.yaml"):
        frameworks.append("Pulumi")
        add("infrastructure", 0.9, "Pulumi.yaml")
    cfn = [str(p) for p in repo.files if p.suffix in {".yaml", ".yml", ".json"}
           and "AWSTemplateFormatVersion" in repo.read(str(p), 3000)]
    if cfn:
        frameworks.append("CloudFormation")
        add("infrastructure", 0.9, f"CloudFormation {cfn[0]}")

    # Frontend without a framework dependency: plain web app.
    has_ui_files = any(p.suffix in {".tsx", ".jsx", ".vue", ".svelte"} for p in repo.files)
    if "frontend" not in kinds and has_ui_files and repo.exists_anywhere("index.html"):
        add("frontend", 0.6, "UI components and index.html")

    # Docs sites.
    if repo.exists_anywhere("mkdocs.yml", "docusaurus.config.js", "docusaurus.config.ts",
                            "_config.yml", "conf.py") and not kinds:
        add("docs", 0.7, "documentation site config")

    code_files = [p for p in repo.files if p.suffix.lower() in CODE_EXT]
    cfg_files = [p for p in repo.files if p.suffix.lower() in CONFIG_EXT]
    # Config repo: mostly config files, little code, often split by environment.
    env_dirs = {part.lower() for p in cfg_files for part in p.parts[:-1]} & ENV_DIRS
    if cfg_files and len(code_files) <= 3 and "infrastructure" not in kinds \
            and len(cfg_files) >= max(3, 0.5 * len(repo.files)):
        ev = f"{len(cfg_files)} config files, {len(code_files)} code files"
        add("config", 0.8 if env_dirs else 0.6, ev)
        if env_dirs:
            add("config", 0.8, f"environment folders: {', '.join(sorted(env_dirs))}")

    # Library: a package with code but no app, service or infra signals.
    if code_files and not kinds.keys() & {"frontend", "mobile", "backend", "infrastructure",
                                          "config", "data"}:
        pkg = json.loads(repo.read("package.json") or "{}") if repo.exists("package.json") else {}
        if repo.exists("pyproject.toml", "setup.py", "cargo.toml", "go.mod") or \
                any(k in pkg for k in ("main", "exports", "module")):
            add("library", 0.7, "package without an app, service or infrastructure entry point")

    # Monorepo: several manifests in different top-level folders, or declared workspaces.
    mans = manifest_files(repo)
    roots = sorted({str(PurePosixPath(m).parent) for m in mans if "/" in m})
    declared = repo.exists("pnpm-workspace.yaml", "lerna.json", "go.work", "turbo.json", "nx.json")
    if repo.exists("package.json") and "workspaces" in repo.read("package.json"):
        declared = declared or "package.json workspaces"
    top = {r.split("/")[0] for r in roots}
    packages = []
    if declared or len(top) >= 2:
        add("monorepo", 0.9 if declared else 0.7,
            f"workspaces declared in {declared}" if declared else f"{len(roots)} packages")
        for r in roots[:12]:
            sub_deps = {d.name for d in deps if d.file.startswith(r + "/")}
            sub_kinds = []
            if sub_deps & FRONTEND_LIBS.keys():
                sub_kinds.append("frontend")
            if sub_deps & BACKEND_LIBS.keys():
                sub_kinds.append("backend")
            packages.append({"path": r, "kinds": sub_kinds or ["package"]})
    if any(p.parts and p.parts[0] in {"terraform", "infra", "deploy", "helm", "k8s"}
           for p in repo.files) and "infrastructure" in kinds and code_files:
        kinds["infrastructure"].evidence.append("infrastructure folder alongside application code")

    order = ["frontend", "mobile", "backend", "api", "infrastructure", "config", "data",
             "library", "docs", "monorepo"]
    ordered = sorted(kinds.values(), key=lambda k: order.index(k.kind))
    return Profile(kinds=ordered, frameworks=sorted(set(frameworks)), languages=_languages(repo),
                   packages=packages, manifests=mans, file_count=len(repo.files))
