"""Discover a frontend's pages and build wireframe mocks from their components.

Routes come from React Router, Next.js (pages/ and app/), Vue Router and Angular routes; if no
router is found, page-like folders (pages/, views/, screens/) or App.* are used. Each page's
JSX / template is scanned in order for headings, text, buttons, inputs, links, images, tables,
lists and child components (resolved one level deep), and rendered as a neutral wireframe so a
team can see the shape of every screen without running the app.
"""

from __future__ import annotations

import html
import re
from pathlib import PurePosixPath

from filament.readiness.scanner import Repo

UI_EXT = (".tsx", ".jsx", ".vue", ".svelte", ".js", ".ts")
SKIP = {"node_modules", "dist", "build", "__tests__", "test", "tests", ".storybook", "stories"}

ROUTE_JSX = re.compile(r"<Route\b[^>]*\bpath=[\"'{`]+([^\"'`}]+)[\"'`}]+[^>]*\belement=\{\s*<(\w+)")
ROUTE_OBJ = re.compile(r"path:\s*['\"`]([^'\"`]*)['\"`][^}]{0,200}?(?:element:\s*<(\w+)|"
                       r"component:\s*(?:\(\)\s*=>\s*import\(['\"]([^'\"]+)['\"]\)|(\w+)))", re.S)
IMPORT = re.compile(r"import\s+(?:(\w+)|\{([^}]+)\})\s+from\s+['\"]([^'\"]+)['\"]")
TAG = re.compile(r"<(/?)([A-Za-z][\w.]*)((?:[^>\"'{]|\"[^\"]*\"|'[^']*'|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})*?)(/?)>")
ATTR = re.compile(r"(\w[\w-]*)=(?:\"([^\"]*)\"|'([^']*)'|\{['\"`]([^'\"`]*)['\"`]\})")


def _ui_files(repo: Repo) -> list[str]:
    return [str(p) for p in repo.files if p.suffix in UI_EXT and not (set(p.parts) & SKIP)
            and not re.search(r"\.(test|spec|stories|d)\.[jt]sx?$", p.name)]


def _find_component(repo: Repo, name: str) -> str | None:
    """Locate a component that was not imported explicitly (globals, barrels, same file)."""
    files = _ui_files(repo)
    for f in files:
        if PurePosixPath(f).stem == name:
            return f
    pat = re.compile(rf"(?:function|class)\s+{name}\b|(?:const|let)\s+{name}\s*[:=]")
    return next((f for f in files if pat.search(repo.read(f, 200_000))), None)


def _resolve(repo: Repo, from_file: str, spec: str) -> str | None:
    if not spec.startswith((".", "@/", "~/", "src/")):
        return None
    base = PurePosixPath(from_file).parent
    if spec.startswith(("@/", "~/")):
        rel = PurePosixPath("src") / spec[2:]
    elif spec.startswith("src/"):
        rel = PurePosixPath(spec)
    else:
        rel = base / spec
    parts: list[str] = []
    for part in rel.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    stem = "/".join(parts)
    for cand in [stem] + [stem + e for e in UI_EXT] + [f"{stem}/index{e}" for e in UI_EXT]:
        if cand.lower() in repo.rel:
            return next(str(p) for p in repo.files if str(p).lower() == cand.lower())
    return None


def _imports(repo: Repo, file: str, text: str) -> dict[str, str]:
    out = {}
    for m in IMPORT.finditer(text):
        target = _resolve(repo, file, m.group(3))
        if not target:
            continue
        if m.group(1):
            out[m.group(1)] = target
        for name in (m.group(2) or "").split(","):
            name = name.split(" as ")[-1].strip()
            if name:
                out[name] = target
    return out


