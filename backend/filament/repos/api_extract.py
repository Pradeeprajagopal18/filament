"""Find a repo's API contract: read checked-in specs, or infer endpoints from the code.

Spec sources: OpenAPI 3 / Swagger 2 (YAML or JSON), AsyncAPI, gRPC .proto, GraphQL SDL.
Code inference: FastAPI, Flask, Django, Express / Fastify / Koa, NestJS, Spring, Go (net/http,
Gin, Echo, chi, Fiber). Every inferred endpoint carries its file and line, and an OpenAPI 3.1
document is generated from what was found so it can be browsed and exported.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

import yaml

from filament.readiness.scanner import Repo

METHODS = ["get", "post", "put", "patch", "delete", "options", "head"]
SKIP_PARTS = {"tests", "test", "__tests__", "spec", "fixtures", "examples", "e2e", "mocks"}


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _norm(path: str) -> str:
    path = re.sub(r":(\w+)", r"{\1}", path)  # express/gin :id -> {id}
    path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)  # flask <int:id> -> {id}
    path = re.sub(r"\{(\w+):[^}]+\}", r"{\1}", path)  # chi/spring {id:[0-9]+} -> {id}
    if not path.startswith("/"):
        path = "/" + path
    return re.sub(r"/{2,}", "/", path)


def _join(prefix: str, path: str) -> str:
    if not prefix:
        return _norm(path)
    tail = path.lstrip("/")
    return _norm(prefix.rstrip("/") + ("/" + tail if tail else ""))


def _endpoint(method, path, file, line, handler, framework, summary=""):
    path = _norm(path)
    return {"method": method.upper(), "path": path, "file": file, "line": line,
            "handler": handler, "framework": framework, "summary": summary,
            "params": [{"name": p, "in": "path"} for p in re.findall(r"\{(\w+)\}", path)],
            "tag": next((s for s in path.split("/") if s and not s.startswith("{")
                         and s not in {"api", "v1", "v2", "v3"}), "root")}


# -- checked-in specs ---------------------------------------------------------------------------
def _from_openapi(doc: dict, file: str) -> list[dict]:
    out = []
    base = ""
    if doc.get("swagger"):
        base = doc.get("basePath", "") or ""
    for path, ops in (doc.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in METHODS or not isinstance(op, dict):
                continue
            ep = _endpoint(method, _join(base, path), file, None, op.get("operationId") or "",
                           "openapi", op.get("summary") or (op.get("description") or "")[:120])
            if op.get("tags"):
                ep["tag"] = op["tags"][0]
            ep["params"] = [{"name": p.get("name"), "in": p.get("in")}
                            for p in op.get("parameters", []) if isinstance(p, dict)
                            and p.get("name")] or ep["params"]
            out.append(ep)
    return out


def _from_asyncapi(doc: dict, file: str) -> list[dict]:
    out = []
    for channel, spec in (doc.get("channels") or {}).items():
        for op in ("publish", "subscribe", "send", "receive"):
            if isinstance(spec, dict) and op in spec:
                ep = _endpoint(op.upper(), channel, file, None, "", "asyncapi",
                               (spec[op] or {}).get("summary", "") if isinstance(spec[op], dict)
                               else "")
                ep["path"] = channel  # channels are not HTTP paths
                out.append(ep)
    return out


def _from_proto(text: str, file: str) -> list[dict]:
    out = []
    for svc in re.finditer(r"service\s+(\w+)\s*\{(.*?)\n\}", text, re.S):
        for rpc in re.finditer(r"rpc\s+(\w+)\s*\(\s*(stream\s+)?(\w+)\s*\)\s*returns\s*\(\s*"
                               r"(stream\s+)?(\w+)\s*\)", svc.group(2)):
            ep = _endpoint("RPC", f"/{svc.group(1)}/{rpc.group(1)}", file,
                           _line(text, svc.start(2) + rpc.start()), rpc.group(1), "grpc",
                           f"{rpc.group(3)} → {rpc.group(5)}")
            ep["tag"] = svc.group(1)
            out.append(ep)
    return out


def _from_graphql(text: str, file: str) -> list[dict]:
    out = []
    for block in re.finditer(r"type\s+(Query|Mutation|Subscription)\s*\{(.*?)\}", text, re.S):
        for field in re.finditer(r"^\s*(\w+)\s*(\([^)]*\))?\s*:\s*([^\n#]+)", block.group(2), re.M):
            ep = _endpoint(block.group(1).upper(), f"/{field.group(1)}", file,
                           _line(text, block.start(2) + field.start()), field.group(1), "graphql",
                           f"returns {field.group(3).strip()}")
            ep["tag"] = block.group(1)
            out.append(ep)
    return out


# -- inference from code -----------------------------------------------------------------------
PY_ROUTE = re.compile(r"@(\w+)\.(get|post|put|patch|delete|options|head|api_route)\(\s*"
                      r"[rfb]?[\"']([^\"']*)[\"']")
FLASK_ROUTE = re.compile(r"@(\w+)\.route\(\s*[\"']([^\"']+)[\"'](?:[^)]*methods\s*=\s*\[([^\]]*)\])?")
DJANGO_PATH = re.compile(r"\b(?:re_)?path\(\s*r?[\"']([^\"']*)[\"']\s*,\s*([\w.]+)")
PY_DEF = re.compile(r"(?:async\s+)?def\s+(\w+)\s*\([^)]*\)[^:]*:\s*(?:[\"']{3}([^\n\"']*))?", re.S)
PY_PREFIX = re.compile(r"(\w+)\s*=\s*APIRouter\(([^)]*)\)")
BLUEPRINT = re.compile(r"(\w+)\s*=\s*Blueprint\(([^)]*)\)")
INCLUDE = re.compile(r"include_router\(\s*([\w.]+)[^)]*prefix\s*=\s*[\"']([^\"']+)[\"']")
JS_ROUTE = re.compile(r"\b(\w+)\.(get|post|put|patch|"
                      r"delete|options|head|all)\(\s*[`'\"]([^`'\"]+)[`'\"]")
JS_USE = re.compile(r"\b\w+\.(?:use|register|route)\(\s*[`'\"]([^`'\"]+)[`'\"]\s*,\s*(\w+)")
JS_ROUTER_VAR = re.compile(r"\b(\w+)\s*=\s*(?:new\s+)?(?:express\.|koa-?)?Router\(")
JS_ROUTE_OWNERS = re.compile(r"^(app|router|api|server|fastify|\w+Router|\w+Routes|routes)$")
NEST_CTRL = re.compile(r"@Controller\(\s*['\"`]?([^'\"`)]*)['\"`]?\s*\)")
NEST_ROUTE = re.compile(r"@(Get|Post|Put|Patch|Delete|Options|Head|All)\(\s*['\"`]?([^'\"`)]*)"
                        r"['\"`]?\s*\)\s*(?:@\w+\([^)]*\)\s*)*(?:async\s+)?(\w+)\s*\(")
SPRING_CLASS = re.compile(r"@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\"[^)]*\)"
                          r"\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public\s+)?(?:final\s+)?class")
SPRING_ROUTE = re.compile(r"@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?"
                          r"(?:\{\s*)?\"([^\"]*)\"[^)]*\)[\s\S]{0,300}?\s(\w+)\s*\(")
SPRING_REQ = re.compile(r"@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\"\s*,\s*"
                        r"method\s*=\s*RequestMethod\.(\w+)[^)]*\)[\s\S]{0,300}?\s(\w+)\s*\(")
GO_ROUTE = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\(\s*\"([^\"]+)\"\s*,"
                      r"\s*([\w.]+)")
GO_HANDLE = re.compile(r"(?:HandleFunc|Handle)\(\s*\"(?:(GET|POST|PUT|PATCH|DELETE)\s+)?([^\"]+)\""
                       r"\s*,\s*([\w.]+)")


def _code_files(repo: Repo, suffixes: tuple[str, ...]) -> list[str]:
    return [str(p) for p in repo.files if p.suffix in suffixes
            and not (set(p.parts[:-1]) & SKIP_PARTS)
            and not p.name.startswith(("test_", "conftest"))
            and not re.search(r"\.(test|spec)\.[jt]sx?$", p.name)]


def _infer_python(repo: Repo) -> list[dict]:
    files = _code_files(repo, (".py",))
    texts = {f: repo.read(f, 300_000) for f in files}
    include_prefix: dict[str, str] = {}
    loose_prefixes = set()
    for text in texts.values():
        for m in INCLUDE.finditer(text):
            target = m.group(1).split(".")[0]
            include_prefix[target] = m.group(2)
            loose_prefixes.add(m.group(2))
    out = []
    for f, text in texts.items():
        if not re.search(r"fastapi|flask|APIRouter|Blueprint|django", text):
            continue
        module = PurePosixPath(f).stem
        prefixes = {}
        for m in list(PY_PREFIX.finditer(text)) + list(BLUEPRINT.finditer(text)):
            pm = re.search(r"(?:url_)?prefix\s*=\s*[\"']([^\"']*)[\"']", m.group(2))
            prefixes[m.group(1)] = pm.group(1) if pm else ""
        outer = include_prefix.get(module, "")
        if not outer and len(loose_prefixes) == 1 and prefixes:
            outer = next(iter(loose_prefixes))  # routers included in a loop with one prefix
        framework = "FastAPI" if "fastapi" in text.lower() or "APIRouter" in text else "Flask"
        for m in PY_ROUTE.finditer(text):
            if m.group(2) == "api_route":
                continue
            d = PY_DEF.search(text, m.end())
            path = _join(outer, _join(prefixes.get(m.group(1), ""), m.group(3)))
            out.append(_endpoint(m.group(2), path, f, _line(text, m.start()),
                                 d.group(1) if d else "", framework,
                                 (d.group(2) or "").strip() if d else ""))
        for m in FLASK_ROUTE.finditer(text):
            d = PY_DEF.search(text, m.end())
            methods = re.findall(r"[\"'](\w+)[\"']", m.group(3) or "") or ["GET"]
            for method in methods:
                path = _join(outer, _join(prefixes.get(m.group(1), ""), m.group(2)))
                out.append(_endpoint(method, path, f, _line(text, m.start()),
                                     d.group(1) if d else "", "Flask",
                                     (d.group(2) or "").strip() if d else ""))
        if PurePosixPath(f).name == "urls.py":
            for m in DJANGO_PATH.finditer(text):
                out.append(_endpoint("ANY", m.group(1) or "/", f, _line(text, m.start()),
                                     m.group(2), "Django"))
    return out


def _infer_js(repo: Repo) -> list[dict]:
    out = []
    files = _code_files(repo, (".js", ".ts", ".mjs", ".cjs"))
    texts = {f: repo.read(f, 300_000) for f in files}
    mounts: dict[str, str] = {}
    for text in texts.values():
        for m in JS_USE.finditer(text):
            mounts[m.group(2).lower()] = m.group(1)
    for f, text in texts.items():
        if "@Controller" in text:
            ctrl = NEST_CTRL.search(text)
            base = ctrl.group(1) if ctrl else ""
            for m in NEST_ROUTE.finditer(text):
                out.append(_endpoint(m.group(1), _join(base, m.group(2)), f,
                                     _line(text, m.start()), m.group(3), "NestJS"))
            continue
        if not re.search(r"express|fastify|koa|Router\(|hono", text):
            continue
        stem = PurePosixPath(f).stem.lower()
        base = next((p for name, p in mounts.items() if name.startswith(stem) or stem in name), "")
        router_vars = set(JS_ROUTER_VAR.findall(text))
        for m in JS_ROUTE.finditer(text):
            owner = m.group(1)
            if owner not in router_vars and not JS_ROUTE_OWNERS.match(owner):
                continue
            if m.group(3).startswith(("http", ".")) or "/" not in m.group(3) and m.group(3) != "*":
                continue
            handler = re.search(r",\s*(?:async\s*)?(\w+)", text[m.end():m.end() + 120])
            prefix = "" if owner == "app" else mounts.get(owner.lower(), base)
            out.append(_endpoint("ANY" if m.group(2) == "all" else m.group(2),
                                 _join(prefix, m.group(3)), f, _line(text, m.start()),
                                 handler.group(1) if handler else "", "Express"))
    return out


def _infer_java(repo: Repo) -> list[dict]:
    out = []
    for f in _code_files(repo, (".java", ".kt")):
        text = repo.read(f, 300_000)
        if "Mapping" not in text:
            continue
        cls = SPRING_CLASS.search(text)
        base = cls.group(1) if cls else ""
        for m in SPRING_ROUTE.finditer(text):
            out.append(_endpoint(m.group(1), _join(base, m.group(2)), f, _line(text, m.start()),
                                 m.group(3), "Spring"))
        for m in SPRING_REQ.finditer(text):
            if cls and m.start() == cls.start():
                continue
            out.append(_endpoint(m.group(2), _join(base, m.group(1)), f, _line(text, m.start()),
                                 m.group(3), "Spring"))
    return out


def _infer_go(repo: Repo) -> list[dict]:
    out = []
    for f in _code_files(repo, (".go",)):
        if f.endswith("_test.go"):
            continue
        text = repo.read(f, 300_000)
        for m in GO_ROUTE.finditer(text):
            out.append(_endpoint(m.group(1), m.group(2), f, _line(text, m.start()), m.group(3),
                                 "Go"))
        for m in GO_HANDLE.finditer(text):
            out.append(_endpoint(m.group(1) or "ANY", m.group(2), f, _line(text, m.start()),
                                 m.group(3), "net/http"))
    return out


def to_openapi(title: str, endpoints: list[dict], inferred: bool) -> dict:
    paths: dict = {}
    for ep in endpoints:
        if ep["method"].lower() not in METHODS + ["any"]:
            continue
        methods = METHODS[:5] if ep["method"] == "ANY" else [ep["method"].lower()]
        for method in methods:
            op = {"operationId": ep["handler"] or f"{method}_{ep['path']}",
                  "summary": ep["summary"] or f"{method.upper()} {ep['path']}",
                  "tags": [ep["tag"]],
                  "responses": {"200": {"description": "Successful response"}}}
            if ep["params"]:
                op["parameters"] = [{"name": p["name"], "in": p["in"] or "path",
                                     "required": (p["in"] or "path") == "path",
                                     "schema": {"type": "string"}} for p in ep["params"]]
            if ep.get("file"):
                op["x-source"] = f"{ep['file']}:{ep['line']}" if ep.get("line") else ep["file"]
            paths.setdefault(ep["path"], {})[method] = op
    return {"openapi": "3.1.0",
            "info": {"title": title, "version": "inferred" if inferred else "from-spec",
                     "description": ("Generated by Filament from the code. Review request and "
                                     "response schemas before publishing." if inferred else
                                     "Collected by Filament from the repository's contracts.")},
            "paths": dict(sorted(paths.items()))}


def extract(repo: Repo, title: str = "API") -> dict:
    endpoints: list[dict] = []
    specs = []
    for p in repo.files:
        f, name = str(p), p.name.lower()
        if set(p.parts[:-1]) & {"node_modules", "fixtures"}:
            continue
        if re.match(r"(openapi|swagger|asyncapi)[\w.-]*\.(ya?ml|json)$", name):
            try:
                text = repo.read(f)
                doc = json.loads(text) if name.endswith(".json") else yaml.safe_load(text)
            except (json.JSONDecodeError, yaml.YAMLError):
                continue
            if isinstance(doc, dict):
                specs.append(f)
                endpoints += _from_asyncapi(doc, f) if "asyncapi" in doc else _from_openapi(doc, f)
        elif p.suffix == ".proto":
            specs.append(f)
            endpoints += _from_proto(repo.read(f), f)
        elif p.suffix in {".graphql", ".gql"}:
            specs.append(f)
            endpoints += _from_graphql(repo.read(f), f)

    source = "spec" if endpoints else "none"
    if not endpoints:
        endpoints = _infer_python(repo) + _infer_js(repo) + _infer_java(repo) + _infer_go(repo)
        source = "inferred" if endpoints else "none"
    seen, unique = set(), []
    for ep in endpoints:
        key = (ep["method"], ep["path"])
        if key not in seen:
            seen.add(key)
            unique.append(ep)
    unique.sort(key=lambda e: (e["path"], e["method"]))
    return {"source": source, "specs": specs, "endpoints": unique,
            "openapi": to_openapi(title, unique, source == "inferred") if unique else None}
