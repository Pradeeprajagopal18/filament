"""API documentation written in code: the annotations spec generators read.

Many services never check in an OpenAPI file: a generator builds it from annotations at build
time or serves it at runtime. Filament reads the same annotations, so the contract it shows
matches what the generator would publish:

- Go, swaggo:          // @Summary, @Description, @Tags, @Param, @Success, @Failure,
                       // @Router, @Security, @BasePath (endpoints come from @Router itself)
- Java, springdoc:     @Operation, @ApiResponse(s), @Parameter, @Tag, @Hidden, @SecurityRequirement
- Java, springfox:     @ApiOperation, @ApiResponse(code, message), @ApiParam, @Api(tags), @ApiIgnore
- NestJS, @nestjs/swagger: @ApiOperation, @ApiResponse, @Api<Status>Response, @ApiTags,
                       @ApiBearerAuth, @ApiExcludeEndpoint / @ApiExcludeController
- Python:              FastAPI summary/description/tags/responses/deprecated/include_in_schema
                       and docstrings; drf-spectacular @extend_schema

It also says which generator a repo uses and where that generator serves the spec at runtime,
so a repo can be pointed at the live document (see `live_spec`).
"""

from __future__ import annotations

import re

from filament.readiness.scanner import Repo

STATUS_NAMES = {"Ok": "200", "Created": "201", "Accepted": "202", "NoContent": "204",
                "BadRequest": "400", "Unauthorized": "401", "Forbidden": "403",
                "NotFound": "404", "MethodNotAllowed": "405", "Conflict": "409",
                "Gone": "410", "PayloadTooLarge": "413", "UnprocessableEntity": "422",
                "TooManyRequests": "429", "InternalServerError": "500",
                "ServiceUnavailable": "503"}
GO_TYPES = {"string": "string", "bool": "boolean", "int": "integer", "int32": "integer",
            "int64": "integer", "uint": "integer", "uint32": "integer", "uint64": "integer",
            "float32": "number", "float64": "number", "integer": "integer", "number": "number",
            "boolean": "boolean", "file": "string"}


def _s(m: re.Match | None, i: int = 1) -> str:
    return (m.group(i) if m else "") or ""


# -- generators ------------------------------------------------------------------------------------
def generators(repo: Repo, deps: set[str]) -> list[dict]:
    """Spec generators in use and the path each serves the spec on by default (or as configured)."""
    out = []

    def add(name: str, path: str, evidence: str):
        out.append({"generator": name, "spec_path": path, "evidence": evidence})

    springdoc = next((d for d in deps if d.startswith("springdoc-openapi")), None)
    if springdoc:
        path = "/v3/api-docs"
        for f in [str(p) for p in repo.files if p.name.startswith("application.")][:5]:
            m = re.search(r"springdoc\.api-docs\.path\s*[=:]\s*(\S+)|api-docs:\s*\n\s*path:\s*(\S+)",
                          repo.read(f))
            if m:
                path = m.group(1) or m.group(2)
        add("springdoc", path, springdoc)
    if any(d.startswith("springfox") for d in deps):
        add("springfox", "/v2/api-docs", "springfox")
    if any(d in deps for d in ("github.com/swaggo/swag", "github.com/swaggo/gin-swagger",
                               "github.com/swaggo/http-swagger", "github.com/swaggo/echo-swagger")):
        add("swaggo", "/swagger/doc.json", "github.com/swaggo/swag")
    if "@nestjs/swagger" in deps:
        path = "/api-json"
        for f in [str(p) for p in repo.files if p.name in {"main.ts", "app.ts", "main.js"}]:
            m = re.search(r"SwaggerModule\.setup\(\s*['\"`]/?([^'\"`]+)", repo.read(f))
            if m:
                path = f"/{m.group(1).strip('/')}-json"
        add("nestjs-swagger", path, "@nestjs/swagger")
    if "fastapi" in deps:
        path = "/openapi.json"
        for f in [str(p) for p in repo.files if p.suffix == ".py"][:200]:
            m = re.search(r"FastAPI\([^)]*openapi_url\s*=\s*['\"]([^'\"]+)", repo.read(f))
            if m:
                path = m.group(1)
                break
        add("fastapi", path, "fastapi")
    if "drf-spectacular" in deps:
        add("drf-spectacular", "/api/schema/", "drf-spectacular")
    return out