def _routes(repo: Repo, files: list[str]) -> list[dict]:
    routes = []
    for f in files:
        text = repo.read(f, 200_000)
        if "Route" not in text and "path:" not in text:
            continue
        imports = _imports(repo, f, text)
        for m in ROUTE_JSX.finditer(text):
            routes.append({"path": m.group(1), "component": m.group(2),
                           "file": imports.get(m.group(2)), "router": "React Router"})
        if re.search(r"createBrowserRouter|createRouter|RouterModule|Routes\s*=|routes\s*[:=]", text):
            for m in ROUTE_OBJ.finditer(text):
                comp = m.group(2) or m.group(4)
                target = imports.get(comp) if comp else (_resolve(repo, f, m.group(3))
                                                         if m.group(3) else None)
                router = "Vue Router" if "vue" in text.lower() else (
                    "Angular" if "RouterModule" in text else "React Router")
                routes.append({"path": m.group(1) or "/", "component": comp or
                               PurePosixPath(m.group(3) or "page").stem, "file": target,
                               "router": router})
    # Next.js file-based routing (only when the repo actually uses Next.js).
    is_next = any(p.name.startswith("next.config") for p in repo.files) or any(
        re.search(r'"next"\s*:', repo.read(str(p))) for p in repo.files if p.name == "package.json")
    for p in (repo.files if is_next else []):
        parts = p.parts
        if p.suffix not in (".tsx", ".jsx", ".js", ".ts") or set(parts) & SKIP \
                or re.search(r"\.(test|spec|stories|d)\.[jt]sx?$", p.name):
            continue
        if "pages" in parts and "api" not in parts and not p.name.startswith("_"):
            i = parts.index("pages")
            route = "/" + "/".join(parts[i + 1:-1] + ((p.stem,) if p.stem != "index" else ()))
            routes.append({"path": re.sub(r"\[(\w+)\]", r"{\1}", route), "component": p.stem,
                           "file": str(p), "router": "Next.js pages"})
        elif "app" in parts and p.stem == "page":
            i = parts.index("app")
            segs = [s for s in parts[i + 1:-1] if not s.startswith("(")]
            routes.append({"path": "/" + "/".join(re.sub(r"\[(\w+)\]", r"{\1}", s) for s in segs),
                           "component": "page", "file": str(p), "router": "Next.js app"})
    seen, unique = set(), []
    for r in routes:
        key = (r["path"], r["component"])
        if key not in seen:
            seen.add(key)
            r["path"] = "/" + r["path"].lstrip("/") if r["path"] != "*" else "*"
            unique.append(r)
    return unique


CODEY = re.compile(r"=>|[{}();]|^\s*[)\]]|\breturn\b|&&|\|\||\?\s*\(|\bconst\b")


def _text_of(segment: str) -> str:
    segment = re.sub(r"\{[^{}]*\}", " … ", segment)
    text = re.sub(r"\s+", " ", html.unescape(segment)).strip()
    return "" if CODEY.search(text) else text


TOP_DEF = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s+([A-Z]\w*)|"
                     r"(?:const|let)\s+([A-Z]\w*)\s*[:=])", re.M)


def component_source(text: str, name: str | None) -> str:
    """The slice of a file holding one top-level component, so siblings don't bleed in."""
    if not name:
        return text
    defs = list(TOP_DEF.finditer(text))
    for i, m in enumerate(defs):
        if (m.group(1) or m.group(2)) == name:
            end = defs[i + 1].start() if i + 1 < len(defs) else len(text)
            return text[m.start():end]
    return text


