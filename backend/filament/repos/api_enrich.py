"""Fill in what route discovery alone misses: request and response schemas, query parameters,
auth and error responses, plus example payloads for the API simulator.

Everything here is read from the code (Pydantic / dataclass-like models, FastAPI signatures,
Express handlers, Spring controllers), so nothing is invented. A separate, optional model pass
(`model_enrich`) can add summaries and fill gaps; its output is marked as model-inferred.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filament.readiness.scanner import Repo

PY_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
            "dict": "object", "Any": "object", "bytes": "string", "UUID": "string",
            "Decimal": "number", "EmailStr": "string", "HttpUrl": "string", "AnyUrl": "string"}
PY_FORMATS = {"datetime": "date-time", "date": "date", "UUID": "uuid", "EmailStr": "email",
              "HttpUrl": "uri", "AnyUrl": "uri", "Decimal": "decimal"}
JAVA_TYPES = {"String": "string", "Integer": "integer", "int": "integer", "Long": "integer",
              "long": "integer", "Double": "number", "double": "number", "Float": "number",
              "BigDecimal": "number", "Boolean": "boolean", "boolean": "boolean",
              "UUID": "string", "Instant": "string", "LocalDate": "string",
              "LocalDateTime": "string", "OffsetDateTime": "string", "Map": "object"}
AUTH_HINT = re.compile(r"(?i)\b(get_current_user|current_user|require_auth|requires_auth|"
                       r"authenticate|authenticated|verify_token|verify_jwt|jwt_required|"
                       r"login_required|requireAuth|isAuthenticated|ensureAuth|passport\.authenticate|"
                       r"HTTPBearer|OAuth2PasswordBearer|APIKeyHeader|Security\(|api_key|"
                       r"@PreAuthorize|@Secured|@RolesAllowed|auth_required|checkJwt|authMiddleware)")
STATUS_TEXT = {"400": "Bad request", "401": "Unauthorized", "403": "Forbidden",
               "404": "Not found", "409": "Conflict", "422": "Validation error",
               "429": "Too many requests", "500": "Server error", "502": "Bad gateway",
               "503": "Unavailable"}


# -- models ------------------------------------------------------------------------------
PY_CLASS = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", re.M)
PY_FIELD = re.compile(r"^\s{4}(\w+)\s*:\s*([^=\n#]+?)\s*(?:=\s*([^\n#]+))?\s*(?:#.*)?$", re.M)
MODEL_BASES = re.compile(r"\b(BaseModel|SQLModel|Schema|BaseSchema|TypedDict|"
                         r"pydantic\.BaseModel)\b")


def _py_schema(annotation: str, models: set[str]) -> tuple[dict, bool]:
    """JSON schema for a Python annotation, and whether the value is optional."""
    a = annotation.strip().strip("'\"")
    optional = False
    if a.startswith("Optional[") and a.endswith("]"):
        a, optional = a[9:-1], True
    parts = [p.strip() for p in re.split(r"\|(?![^\[]*\])", a)]
    if "None" in parts:
        optional = True
        parts = [p for p in parts if p != "None"]
    a = parts[0] if parts else "Any"
    m = re.match(r"(?:list|List|Sequence|set|Set|tuple|Tuple)\[(.+)\]$", a)
    if m:
        inner, _ = _py_schema(m.group(1).split(",")[0], models)
        return {"type": "array", "items": inner}, optional
    if re.match(r"(?:dict|Dict|Mapping)\[", a):
        return {"type": "object"}, optional
    if m := re.match(r"Literal\[(.+)\]$", a):
        return {"type": "string", "enum": re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))}, optional
    if a in models:
        return {"$ref": f"#/components/schemas/{a}"}, optional
    base = a.split(".")[-1]
    schema = {"type": PY_TYPES.get(base, "string")}
    if base in PY_FORMATS:
        schema["format"] = PY_FORMATS[base]
    return schema, optional


def python_models(texts: dict[str, str]) -> dict[str, dict]:
    raw = {}
    for f, text in texts.items():
        for m in PY_CLASS.finditer(text):
            if not MODEL_BASES.search(m.group(2)) and m.group(2).strip() not in raw:
                continue
            end = re.search(r"^\S", text[m.end():], re.M)
            body = text[m.end(): m.end() + end.start()] if end else text[m.end():]
            raw[m.group(1)] = (body, f, m.group(2))
    # Classes that inherit from another model in the set are models too.
    names = set(raw)
    schemas = {}
    for name, (body, f, bases) in raw.items():
        props, required = {}, []
        for base in re.findall(r"\w+", bases):
            if base in raw and base != name:
                props.update(_fields(raw[base][0], names)[0])
        fields, req = _fields(body, names)
        props.update(fields)
        required += req
        if props:
            schemas[name] = {"type": "object", "properties": props, "x-source": f,
                             **({"required": required} if required else {})}
    return schemas


def _fields(body: str, models: set[str]) -> tuple[dict, list[str]]:
    props, required = {}, []
    for fm in PY_FIELD.finditer(body):
        name, ann, default = fm.group(1), fm.group(2), fm.group(3)
        if name.startswith("_") or name in {"model_config", "Config"} or "ClassVar" in ann:
            continue
        schema, optional = _py_schema(ann, models)
        props[name] = schema
        if not optional and default is None:
            required.append(name)
    return props, required


JAVA_CLASS = re.compile(r"(?:public\s+)?(?:class|record)\s+(\w+)\s*(\([^)]*\))?[^{]*\{")
JAVA_FIELD = re.compile(r"(?:private|public|protected)\s+(?:final\s+)?([\w<>, ?]+?)\s+(\w+)\s*[;=]")


def java_models(texts: dict[str, str]) -> dict[str, dict]:
    schemas = {}
    for f, text in texts.items():
        if not re.search(r"\b(class|record)\b", text) or "Controller" in f:
            continue
        for m in JAVA_CLASS.finditer(text):
            name = m.group(1)
            fields = re.findall(r"([\w<>?]+)\s+(\w+)\s*(?:,|$)", m.group(2)[1:-1]) if m.group(2) \
                else [(t, n) for t, n in JAVA_FIELD.findall(text[m.end(): m.end() + 6000])]
            props = {}
            for jtype, fname in fields:
                base = jtype.split("<")[0].strip()
                if base in {"List", "Set", "Collection"}:
                    inner = re.search(r"<\s*(\w+)", jtype)
                    item = {"type": JAVA_TYPES.get(inner.group(1), "object")} if inner else {}
                    props[fname] = {"type": "array", "items": item}
                else:
                    props[fname] = {"type": JAVA_TYPES.get(base, "object")}
            if props and name.endswith(("Request", "Response", "Dto", "DTO", "Body", "Model",
                                        "Command", "View", "Resource")) or (m.group(2) and props):
                schemas[name] = {"type": "object", "properties": props, "x-source": f}
    return schemas


# -- handler bodies ------------------------------------------------------------------------
def python_handler(text: str, line: int) -> tuple[str, str, str]:
    """(decorators, signature, body) of the function decorated at `line` (1-based)."""
    lines = text.splitlines()
    i = max(0, line - 1)
    deco_start = i
    while i < len(lines) and not re.match(r"\s*(async\s+)?def\s", lines[i]):
        i += 1
    if i >= len(lines):
        return "", "", ""
    indent = len(lines[i]) - len(lines[i].lstrip())
    sig_lines = []
    while i < len(lines):
        sig_lines.append(lines[i])
        if re.search(r"\)\s*(->\s*[^:]+)?:\s*(#.*)?$", lines[i]):
            break
        i += 1
    body = []
    for ln in lines[i + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        body.append(ln)
    return "\n".join(lines[deco_start:i - len(sig_lines) + 1]), "\n".join(sig_lines), \
        "\n".join(body)[:6000]


def _split_params(sig: str) -> list[str]:
    inner = sig[sig.find("(") + 1: sig.rfind(")")]
    out, depth, cur = [], 0, ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out if p.strip() and p.strip() not in {"self", "*", "/"}]


def _errors(body: str) -> list[str]:
    codes = set(re.findall(r"HTTPException\(\s*(?:status_code\s*=\s*)?(\d{3})", body))
    codes |= set(re.findall(r"HTTP_(\d{3})_", body))
    codes |= set(re.findall(r"abort\(\s*(\d{3})", body))
    codes |= set(re.findall(r"\.status\(\s*(\d{3})\s*\)", body))
    spring = {"NOT_FOUND": "404", "BAD_REQUEST": "400", "CONFLICT": "409", "FORBIDDEN": "403",
              "UNAUTHORIZED": "401", "UNPROCESSABLE_ENTITY": "422"}
    codes |= {spring.get(s, "") for s in re.findall(r"HttpStatus\.(\w+)", body)}
    codes |= {"404" for _ in re.findall(r"(?:ResponseStatusException|NotFoundException|"
                                        r"EntityNotFound|ResourceNotFound)", body)[:1]}
    return sorted(c for c in codes if c and not c.startswith("2"))


def _enrich_fastapi(ep: dict, text: str, models: set[str], router_auth: bool) -> None:
    deco, sig, body = python_handler(text, ep["line"] or 1)
    if not sig:
        return
    path_params = {p["name"] for p in ep["params"]}
    params, request = [], None
    for raw in _split_params(sig):
        m = re.match(r"(\w+)\s*(?::\s*([^=]+?))?\s*(?:=\s*(.+))?$", raw, re.S)
        if not m:
            continue
        name, ann, default = m.group(1), (m.group(2) or "").strip(), (m.group(3) or "").strip()
        if re.match(r"(Request|Response|Session|AsyncSession|BackgroundTasks|WebSocket)\b", ann) \
                or default.startswith(("Depends(", "Security(")):
            if AUTH_HINT.search(raw):
                ep["auth"] = "bearer"
            continue
        if re.match(r"Annotated\[", ann):
            if "Depends(" in ann or "Security(" in ann:
                if AUTH_HINT.search(ann):
                    ep["auth"] = "bearer"
                continue
            ann = ann[10:].split(",")[0]
        schema, optional = _py_schema(ann or "str", models)
        if name in path_params:
            for p in ep["params"]:
                if p["name"] == name:
                    p["schema"] = schema
            continue
        if "$ref" in schema or (schema.get("type") == "array" and "$ref" in schema["items"]) \
                or default.startswith("Body("):
            request = {"content_type": "application/json", "schema": schema}
            continue
        if default.startswith(("Header(", "Cookie(")):
            continue
        params.append({"name": name, "in": "query", "schema": schema,
                       "required": not optional and not default})
    ep["params"] += params
    if request:
        ep["request"] = request
    status = re.search(r"status_code\s*=\s*(?:status\.HTTP_)?(\d{3})", deco)
    rm = re.search(r"response_model\s*=\s*([\w\[\]., |]+?)\s*[,)]", deco)
    ret = re.search(r"->\s*([^:]+):\s*$", sig.strip())
    resp_ann = rm.group(1) if rm else (ret.group(1).strip() if ret else "")
    ok = status.group(1) if status else ("201" if ep["method"] == "POST" and
                                         re.search(r"create|add|place|register", ep["handler"])
                                         else "200")
    resp: dict = {"description": "Successful response"}
    if resp_ann and resp_ann not in {"None", "dict", "Any", "Response", "JSONResponse"}:
        resp["schema"] = _py_schema(resp_ann, models)[0]
    if ok == "204":
        resp = {"description": "No content"}
    ep["responses"] = {ok: resp, **{c: {"description": STATUS_TEXT.get(c, "Error")}
                                    for c in _errors(body)}}
    if request or path_params:
        ep["responses"].setdefault("422", {"description": STATUS_TEXT["422"]})
    if router_auth or AUTH_HINT.search(deco):
        ep["auth"] = "bearer"
    if ep.get("auth"):
        ep["responses"].setdefault("401", {"description": STATUS_TEXT["401"]})


def _enrich_express(ep: dict, text: str) -> None:
    lines = text.splitlines()
    start = max(0, (ep["line"] or 1) - 1)
    head = "\n".join(lines[start:start + 3])
    chunk = "\n".join(lines[start:start + 60])
    body = chunk
    handler = ep.get("handler")
    if handler and not re.search(r"=>|function\s*\(", head):
        fn = re.search(rf"(?:function\s+{handler}\s*\(|(?:const|let)\s+{handler}\s*=)", text)
        if fn:
            body = text[fn.start(): fn.start() + 3000]
    mw = head[head.find(","):].split("=>")[0] if "," in head else ""
    if AUTH_HINT.search(mw) or re.search(r"\b(auth|authenticate|requireAuth|protect|"
                                         r"verifyToken|ensureLoggedIn)\b", mw):
        ep["auth"] = "bearer"
    fields = sorted(set(re.findall(r"req\.body\.(\w+)", body)) |
                    {f.strip() for m in re.findall(r"\{([^{}]+)\}\s*=\s*req\.body", body)
                     for f in m.split(",") if re.match(r"^\s*\w+\s*$", f)})
    if fields:
        ep["request"] = {"content_type": "application/json",
                         "schema": {"type": "object",
                                    "properties": {f: {"type": "string"} for f in fields}}}
    query = sorted(set(re.findall(r"req\.query\.(\w+)", body)))
    ep["params"] += [{"name": q, "in": "query", "schema": {"type": "string"}, "required": False}
                     for q in query]
    ok = re.search(r"\.status\(\s*(20\d)\s*\)", body)
    ep["responses"] = {ok.group(1) if ok else "200": {"description": "Successful response"},
                       **{c: {"description": STATUS_TEXT.get(c, "Error")} for c in _errors(body)}}
    if ep.get("auth"):
        ep["responses"].setdefault("401", {"description": STATUS_TEXT["401"]})


def _enrich_spring(ep: dict, text: str, models: set[str]) -> None:
    lines = text.splitlines()
    start = max(0, (ep["line"] or 1) - 1)
    chunk = "\n".join(lines[start:start + 40])
    sig = chunk[: chunk.find("{")] if "{" in chunk else chunk
    body = chunk[chunk.find("{"):] if "{" in chunk else ""
    rb = re.search(r"@RequestBody\s+(?:@\w+\s+)*([\w<>]+)\s+\w+", sig)
    if rb:
        name = rb.group(1).split("<")[0]
        ep["request"] = {"content_type": "application/json",
                         "schema": {"$ref": f"#/components/schemas/{name}"} if name in models
                         else {"type": "object"}}
    for q in re.findall(r"@RequestParam(?:\([^)]*\))?\s+([\w<>]+)\s+(\w+)", sig):
        ep["params"].append({"name": q[1], "in": "query",
                             "schema": {"type": JAVA_TYPES.get(q[0], "string")}, "required": False})
    ret = re.search(r"(?:public\s+)?(?:ResponseEntity<\s*)?([\w<>]+?)>?\s+" +
                    re.escape(ep.get("handler") or "x") + r"\s*\(", sig)
    resp: dict = {"description": "Successful response"}
    if ret:
        t = ret.group(1)
        inner = re.search(r"List<\s*(\w+)", t)
        name = inner.group(1) if inner else t.split("<")[0]
        if name in models:
            ref = {"$ref": f"#/components/schemas/{name}"}
            resp["schema"] = {"type": "array", "items": ref} if inner else ref
    created = "HttpStatus.CREATED" in chunk or "@ResponseStatus(HttpStatus.CREATED)" in text[
        max(0, text.find(sig) - 200): text.find(sig)]
    ep["responses"] = {"201" if created else "200": resp,
                       **{c: {"description": STATUS_TEXT.get(c, "Error")} for c in _errors(body)}}
    if AUTH_HINT.search(sig) or re.search(r"@PreAuthorize|@Secured|@RolesAllowed",
                                          "\n".join(lines[max(0, start - 4):start])):
        ep["auth"] = "bearer"


# -- examples (API simulator) --------------------------------------------------------------
_SAMPLE_STR = [("email", "ada@example.com"), ("currency", "usd"), ("status", "active"),
               ("url", "https://example.com"), ("token", "tok_visa_4242"),
               ("phone", "+1-555-0100"), ("sku", "SKU-1001"), ("description", "Sample text"),
               ("title", "Sample title"), ("name", "Ada Lovelace"), ("note", "Leave at door"),
               ("code", "WELCOME10"), ("country", "US"), ("city", "Tracy")]


def example(schema: dict | None, components: dict, field: str = "", depth: int = 0,
            seen: frozenset = frozenset()):
    """A plausible example value for a JSON schema (refs resolved, cycles cut)."""
    if not schema or depth > 8:
        return None
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return {}
        return example(components.get(name), components, field, depth + 1, seen | {name})
    t = schema.get("type")
    if schema.get("example") is not None:
        return schema["example"]
    if schema.get("enum"):
        return schema["enum"][0]
    if t == "object":
        return {k: example(v, components, k, depth + 1, seen)
                for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [example(schema.get("items"), components, field, depth + 1, seen)]
    f = field.lower()
    if t == "integer":
        return 2 if re.search(r"qty|quantity|count", f) else 20 if "limit" in f else \
            1042 if f.endswith("id") else 1
    if t == "number":
        return 49.99
    if t == "boolean":
        return True
    fmt = schema.get("format")
    if fmt == "date-time" or f.endswith("_at"):
        return "2026-11-01T09:00:00Z"
    if fmt == "date":
        return "2026-11-01"
    if fmt == "uuid":
        return "3f2b8c1e-7d4a-4e2b-9a61-5c0d8e7f1a23"
    if f == "id" or f.endswith("_id"):
        stem = f[:-3] or "obj"
        return f"{stem[:4]}_1042"
    hit = next((v for k, v in _SAMPLE_STR if k in f), None)
    return hit or (f"sample-{field}" if field else "sample")


# -- entry points ----------------------------------------------------------------------------
def enrich(repo: Repo, endpoints: list[dict]) -> dict:
    """Add request/response/auth/error details to inferred endpoints; return component schemas."""
    py_files = [str(p) for p in repo.files if p.suffix == ".py"]
    java_files = [str(p) for p in repo.files if p.suffix in {".java", ".kt"}]
    texts = {f: repo.read(f, 300_000) for f in py_files + java_files}
    schemas = python_models({f: t for f, t in texts.items() if f.endswith(".py")})
    schemas.update(java_models({f: t for f, t in texts.items() if not f.endswith(".py")}))
    models = set(schemas)
    router_auth: dict[str, bool] = {}
    for ep in endpoints:
        f = ep.get("file") or ""
        text = texts.get(f) if f in texts else (repo.read(f, 300_000) if f else "")
        if not text:
            continue
        ep.setdefault("responses", {})
        try:
            if ep["framework"] == "FastAPI":
                if f not in router_auth:
                    deps = re.findall(r"APIRouter\(([^)]*dependencies\s*=\s*\[[^\]]*\])", text)
                    router_auth[f] = any(AUTH_HINT.search(d) for d in deps)
                _enrich_fastapi(ep, text, models, router_auth[f])
            elif ep["framework"] in {"Express", "NestJS"}:
                _enrich_express(ep, text)
            elif ep["framework"] == "Spring":
                _enrich_spring(ep, text, models)
        except (re.error, IndexError, ValueError):
            continue
    for ep in endpoints:
        if ep.get("request"):
            ep["example_request"] = example(ep["request"].get("schema"), schemas)
        ok = next((c for c in sorted(ep.get("responses") or {}) if c.startswith("2")), None)
        if ok and ep["responses"][ok].get("schema"):
            ep["example_response"] = example(ep["responses"][ok]["schema"], schemas)
    return schemas


def spec_examples(endpoints: list[dict], spec: dict) -> None:
    """Examples for endpoints read from a checked-in OpenAPI document."""
    components = (spec.get("components") or {}).get("schemas") or {}
    for ep in endpoints:
        op = ((spec.get("paths") or {}).get(ep.get("spec_path", ep["path"])) or {}).get(
            ep["method"].lower()) or {}
        body = ((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        if body.get("schema"):
            ep["request"] = {"content_type": "application/json", "schema": body["schema"]}
            ep["example_request"] = body.get("example") or example(body["schema"], components)
        responses = op.get("responses") or {}
        ep["responses"] = {str(k): {"description": (v or {}).get("description", "")}
                           for k, v in responses.items()}
        ok = next((str(c) for c in responses if str(c).startswith("2")), None)
        if ok:
            content = ((responses[ok if ok in responses else int(ok)] or {}).get("content") or {})
            js = content.get("application/json") or {}
            if js.get("schema"):
                ep["responses"][ok]["schema"] = js["schema"]
                ep["example_response"] = js.get("example") or example(js["schema"], components)
        if op.get("security") or spec.get("security"):
            ep["auth"] = "bearer"


# -- optional model pass ------------------------------------------------------------------------
MODEL_SYSTEM = (
    "You document HTTP APIs from source code. For each endpoint you are given its method, path "
    "and handler source. Reply with ONLY a JSON array, one object per endpoint you can describe: "
    '{"key": "<METHOD path exactly as given>", "summary": "<one line>", '
    '"description": "<1-2 sentences>", "errors": ["404", ...]}. Describe only behaviour visible '
    "in the code. Omit an endpoint rather than guess."
)


def model_prompt(repo: Repo, endpoints: list[dict], limit: int = 40) -> tuple[str, list[dict]]:
    picked = [e for e in endpoints if e.get("file") and e["method"] not in {"RPC", "QUERY",
                                                                              "MUTATION"}][:limit]
    parts = []
    for ep in picked:
        text = repo.read(ep["file"], 300_000)
        lines = text.splitlines()
        start = max(0, (ep.get("line") or 1) - 1)
        snippet = "\n".join(lines[start:start + 30])[:1200]
        parts.append(f"### {ep['method']} {ep['path']}\n```\n{snippet}\n```")
    return "\n\n".join(parts), picked


def apply_model_output(text: str, endpoints: list[dict]) -> int:
    """Merge model output into endpoints. Only keys that match a real endpoint are accepted."""
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return 0
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return 0
    by_key = {f"{e['method']} {e['path']}": e for e in endpoints}
    applied = 0
    for it in items if isinstance(items, list) else []:
        ep = by_key.get(str(it.get("key", "")))
        if not ep:
            continue  # unknown endpoint: ignored (no invented routes)
        if it.get("summary") and not ep.get("summary"):
            ep["summary"] = str(it["summary"])[:160]
        if it.get("description"):
            ep["description"] = str(it["description"])[:600]
        for code in it.get("errors") or []:
            code = str(code)
            if re.fullmatch(r"[45]\d\d", code):
                ep.setdefault("responses", {}).setdefault(
                    code, {"description": STATUS_TEXT.get(code, "Error"), "x-inferred": "model"})
        ep["enriched"] = "model"
        applied += 1
    return applied


def offline_summaries(endpoints: list[dict]) -> str:
    """Deterministic stand-in for the model pass when no API key is set."""
    out = []
    for ep in endpoints:
        words = re.sub(r"(?<!^)(?=[A-Z])", "_", ep.get("handler") or "").lower().split("_")
        words = [w for w in words if w]
        if words:
            out.append({"key": f"{ep['method']} {ep['path']}",
                        "summary": " ".join(words).capitalize()})
    return json.dumps(out)