# -- Go: swaggo ------------------------------------------------------------------------------------
SWAG = re.compile(r"^\s*//\s*@(\w+)\s*(.*?)\s*$")
GO_FUNC = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")
SWAG_PARAM = re.compile(r'(\S+)\s+(\w+)\s+(\S+)\s+(true|false)\s*(?:"([^"]*)")?')
SWAG_RESP = re.compile(r'(\d{3}|default)\s*(?:\{(\w+)\}\s*)?([\w.\[\]{}*]+)?\s*(?:"([^"]*)")?')
SWAG_ROUTER = re.compile(r"(\S+)\s+\[(\w+)\]")


def go_models(texts: dict[str, str]) -> dict[str, dict]:
    """Go structs as JSON schemas (json tags, binding/validate required)."""
    out: dict[str, dict] = {}
    for f, text in texts.items():
        for m in re.finditer(r"^type\s+(\w+)\s+struct\s*\{(.*?)^\}", text, re.M | re.S):
            props, required = {}, []
            for line in m.group(2).splitlines():
                fm = re.match(r"\s*(\w+)\s+([\w.*\[\]]+)\s*(`[^`]*`)?", line)
                if not fm or not fm.group(1)[0].isupper():
                    continue
                tag = fm.group(3) or ""
                jm = re.search(r'json:"([^",]*)', tag)
                name = jm.group(1) if jm and jm.group(1) else fm.group(1)
                if name == "-":
                    continue
                props[name] = _go_schema(fm.group(2), set())
                if re.search(r'(binding|validate):"[^"]*required', tag):
                    required.append(name)
            schema: dict = {"type": "object", "properties": props, "x-source": f}
            if required:
                schema["required"] = required
            out[m.group(1)] = schema
    known = set(out)
    for schema in out.values():  # second pass: refs to other structs
        for prop, s in list(schema["properties"].items()):
            schema["properties"][prop] = _relink(s, known)
    return out


def _go_schema(t: str, models: set[str]) -> dict:
    t = t.lstrip("*")
    if t.startswith("[]"):
        return {"type": "array", "items": _go_schema(t[2:], models)}
    if t.startswith("map["):
        return {"type": "object"}
    if t in {"time.Time"}:
        return {"type": "string", "format": "date-time"}
    base = t.split(".")[-1]
    if base in GO_TYPES:
        return {"type": GO_TYPES[base]}
    return {"$ref": f"#/components/schemas/{base}"} if base in models else \
        {"type": "object", "x-go-type": base}


def _relink(s: dict, known: set[str]) -> dict:
    if s.get("type") == "array":
        return {"type": "array", "items": _relink(s["items"], known)}
    if s.get("x-go-type") in known:
        return {"$ref": f"#/components/schemas/{s['x-go-type']}"}
    return {k: v for k, v in s.items() if k != "x-go-type"}


def _swag_type(kind: str, t: str, models: set[str]) -> dict:
    t = (t or "").strip()
    if kind == "array":
        return {"type": "array", "items": _go_schema(t, models)}
    if kind in {"string", "integer", "number", "boolean"} and not t:
        return {"type": kind}
    return _go_schema(t, models) if t else {"type": "object"}


def swaggo(repo: Repo, models: set[str]) -> list[dict]:
    """Endpoints documented with swaggo comments. @Router is the route itself."""
    files = [str(p) for p in repo.files if p.suffix == ".go" and not str(p).endswith("_test.go")]
    base = ""
    out = []
    for f in files:
        text = repo.read(f, 300_000)
        if "@Router" not in text and "@BasePath" not in text:
            continue
        lines = text.splitlines()
        block: list[tuple[int, str, str]] = []
        for i, line in enumerate(lines, 1):
            m = SWAG.match(line)
            if m:
                block.append((i, m.group(1), m.group(2)))
                if m.group(1) == "BasePath":
                    base = m.group(2).rstrip("/")
                continue
            if line.strip().startswith("//") and block:
                continue  # plain comment inside a doc block
            fn = GO_FUNC.match(line)
            if block and fn:
                out += _swag_endpoints(block, fn.group(1), f, models)
            if line.strip():
                block = []
    for ep in out:
        ep["router_path"] = ep["path"]  # as written in @Router, relative to @BasePath
        if base and not ep["path"].startswith(base):
            ep["path"] = base + ep["path"]
    return out