def elements(repo: Repo, file: str, depth: int = 1, component: str | None = None) -> list[dict]:
    """Ordered UI elements from a component's markup (child components expanded to `depth`)."""
    full = repo.read(file, 200_000)
    text = component_source(full, component) if not file.endswith(".vue") else full
    if file.endswith(".vue"):
        m = re.search(r"<template>(.*)</template>", text, re.S)
        markup = m.group(1) if m else ""
    else:
        start = text.find("return (")
        markup = text[start + len("return ("):] if start >= 0 else text
    imports = _imports(repo, file, full)
    local = {(m.group(1) or m.group(2)) for m in TOP_DEF.finditer(full)} - {component}
    out: list[dict] = []
    stack: list[str] = []
    pos = 0
    for m in TAG.finditer(markup):
        closing, name, attrs, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        between = _text_of(markup[pos:m.start()])
        pos = m.end()
        open_tag = stack[-1] if stack else ""
        if between and len(between) > 1 and between != "…":
            kind = {"h1": "heading", "h2": "heading", "h3": "subheading", "button": "button",
                    "Button": "button", "a": "link", "Link": "link", "label": "label",
                    "th": "column", "li": "item", "option": "option"}.get(open_tag, "text")
            if kind in {"button", "link"} and out and out[-1]["type"] == kind and not out[-1]["label"]:
                out[-1]["label"] = between[:60]
            elif out and out[-1]["type"] == "checkbox" and out[-1]["label"] in {"", "checkbox", "radio"}:
                out[-1]["label"] = between[:60]  # <label><input type="checkbox"/> Text</label>
            else:
                out.append({"type": kind, "label": between[:80]})
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            continue
        a = {k: v1 or v2 or v3 for k, v1, v2, v3 in ATTR.findall(attrs)}
        lname = name.lower()
        if lname in {"input", "textarea", "select"} or name in {"Input", "TextField", "Select",
                                                              "Textarea", "TextInput"}:
            kind = "select" if "select" in lname else "textarea" if "textarea" in lname else (
                "checkbox" if a.get("type") in {"checkbox", "radio"} else "input")
            out.append({"type": kind, "label": a.get("placeholder") or a.get("label")
                        or a.get("aria-label") or a.get("name") or a.get("type") or ""})
        elif lname in {"button"} or name in {"Button", "IconButton"}:
            out.append({"type": "button", "label": a.get("aria-label") or a.get("title") or ""})
        elif lname in {"a"} or name in {"Link", "NavLink", "RouterLink", "router-link"}:
            out.append({"type": "link", "label": a.get("to") or a.get("href") or ""})
        elif lname in {"img", "svg"} or name in {"Image", "Avatar"}:
            out.append({"type": "image", "label": a.get("alt") or ""})
        elif lname in {"table"} or name in {"Table", "DataGrid", "DataTable"}:
            out.append({"type": "table", "label": ""})
        elif lname == "form" or name == "Form":
            out.append({"type": "form", "label": ""})
        elif lname in {"nav", "header", "footer", "aside"}:
            out.append({"type": lname, "label": ""})
        elif name[0].isupper() and name not in {"Fragment", "React.Fragment", "Suspense",
                                                "Route", "Routes", "Router", "Provider"}:
            base = name.split(".")[0]
            child = imports.get(base) or (file if base in local else None)
            if child and depth > 0 and (child != file or base in local):
                inner = elements(repo, child, depth - 1, base)
                out.append({"type": "component", "label": name, "children": inner[:40]})
            else:
                out.append({"type": "component", "label": name})
        if not selfclose and lname not in {"input", "img", "br", "hr"}:
            stack.append(name)
        if ".map(" in markup[max(0, m.start() - 80):m.start()] and out:
            out[-1]["repeated"] = True
    return out[:120]


def _box(e: dict) -> str:
    label = html.escape(e.get("label") or "")
    t = e["type"]
    rep = ' <span class="rep">× n</span>' if e.get("repeated") else ""
    if t == "heading":
        return f"<h2>{label or 'Heading'}</h2>"
    if t == "subheading":
        return f"<h3>{label or 'Section'}</h3>"
    if t in {"text", "label", "column", "option"}:
        return f'<p class="txt">{label}</p>' if label else '<p class="bar"></p>'
    if t == "button":
        return f'<span class="btn">{label or "Button"}</span>{rep}'
    if t == "link":
        return f'<span class="lnk">{label or "link"}</span>'
    if t in {"input", "textarea", "select"}:
        h = " tall" if t == "textarea" else ""
        caret = " ▾" if t == "select" else ""
        return f'<div class="inp{h}">{label or t}{caret}</div>'
    if t == "checkbox":
        return f'<div class="chk">☐ {label}</div>'
    if t == "image":
        return f'<div class="img">{label or "image"}</div>'
    if t == "table":
        return '<div class="tbl"><div></div><div></div><div></div></div>'
    if t == "item":
        return f'<div class="itm">{label}{rep}</div>'
    if t in {"nav", "header", "footer", "aside"}:
        return f'<div class="zone">{t}</div>'
    if t == "form":
        return '<div class="zone">form</div>'
    if t == "component":
        kids = "".join(_box(c) for c in e.get("children", []))
        return (f'<div class="cmp"><span class="cmpname">{label}{rep}</span>{kids or ""}</div>')
    return ""


