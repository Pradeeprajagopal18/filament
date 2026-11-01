"""Find the HTTP calls a repo makes to other services (fetch, axios, requests, httpx, ...).

Each call keeps the path it hits and, when the URL is built from an env var or host, that base,
so the system map can link the caller to the repo that serves the endpoint.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filament.readiness.scanner import Repo

CODE_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".py", ".go", ".java", ".kt", ".rb", ".vue",
            ".svelte"}
SKIP = {"node_modules", "dist", "build", "vendor", ".venv", "venv", "tests", "test", "__tests__",
        "e2e", "fixtures", "mocks", "coverage"}
STR = r"""(`[^`]*`|'[^'\n]*'|"[^"\n]*"|f'[^'\n]*'|f"[^"\n]*")"""
CALLS = [
    # fetch(url, { method: 'POST' })
    (re.compile(r"\bfetch\(\s*" + STR + r"(?:\s*,\s*\{[^}]*?method:\s*['\"`](\w+))?", re.S), None),
    # axios.get(url) / http.post(url) / api.delete(url) / client.put(url) / ky.patch(url)
    (re.compile(r"\b(?:axios|http|https|api|client|apiClient|httpClient|request|ky|\$http|this\.http)"
                r"\.(get|post|put|patch|delete)\s*(?:<[^>]*>)?\(\s*" + STR), "group1"),
    # requests.get(url) / httpx.post(url) / session.get(url) / client.get(url) (python)
    (re.compile(r"\b(?:requests|httpx|session|self\.session|self\.client|aiohttp\.\w+)"
                r"\.(get|post|put|patch|delete)\(\s*" + STR), "group1"),
    # Go: http.Get("...") / http.NewRequest("POST", "...")
    (re.compile(r"http\.NewRequest(?:WithContext)?\([^,]*?,?\s*\"(GET|POST|PUT|PATCH|DELETE)\"\s*,\s*"
                + STR), "group1"),
    # Java/Spring: restTemplate.getForObject("...") / webClient.get().uri("...")
    (re.compile(r"\b(?:restTemplate|webClient|restClient)\.(get|post|put|patch|delete)\w*\(\s*"
                r"(?:\)\s*\.uri\(\s*)?" + STR), "group1"),
]
TEMPLATE_VAR = re.compile(r"\$\{\s*([^}]+?)\s*\}|\{([A-Za-z_][\w.]*)\}")
ENV_IN_EXPR = re.compile(r"(?:process\.env\.|import\.meta\.env\.|os\.environ\[?['\"]?|"
                         r"os\.getenv\(['\"]|settings\.)([A-Z][A-Z0-9_]{2,})")


def _path_of(literal: str, text: str) -> tuple[str | None, str | None]:
    """(path with {} params, base) from a URL literal or template."""
    s = literal.strip()
    if s[:1] == "f":
        s = s[1:]
    s = s[1:-1]
    base = None
    m = re.match(r"https?://([^/'\"`$]+)", s)
    if m:
        base = m.group(1)
        s = s[m.end():]
    first = TEMPLATE_VAR.match(s)
    if first:  # `${BASE}/v1/carts` -> base from the variable
        expr = first.group(1) or first.group(2)
        env = ENV_IN_EXPR.search(expr)
        if env:
            base = env.group(1)
        else:
            var = expr.split(".")[-1]
            decl = re.search(rf"\b{re.escape(var)}\s*[:=]\s*([^\n;]+)", text)
            env = ENV_IN_EXPR.search(decl.group(1)) if decl else None
            base = env.group(1) if env else var
        s = s[first.end():]
    if not s.startswith("/"):
        return None, base
    s = s.split("?")[0].split("#")[0]
    s = TEMPLATE_VAR.sub("{}", s)
    s = re.sub(r"/:\w+", "/{}", s)
    s = re.sub(r"/\d+(?=/|$)", "/{}", s)
    if not re.match(r"^/[\w{}\-./]*$", s) or s.endswith((".js", ".css", ".png", ".svg", ".html")):
        return None, base
    return (s.rstrip("/") or "/"), base


def extract(repo: Repo, limit: int = 400) -> list[dict]:
    out, seen = [], set()
    for p in repo.files:
        if p.suffix not in CODE_EXT or set(p.parts[:-1]) & SKIP or ".test." in p.name \
                or ".spec." in p.name:
            continue
        f = str(p)
        text = repo.read(f, 300_000)
        for rx, method_group in CALLS:
            for m in rx.finditer(text):
                if method_group:
                    method, literal = m.group(1), m.group(2)
                else:
                    literal, method = m.group(1), m.group(2) or "GET"
                path, base = _path_of(literal, text)
                if not path:
                    continue
                key = (method.upper(), path, f)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"method": method.upper(), "path": path, "base": base, "file": f,
                            "line": text.count("\n", 0, m.start()) + 1})
                if len(out) >= limit:
                    return out
    return out