def _swag_endpoints(block, handler: str, file: str, models: set[str]) -> list[dict]:
    doc: dict = {"summary": "", "description": [], "tags": [], "params": [], "responses": {},
                 "auth": False, "deprecated": False, "routes": [], "line": block[0][0]}
    for _line, key, val in block:
        k = key.lower()
        if k == "summary":
            doc["summary"] = val
        elif k == "description":
            doc["description"].append(val)
        elif k == "tags":
            doc["tags"] += [t.strip() for t in val.split(",") if t.strip()]
        elif k == "param":
            m = SWAG_PARAM.match(val)
            if m:
                doc["params"].append({"name": m.group(1), "in": m.group(2), "type": m.group(3),
                                      "required": m.group(4) == "true",
                                      "description": m.group(5) or ""})
        elif k in {"success", "failure", "response"}:
            m = SWAG_RESP.match(val)
            if m:
                code = "200" if m.group(1) == "default" else m.group(1)
                r: dict = {"description": m.group(4) or ""}
                if m.group(3):
                    r["schema"] = _swag_type(m.group(2) or "object", m.group(3), models)
                doc["responses"][code] = r
        elif k == "security":
            doc["auth"] = True
        elif k == "deprecated":
            doc["deprecated"] = True
        elif k == "router":
            m = SWAG_ROUTER.match(val)
            if m:
                doc["routes"].append((m.group(2).upper(), m.group(1)))
    eps = []
    for method, path in doc["routes"]:
        path = re.sub(r":(\w+)", r"{\1}", path)
        params, request = [], None
        for p in doc["params"]:
            if p["in"] == "body":
                request = {"content_type": "application/json",
                           "schema": _swag_type("object", p["type"], models)}
            elif p["in"] == "formData":
                request = request or {"content_type": "multipart/form-data",
                                      "schema": {"type": "object", "properties": {}}}
                request["schema"].setdefault("properties", {})[p["name"]] = {
                    "type": GO_TYPES.get(p["type"], "string")}
            else:
                params.append({"name": p["name"], "in": p["in"], "required": p["required"]
                               or p["in"] == "path", "description": p["description"],
                               "schema": {"type": GO_TYPES.get(p["type"], "string")}})
        ep = {"method": method, "path": path, "file": file, "line": doc["line"],
              "handler": handler, "framework": "swaggo", "summary": doc["summary"],
              "description": " ".join(doc["description"]), "params": params,
              "tag": doc["tags"][0] if doc["tags"] else _tag(path),
              "responses": doc["responses"] or {"200": {"description": "Successful response"}},
              "documented": "swaggo"}
        if request:
            ep["request"] = request
        if doc["auth"]:
            ep["auth"] = "bearer"
        if doc["deprecated"]:
            ep["deprecated"] = True
        eps.append(ep)
    return eps


def _tag(path: str) -> str:
    return next((s for s in path.split("/") if s and not s.startswith("{")
                 and s not in {"api", "v1", "v2", "v3"}), "root")