WIRE_CSS = """body{font:13px system-ui,sans-serif;margin:0;padding:16px;color:#333;background:#fff}
h2{font-size:18px;margin:8px 0}h3{font-size:14px;margin:8px 0;color:#555}
.txt{margin:4px 0;color:#666}.bar{height:8px;background:#eee;border-radius:4px;width:60%;margin:8px 0}
.btn{display:inline-block;border:1.5px solid #333;border-radius:6px;padding:4px 12px;margin:4px 6px 4px 0;font-weight:600}
.lnk{display:inline-block;color:#2b6cb0;text-decoration:underline;margin:4px 8px 4px 0}
.inp{border:1.5px solid #aaa;border-radius:6px;padding:7px 10px;margin:6px 0;color:#999;max-width:420px}
.inp.tall{height:56px}.chk{margin:6px 0;color:#555}
.img{background:repeating-linear-gradient(45deg,#f2f2f2,#f2f2f2 8px,#e6e6e6 8px,#e6e6e6 16px);height:80px;border-radius:6px;margin:6px 0;display:flex;align-items:center;justify-content:center;color:#888}
.tbl{border:1px solid #ccc;border-radius:6px;margin:8px 0}.tbl div{height:22px;border-bottom:1px solid #eee}
.itm{border-bottom:1px solid #eee;padding:6px 0}.zone{background:#f6f6f6;border:1px dashed #bbb;border-radius:6px;padding:6px 10px;margin:6px 0;color:#888;text-transform:uppercase;font-size:10px}
.cmp{border:1px dashed #c9a24b;border-radius:8px;padding:8px 10px 6px;margin:8px 0;position:relative}
.cmpname{font-size:10px;color:#a07a20;font-weight:600;display:block;margin-bottom:4px}.rep{font-size:10px;color:#a07a20}"""


def render(page: dict) -> str:
    body = "".join(_box(e) for e in page["elements"]) or '<p class="txt">No markup found</p>'
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{WIRE_CSS}</style></head>"
            f"<body>{body}</body></html>")


def extract(repo: Repo) -> dict:
    files = _ui_files(repo)
    routes = _routes(repo, files)
    if not routes:
        candidates = [f for f in files if set(PurePosixPath(f).parts) & {"pages", "views", "screens"}
                      and f.endswith((".tsx", ".jsx", ".vue", ".svelte"))]
        if not candidates:
            candidates = [f for f in files if PurePosixPath(f).stem in {"App", "app"}
                          and f.endswith((".tsx", ".jsx", ".vue", ".svelte"))]
        routes = [{"path": "/" + PurePosixPath(f).stem.lower().replace("page", "").strip("-")
                   if PurePosixPath(f).stem not in {"App", "app"} else "/",
                   "component": PurePosixPath(f).stem, "file": f, "router": "inferred"}
                  for f in candidates[:20]]
    pages = []
    for r in routes[:30]:
        if not r.get("file") and r.get("component"):
            r["file"] = _find_component(repo, r["component"])
        comp = r.get("component") if r.get("router") != "Next.js app" else None
        els = elements(repo, r["file"], component=comp) if r.get("file") else []
        page = {**r, "elements": els}
        page["mock_html"] = render(page)
        pages.append(page)
    # State-driven apps (tabs instead of a router): each *Page/*View/*Screen child is a page.
    if len(pages) == 1 and pages[0]["router"] == "inferred":
        shell = pages[0]
        subs = [e for e in shell["elements"] if e["type"] == "component"
                and re.search(r"(Page|View|Screen)$", e["label"]) and e.get("children")]
        for e in subs[:20]:
            slug = re.sub(r"(Page|View|Screen)$", "", e["label"])
            sub = {"path": "/" + re.sub(r"(?<!^)(?=[A-Z])", "-", slug).lower(),
                   "component": e["label"], "file": shell["file"], "router": "app state",
                   "elements": e["children"]}
            sub["mock_html"] = render(sub)
            pages.append(sub)
    components = sorted({PurePosixPath(f).stem for f in files
                         if "components" in PurePosixPath(f).parts})
    return {"pages": pages, "components": components[:200]}