# -- Java / NestJS / Python: annotations around an inferred endpoint -------------------------------
def _code(line: str) -> str:
    """A line with string literals removed (so "{id}" or '{' in a string isn't code)."""
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`', '""', line)


def _window(lines: list[str], line: int, up: int = 25, down: int = 15) -> str:
    """The annotations around a route declaration: the run of annotations/decorators it sits
    in (multi-line ones included) and the method signature after it."""
    i = max(0, line - 1)
    start = i
    while start > 0 and i - start < up:
        prev = _code(lines[start - 1]).strip()
        if not prev or prev.endswith(("}", ";", "{")) and not prev.startswith("@"):
            break
        start -= 1
    end, depth = i, 0
    while end < len(lines) and end - i < down:
        code = _code(lines[end])
        depth += code.count("(") - code.count(")")
        stripped = code.strip()
        if depth <= 0 and not stripped.startswith("@") and end > i:
            break  # the signature line (first non-annotation line after the route)
        end += 1
    return "\n".join(lines[start:min(len(lines), end + 1)])


def _class_header(text: str, line: int) -> str:
    """Annotations on the class that contains `line` (class-level @Tag / @ApiTags / @Api)."""
    head = text[: sum(len(x) + 1 for x in text.splitlines()[: max(0, line - 1)])]
    m = list(re.finditer(r"((?:@\w+(?:\([^)]*\))?\s*)+)(?:export\s+)?(?:public\s+)?(?:abstract\s+)?"
                         r"class\s+\w+", head))
    return m[-1].group(1) if m else ""


def _str(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return (m.group(1) if m else "").strip()


def java_doc(window: str, cls: str) -> dict:
    d: dict = {"responses": {}, "params": {}}
    op = _str(r"@Operation\((.*?)\)\s*(?=@|public|private|protected|\w+\s+\w+\s*\(|$)", window)
    if op:
        d["summary"] = _str(r'summary\s*=\s*"([^"]*)"', op)
        d["description"] = _str(r'description\s*=\s*"([^"]*)"', op)
        tags = re.findall(r'"([^"]+)"', _str(r"tags\s*=\s*(\{[^}]*\}|\"[^\"]*\")", op))
        if tags:
            d["tag"] = tags[0]
        if re.search(r"hidden\s*=\s*true", op):
            d["hidden"] = True
    sf = _str(r"@ApiOperation\((.*?)\)\s*(?=@|public|private|protected|$)", window)
    if sf:
        d["summary"] = _str(r'(?:value\s*=\s*)?"([^"]*)"', sf)
        d["description"] = _str(r'notes\s*=\s*"([^"]*)"', sf)
    for m in re.finditer(r'@ApiResponse\(\s*(?:responseCode\s*=\s*"(\d{3})"|code\s*=\s*(\d{3}))'
                         r'[^)]*?(?:description|message)\s*=\s*"([^"]*)"', window):
        d["responses"][m.group(1) or m.group(2)] = m.group(3)
    for m in re.finditer(r'@(?:Parameter|ApiParam)\(([^)]*)\)\s*(?:@\w+(?:\([^)]*\))?\s*)*'
                         r'(?:final\s+)?[\w<>?,\s]+?\s(\w+)\s*[,)]', window):
        desc = _str(r'(?:description|value)\s*=\s*"([^"]*)"', m.group(1))
        if desc:
            d["params"][m.group(2)] = desc
    if re.search(r"@Hidden\b|@ApiIgnore\b", window):
        d["hidden"] = True
    if re.search(r"@SecurityRequirement\b", window + cls):
        d["auth"] = True
    if "tag" not in d:
        t = _str(r'@Tag\(\s*name\s*=\s*"([^"]+)"', cls) or _str(
            r'@Api\([^)]*tags\s*=\s*\{?\s*"([^"]+)"', cls)
        if t:
            d["tag"] = t
    if re.search(r"@Hidden\b", cls):
        d["hidden"] = True
    return d


def nest_doc(window: str, cls: str) -> dict:
    d: dict = {"responses": {}, "params": {}}
    op = _str(r"@ApiOperation\(\s*\{(.*?)\}\s*\)", window)
    if op:
        d["summary"] = _str(r"summary\s*:\s*['\"`]([^'\"`]*)", op)
        d["description"] = _str(r"description\s*:\s*['\"`]([^'\"`]*)", op)
    for m in re.finditer(r"@ApiResponse\(\s*\{([^}]*)\}", window):
        code = _str(r"status\s*:\s*(?:HttpStatus\.\w+|(\d{3}))", m.group(1))
        if code:
            d["responses"][code] = _str(r"description\s*:\s*['\"`]([^'\"`]*)", m.group(1))
    for m in re.finditer(r"@Api(\w+?)Response\(\s*(?:\{([^}]*)\})?", window):
        code = STATUS_NAMES.get(m.group(1))
        if code:
            d["responses"][code] = _str(r"description\s*:\s*['\"`]([^'\"`]*)", m.group(2) or "")
    for m in re.finditer(r"@ApiParam\(\s*\{([^}]*)\}", window):
        name = _str(r"name\s*:\s*['\"`]([^'\"`]*)", m.group(1))
        if name:
            d["params"][name] = _str(r"description\s*:\s*['\"`]([^'\"`]*)", m.group(1))
    tag = _str(r"@ApiTags\(\s*['\"`]([^'\"`]+)", window) or _str(
        r"@ApiTags\(\s*['\"`]([^'\"`]+)", cls)
    if tag:
        d["tag"] = tag
    if re.search(r"@ApiBearerAuth\(|@ApiSecurity\(|@ApiOAuth2\(", window + cls):
        d["auth"] = True
    if re.search(r"@ApiExcludeEndpoint\(", window) or re.search(r"@ApiExcludeController\(", cls):
        d["hidden"] = True
    return d


def python_doc(window: str) -> dict:
    d: dict = {"responses": {}, "params": {}}
    deco = window.split("def ", 1)[0]
    d["summary"] = _str(r"summary\s*=\s*['\"]([^'\"]*)", deco)
    d["description"] = _str(r"description\s*=\s*['\"]([^'\"]*)", deco)
    tags = re.findall(r"['\"]([^'\"]+)['\"]", _str(r"tags\s*=\s*\[([^\]]*)\]", deco))
    if tags:
        d["tag"] = tags[0]
    for m in re.finditer(r"(\d{3})\s*:\s*\{[^}]*?['\"]description['\"]\s*:\s*['\"]([^'\"]*)",
                         _str(r"responses\s*=\s*(\{.*\})", deco)):
        d["responses"][m.group(1)] = m.group(2)
    if re.search(r"include_in_schema\s*=\s*False|exclude\s*=\s*True", deco):
        d["hidden"] = True
    if re.search(r"deprecated\s*=\s*True", deco):
        d["deprecated"] = True
    doc = re.search(r"def \w+\(.*?\)\s*(?:->[^:]+)?:\s*\n\s*(?:\"\"\"|''')(.*?)(?:\"\"\"|''')",
                    window, re.S)
    if doc:
        text = doc.group(1).strip()
        first, _, rest = text.partition("\n")
        d.setdefault("summary", "")
        if not d["summary"]:
            d["summary"] = first.strip()
        if not d["description"] and rest.strip():
            d["description"] = " ".join(x.strip() for x in rest.splitlines() if x.strip())[:500]
    return d


def apply(ep: dict, doc: dict, source: str) -> None:
    """Merge documentation into an endpoint; never drop what the code already told us."""
    if doc.get("summary"):
        ep["summary"] = doc["summary"]
    if doc.get("description"):
        ep["description"] = doc["description"]
    if doc.get("tag"):
        ep["tag"] = doc["tag"]
    if doc.get("auth"):
        ep["auth"] = ep.get("auth") or "bearer"
    if doc.get("deprecated"):
        ep["deprecated"] = True
    responses = ep.setdefault("responses", {})
    for code, desc in doc.get("responses", {}).items():
        responses.setdefault(code, {})
        if desc:
            responses[code]["description"] = desc
    for p in ep.get("params", []):
        if doc.get("params", {}).get(p["name"]):
            p["description"] = doc["params"][p["name"]]
    if any(doc.get(k) for k in ("summary", "description", "responses", "tag", "params")):
        ep["documented"] = source


def document(repo: Repo, endpoints: list[dict]) -> list[dict]:
    """Apply annotations to inferred endpoints; drop hidden ones. Returns the kept endpoints."""
    kept = []
    texts: dict[str, list[str]] = {}
    for ep in endpoints:
        f, fw = ep.get("file") or "", ep.get("framework")
        if not f or not ep.get("line") or fw not in {"Spring", "NestJS", "FastAPI", "Flask",
                                                     "Django"}:
            kept.append(ep)
            continue
        if f not in texts:
            texts[f] = repo.read(f, 300_000).splitlines()
        lines = texts[f]
        text = "\n".join(lines)
        if fw == "Spring":
            doc, source = java_doc(_window(lines, ep["line"]), _class_header(text, ep["line"])), \
                "springdoc" if "@Operation" in text or "@Tag" in text else "springfox"
        elif fw == "NestJS":
            doc, source = nest_doc(_window(lines, ep["line"]), _class_header(text, ep["line"])), \
                "nestjs-swagger"
        else:
            window = "\n".join(lines[max(0, ep["line"] - 1): ep["line"] + 25])
            doc, source = python_doc(window), "drf-spectacular" if "extend_schema" in window \
                else fw.lower()
        if doc.get("hidden"):
            continue
        apply(ep, doc, source)
        kept.append(ep)
    return kept


# -- live spec -------------------------------------------------------------------------------------
def live_spec(url: str, timeout: float = 10.0) -> tuple[dict | None, str | None]:
    """Fetch a spec a running service generates (e.g. http://localhost:8080/v3/api-docs).
    Returns (document, error)."""
    import httpx
    import yaml

    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"Accept": "application/json, application/yaml"})
    except httpx.HTTPError as exc:
        return None, f"Could not reach {url}: {exc}"
    if r.status_code >= 400:
        return None, f"{url} returned {r.status_code}"
    try:
        doc = r.json() if "json" in r.headers.get("content-type", "") or r.text.lstrip()[:1] in \
            "{[" else yaml.safe_load(r.text)
    except (ValueError, yaml.YAMLError):
        return None, f"{url} did not return JSON or YAML"
    if not isinstance(doc, dict) or not (doc.get("openapi") or doc.get("swagger")):
        return None, f"{url} is not an OpenAPI/Swagger document"
    return doc, None
