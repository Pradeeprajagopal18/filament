"""Filament's code graph engine: per-file facts (`codeparse`) resolved into one graph.

Replaces running Graphify as a separate tool. Same idea and the same building blocks
(tree-sitter for parsing, Louvain clustering from networkx for subsystems), written for what
Filament needs:

- Modules, classes, functions and methods, with exact start and end lines.
- Edges: contains / method (structure), imports / imports_from, calls, inherits, references,
  and rationale_for (docstrings and NOTE/WHY/DECISION comments).
- Every edge says how we know it. `extracted`: resolved through the file's own scope, its
  imports, `self`/`this`, or the declared type of a variable, field or parameter
  (`repo.save()` where `repo: OrderRepo`). `inferred`: a name match only, used when the callee
  name is unique in the repo and not a common word.
- Incremental: facts are cached per file by content hash, so a rescan only parses files that
  changed; resolution over cached facts is fast.

Languages: Python, JavaScript, TypeScript (incl. JSX/TSX), Java, Go.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import defaultdict
from pathlib import Path

from filament.repos import codeparse

VERSION = 1
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_000_000
# Callee names too common to guess by name alone (they'd match the wrong thing).
STOP = {"get", "set", "add", "put", "post", "delete", "update", "remove", "append", "extend",
        "items", "keys", "values", "join", "split", "format", "read", "write", "close", "open",
        "send", "run", "start", "stop", "json", "list", "dict", "map", "filter", "find", "load",
        "save", "create", "init", "call", "apply", "bind", "then", "catch", "push", "pop",
        "next", "log", "info", "debug", "error", "warn", "print", "len", "str", "int", "toString",
        "equals", "hashCode", "String", "execute", "handle", "process", "parse",
        "render", "emit", "on", "off", "use", "new", "copy", "clear", "count", "sort", "reset",
        "Error", "Close", "Write", "Read", "Get", "Set", "Run", "Start", "Stop", "New", "Println",
        "Printf", "Sprintf", "Errorf", "main", "__init__", "constructor", "setUp", "test"}
SELF = {"self", "this", "cls"}
JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
JS_LANGS = {"javascript", "typescript", "tsx"}


# -- entry point -----------------------------------------------------------------------------------
def build(root: Path, files: list[str], cache_dir: Path | None = None) -> dict:
    """Parse source files under root (paths relative to it) and return the resolved graph."""
    cache_path = cache_dir / "facts.json" if cache_dir else None
    cache: dict = {}
    if cache_path and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if data.get("version") == [VERSION, codeparse.VERSION]:
                cache = data.get("files", {})
        except (OSError, json.JSONDecodeError):
            cache = {}
    facts: dict[str, dict] = {}
    fresh: dict[str, dict] = {}
    parsed = reused = skipped = 0
    go_mods: dict[str, str] = {}
    for rel in sorted(files):
        if rel.endswith("go.mod"):
            text = _read(root, rel)
            m = re.search(r"^module\s+(\S+)", text.decode("utf-8", "ignore") if text else "", re.M)
            if m:
                go_mods[posixpath.dirname(rel)] = m.group(1)
            continue
        lang = codeparse.language_for(rel)
        if not lang:
            continue
        data = _read(root, rel)
        if data is None or _minified(data):
            skipped += 1
            continue
        sha = hashlib.sha1(data).hexdigest()
        hit = cache.get(rel)
        if hit and hit.get("sha") == sha:
            facts[rel] = hit["facts"]
            reused += 1
        else:
            try:
                facts[rel] = codeparse.extract(data, lang)
            except Exception:  # noqa: BLE001 - one bad file never fails the scan
                skipped += 1
                continue
            parsed += 1
        fresh[rel] = {"sha": sha, "facts": facts[rel]}
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"version": [VERSION, codeparse.VERSION],
                                          "files": fresh}))
    resolver = Resolver(facts, go_mods)
    graph = resolver.run()
    langs: dict[str, int] = defaultdict(int)
    for f in facts.values():
        langs[f["lang"]] += 1
    errors = sum(f.get("errors", 0) for f in facts.values())
    graph.update({"languages": dict(langs), "files_parsed": parsed, "files_cached": reused,
                  "files_skipped": skipped,
                  "warnings": ([f"{errors} files had syntax errors; parsed what we could"]
                               if errors else []) +
                              ([f"{resolver.errors} calls or references could not be resolved "
                                "and were skipped"] if resolver.errors else [])})
    return graph


def _read(root: Path, rel: str) -> bytes | None:
    try:
        p = root / rel
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        return p.read_bytes()
    except OSError:
        return None


def _minified(data: bytes) -> bool:
    lines = data.count(b"\n") + 1
    return len(data) > 5000 and len(data) / lines > 400


# -- resolution --------------------------------------------------------------------------------------
class Resolver:
    def __init__(self, facts: dict[str, dict], go_mods: dict[str, str]):
        self.facts = facts
        self.go_mods = go_mods
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[tuple] = set()
        self.records: dict[str, dict] = {}            # node id -> symbol record
        self.top: dict[str, dict[str, str]] = defaultdict(dict)  # file -> name -> id
        self.members: dict[str, dict[str, str]] = defaultdict(dict)  # class -> name -> id
        self.bases: dict[str, list[str]] = defaultdict(list)
        self.owner: dict[str, str] = {}               # method id -> class id
        self._orphans: list[tuple[str, str, dict]] = []  # Go methods whose type is in another file
        self.errors = 0
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.types: dict[tuple, str] = {}
        self._scopes: dict[str, dict] = {}
        self._in_progress: set[str] = set()
        self.py_mod: dict[str, str] = {}
        self.py_suffix: dict[str, set[str]] = defaultdict(set)
        self.py_roots: set[str] = set()
        self.java_fqn: dict[str, str] = {}
        self.java_pkg: dict[str, list[str]] = defaultdict(list)
        self.go_dirs: dict[str, list[str]] = defaultdict(list)

    # -- helpers
    def sid(self, file: str, q: str) -> str:
        return f"{file}#{q}"

    def node(self, nid: str, **kw):
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, "label": kw.get("label", nid), "kind": kw.get("kind", "code"),
                               "file": kw.get("file"), "line": kw.get("line"), "end": kw.get("end"),
                               "community": None, "callable": kw.get("callable", False),
                               "is_class": kw.get("is_class", False)}
        return self.nodes[nid]

    def edge(self, s: str, t: str, rel: str, file: str | None, line: int | None,
             conf: str = "extracted"):
        if s == t or s not in self.nodes or t not in self.nodes:
            return
        key = (s, t, rel)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append({"source": s, "target": t, "relation": rel, "confidence": conf,
                           "score": 1.0 if conf == "extracted" else 0.5, "context": None,
                           "file": file, "line": line})

    def ext(self, pkg: str, name: str | None = None) -> str:
        nid = f"ext:{pkg}" + (f".{name}" if name else "")
        self.node(nid, label=name or pkg, kind="external")
        return nid

    # -- pass 1: symbols
    def declare(self):
        for file, f in self.facts.items():
            self.node(file, label=posixpath.basename(file), file=file, line=1)
            for s in f["symbols"]:
                nid = self.sid(file, s["q"])
                label = s["name"] if s["kind"] == "class" else (
                    f"{s['parent'].split('.')[-1]}.{s['name']}()" if s["kind"] == "method"
                    else f"{s['name']}()")
                self.node(nid, label=label, file=file, line=s["line"], end=s["end"],
                          callable=True, is_class=s["kind"] == "class")
                self.records[nid] = {**s, "file": file}
                self.by_name[s["name"]].append(nid)
                if s["parent"]:
                    parent = self.sid(file, s["parent"])
                    rel = "method" if s["kind"] == "method" else "contains"
                    if parent in self.records:
                        self.edge(parent, nid, rel, file, s["line"])
                        if s["kind"] == "method":
                            self.members[parent][s["name"]] = nid
                            self.owner[nid] = parent
                    elif s["kind"] == "method":
                        # Go: a method's type can be declared in another file of the package.
                        self._orphans.append((file, nid, s))
                        self.edge(file, nid, "contains", file, s["line"])
                else:
                    self.top[file][s["name"]] = nid
                    self.edge(file, nid, "contains", file, s["line"])
            for t in f.get("types", []):
                self.types[(file, t["in"], _strip_self(t["var"]))] = t["type"]
            lang = f["lang"]
            if lang == "python":
                dotted = file[:-3].replace("/", ".")
                if dotted.endswith(".__init__"):
                    dotted = dotted[:-9]
                self.py_mod[dotted] = file
                parts = dotted.split(".")
                for i in range(len(parts)):
                    self.py_suffix[".".join(parts[i:])].add(file)
                self.py_roots.add(parts[0])
            elif lang == "java":
                pkg = f.get("package") or ""
                self.java_pkg[pkg].append(file)
                for s in f["symbols"]:
                    if s["kind"] == "class":
                        self.java_fqn[f"{pkg}.{s['q']}" if pkg else s["q"]] = self.sid(file, s["q"])
            elif lang == "go":
                self.go_dirs[posixpath.dirname(file)].append(file)

    def attach_orphans(self) -> None:
        for file, nid, s in self._orphans:
            pkg_files = self.go_dirs.get(posixpath.dirname(file), [file])
            cls = next((self.top[f][s["parent"]] for f in pkg_files
                        if self.records.get(self.top[f].get(s["parent"], ""), {}).get("kind")
                        == "class"), None)
            if cls is None:
                continue  # type not in this repo (generated or vendored): keep it a free function
            self.edge(cls, nid, "method", file, s["line"])
            self.members[cls][s["name"]] = nid
            self.owner[nid] = cls

    # -- scopes: what each name in a file refers to
    def scope(self, file: str) -> dict:
        if file in self._scopes:
            return self._scopes[file]
        if file in self._in_progress:  # import cycle: use what we have so far
            return {}
        self._in_progress.add(file)
        f = self.facts[file]
        sc: dict[str, tuple] = {}
        lang = f["lang"]
        if lang == "java":
            for other in self.java_pkg.get(f.get("package") or "", []):
                for name, nid in self.top[other].items():
                    sc.setdefault(name, ("sym", nid))
        if lang == "go":
            for other in self.go_dirs.get(posixpath.dirname(file), []):
                for name, nid in self.top[other].items():
                    sc[name] = ("sym", nid)
        for name, nid in self.top[file].items():
            sc[name] = ("sym", nid)
        handler = {"python": self._py_imports, "java": self._java_imports, "go": self._go_imports}
        handler.get(lang, self._js_imports)(file, f, sc)
        self._in_progress.discard(file)
        self._scopes[file] = sc
        return sc

    def _import_edge(self, file, target, imp, rel="imports"):
        self.edge(file, target, rel, file, imp["line"])
        if imp.get("type_only") and self.edges and self.edges[-1]["source"] == file and \
                self.edges[-1]["target"] == target:
            self.edges[-1]["context"] = "type"

    def _py_resolve(self, dotted: str) -> str | None:
        if dotted in self.py_mod:
            return self.py_mod[dotted]
        cands = self.py_suffix.get(dotted, set())
        return next(iter(cands)) if len(cands) == 1 else None

    def _py_imports(self, file, f, sc):
        pkg = file[:-3].replace("/", ".").split(".")[:-1]
        for imp in f["imports"]:
            if imp["kind"] == "import":
                target = self._py_resolve(imp["module"])
                key = imp["alias"] or imp["module"]
                if target:
                    sc[key] = ("mod", [target])
                    self._import_edge(file, target, imp)
                elif imp["module"].split(".")[0] not in self.py_roots:
                    top = imp["module"].split(".")[0]
                    sc[key] = ("ext", imp["module"])
                    sc.setdefault(top, ("ext", top))
                    self._import_edge(file, self.ext(top), imp)
                continue
            if imp["level"]:
                # a/b/c.py and a/b/__init__.py both sit in package a.b; each extra dot goes up one
                base_parts = pkg[: len(pkg) - (imp["level"] - 1)]
                base = ".".join(base_parts + ([imp["module"]] if imp["module"] else []))
            else:
                base = imp["module"]
            base_file = self._py_resolve(base) if base else None
            external = not imp["level"] and not base_file and base.split(".")[0] not in self.py_roots
            for name, alias in imp["names"]:
                if name == "*":
                    if base_file:
                        for n, nid in self.top[base_file].items():
                            sc.setdefault(n, ("sym", nid))
                        self._import_edge(file, base_file, imp, "imports_from")
                    continue
                sub = self._py_resolve(f"{base}.{name}" if base else name)
                if sub and sub != base_file:
                    sc[alias] = ("mod", [sub])
                    self._import_edge(file, sub, imp, "imports_from")
                elif base_file:
                    target = self.top[base_file].get(name) or self._reexport(base_file, name)
                    if target:
                        sc[alias] = target if isinstance(target, tuple) else ("sym", target)
                    self._import_edge(file, base_file, imp, "imports_from")
                elif external:
                    top = base.split(".")[0]
                    sc[alias] = ("ext", f"{base}.{name}")
                    self._import_edge(file, self.ext(top), imp, "imports_from")

    def _reexport(self, file: str, name: str):
        """`from pkg import thing` where pkg/__init__ (or index.ts) re-exports it."""
        hit = self.scope(file).get(name)
        return hit if hit and hit[0] == "sym" else None

    def _js_resolve(self, file: str, spec: str) -> str | None:
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(file), spec))
            cands = [base]
        elif spec.startswith(("@/", "~/")):
            cands = [posixpath.join("src", spec[2:]), spec[2:]]
        else:
            return None
        for c in cands:
            for opt in [c, *(c + e for e in JS_EXT), *(f"{c}/index{e}" for e in JS_EXT)]:
                if opt in self.facts and self.facts[opt]["lang"] in JS_LANGS:
                    return opt
            if c.endswith((".js", ".jsx")):  # TS projects import './x.js' for x.ts
                for e in (".ts", ".tsx"):
                    opt = c.rsplit(".", 1)[0] + e
                    if opt in self.facts:
                        return opt
        return None

    def _js_imports(self, file, f, sc):
        for imp in f["imports"]:
            spec = imp["module"]
            target = self._js_resolve(file, spec)
            if target is None and (spec.startswith((".", "@/", "~/")) or not spec):
                continue  # internal but not found (asset, generated file)
            if target is None:
                pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
                if spec.startswith("node:"):
                    pkg = spec
                node = self.ext(pkg)
                self._import_edge(file, node, imp, "imports_from" if imp["names"] else "imports")
                for _name, alias in imp["names"]:
                    sc[alias] = ("ext", pkg)
                if imp["alias"]:
                    sc[imp["alias"]] = ("ext", pkg)
                continue
            self._import_edge(file, target, imp, "imports_from" if imp["names"] else "imports")
            if imp["alias"]:
                sc[imp["alias"]] = ("mod", [target])
            for name, alias in imp["names"]:
                if name == "default":
                    stem = posixpath.basename(target).split(".")[0]
                    hit = self.top[target].get(alias) or self.top[target].get(stem) or (
                        next(iter(self.top[target].values())) if len(self.top[target]) == 1 else None)
                    sc[alias] = ("sym", hit) if hit else ("mod", [target])
                else:
                    hit = self.top[target].get(name) or self._reexport(target, name)
                    if hit:
                        sc[alias] = hit if isinstance(hit, tuple) else ("sym", hit)

    def _java_imports(self, file, f, sc):
        for imp in f["imports"]:
            mod = imp["module"]
            if mod.endswith(".*"):
                pkg = mod[:-2]
                for other in self.java_pkg.get(pkg, []):
                    for name, nid in self.top[other].items():
                        sc.setdefault(name, ("sym", nid))
                    self._import_edge(file, other, imp)
                if pkg not in self.java_pkg:
                    self._import_edge(file, self.ext(".".join(pkg.split(".")[:2])), imp)
                continue
            if imp["kind"] == "static":
                cls, _, member = mod.rpartition(".")
                cid = self.java_fqn.get(cls)
                if cid and member in self.members[cid]:
                    sc[member] = ("sym", self.members[cid][member])
                    self._import_edge(file, self.records[cid]["file"], imp)
                continue
            cid = self.java_fqn.get(mod)
            name = mod.rsplit(".", 1)[-1]
            if cid:
                sc[name] = ("sym", cid)
                self._import_edge(file, self.records[cid]["file"], imp)
            else:
                pkg = ".".join(mod.split(".")[:2])
                sc[name] = ("ext", pkg)
                self._import_edge(file, self.ext(pkg), imp)

    def _go_dir(self, path: str) -> str | None:
        for mod_dir, module in sorted(self.go_mods.items(), key=lambda x: -len(x[1])):
            if path == module or path.startswith(module + "/"):
                rest = path[len(module):].lstrip("/")
                d = posixpath.normpath(posixpath.join(mod_dir, rest)) if (mod_dir or rest) else ""
                d = "" if d == "." else d
                return d if d in self.go_dirs else None
        cands = [d for d in self.go_dirs if d and (path == d or path.endswith("/" + d))]
        return cands[0] if len(cands) == 1 else None

    def _go_imports(self, file, f, sc):
        for imp in f["imports"]:
            path = imp["module"]
            d = self._go_dir(path)
            last = path.rsplit("/", 1)[-1]
            if d is not None:
                files = self.go_dirs[d]
                pkg_name = next((self.facts[x].get("package") for x in files
                                 if self.facts[x].get("package")), last)
                sc[imp["alias"] or pkg_name] = ("mod", files)
                for target in files[:10]:
                    self._import_edge(file, target, imp)
            else:
                node = self.ext(path if "." in path.split("/")[0] else last)
                names = {imp["alias"]} if imp["alias"] else {
                    last, last.replace("-go", "").replace("go-", ""), last.replace("-", "")}
                for n in names:
                    sc[n] = ("ext", path)
                self._import_edge(file, node, imp)

    # -- lookups
    def member(self, cls: str, name: str, depth: int = 0) -> str | None:
        if cls in self.members and name in self.members[cls]:
            return self.members[cls][name]
        if depth > 5:
            return None
        for b in self.bases.get(cls, []):
            hit = self.member(b, name, depth + 1)
            if hit:
                return hit
        return None

    def enclosing_class(self, file: str, q: str | None) -> str | None:
        while q:
            nid = self.sid(file, q)
            rec = self.records.get(nid)
            if rec and rec["kind"] == "class":
                return nid
            if rec and rec["kind"] == "method":
                return self.owner.get(nid)
            q = rec["parent"] if rec else None
        return None

    def lookup(self, file: str, text: str):
        """Resolve a name or dotted expression in a file: ('sym', id) | ('mod', files) | ('ext', pkg)."""
        sc = self.scope(file)
        if text in sc:
            return sc[text]
        head, _, rest = text.partition(".")
        hit = sc.get(head)
        if not hit or not rest:
            return None
        if hit[0] == "ext":
            return hit
        if hit[0] == "mod":
            for target in hit[1]:
                for part in [rest.split(".")[0]]:
                    if part in self.top[target]:
                        nid = self.top[target][part]
                        tail = rest.split(".")[1:]
                        return ("sym", self.member(nid, tail[0]) or nid) if tail else ("sym", nid)
        if hit[0] == "sym":
            return ("sym", self.member(hit[1], rest.split(".")[0]) or hit[1])
        return None

    def type_class(self, file: str, type_text: str):
        """A declared type -> class node (or ('ext', pkg) for a library type)."""
        tokens = re.findall(r"[A-Za-z_][\w.]*", type_text)
        skip = {"Optional", "List", "list", "Promise", "Array", "Set", "Map", "Dict", "dict",
                "Union", "Annotated", "readonly", "const", "final", "Mono", "Flux", "Iterable",
                "Sequence", "type", "Type", "Awaitable", "Observable", "None", "null",
                "undefined"}
        for tok in tokens:
            if tok in skip:
                continue
            hit = self.lookup(file, tok)
            if hit and hit[0] == "sym" and self.records.get(hit[1], {}).get("kind") == "class":
                return hit
            if hit and hit[0] == "ext":
                return hit
        return None

    def var_type(self, file: str, scope_q: str | None, var: str) -> str | None:
        var = _strip_self(var)
        q = scope_q
        while q:  # function, enclosing functions, then the class (fields)
            t = self.types.get((file, q, var))
            if t:
                return t
            rec = self.records.get(self.sid(file, q))
            q = rec["parent"] if rec else None
        return self.types.get((file, None, var))

    # -- pass 2: imports, inheritance, calls, references, notes
    def resolve_bases(self):
        for nid, rec in self.records.items():
            if rec["kind"] != "class":
                continue
            for base in rec["bases"]:
                hit = self.lookup(rec["file"], base)
                if hit and hit[0] == "sym" and hit[1] != nid:
                    self.bases[nid].append(hit[1])
                    self.edge(nid, hit[1], "inherits", rec["file"], rec["line"])
                elif hit and hit[0] == "ext":
                    pkg = hit[1].split(".")[0] if "/" not in hit[1] else hit[1]
                    self.edge(nid, self.ext(pkg, base.split(".")[-1]), "inherits", rec["file"],
                              rec["line"])

    def resolve_call(self, file: str, f: dict, call: dict) -> tuple[str, str] | None:
        q, recv, name = call["in"], call["recv"] or "", call["name"]
        lang = f["lang"]
        caller_rec = self.records.get(self.sid(file, q)) if q else None
        cls = self.enclosing_class(file, q)
        if not recv:
            # nested function, then own scope / imports
            scope_q = q
            while scope_q:
                nested = self.sid(file, f"{scope_q}.{name}")
                if nested in self.records:
                    return nested, "extracted"
                rec = self.records.get(self.sid(file, scope_q))
                scope_q = rec["parent"] if rec else None
            if lang == "java" and cls:
                hit = self.member(cls, name)
                if hit:
                    return hit, "extracted"
            hit = self.scope(file).get(name)
            if hit and hit[0] == "sym":
                return hit[1], "extracted"
            if hit:  # external or a module object: not ours
                return None
            return self.guess(name, functions_only=True)
        recv_var = caller_rec.get("recv_var") if caller_rec else None
        if recv in SELF or recv in {"super()", "super"} or (recv_var and recv == recv_var):
            if not cls:
                return None
            if recv.startswith("super"):
                for b in self.bases.get(cls, []):
                    hit = self.member(b, name)
                    if hit:
                        return hit, "extracted"
                return None
            hit = self.member(cls, name)
            return (hit, "extracted") if hit else None
        # typed receiver: self.repo / this.repo / repo / s.DB
        head = recv
        if recv_var and recv.startswith(recv_var + "."):
            head = recv[len(recv_var) + 1:]
        if re.fullmatch(r"[\w.]+", head):
            t = self.var_type(file, q, head) if "." not in _strip_self(head) else None
            if t is None and cls and (recv.startswith(("self.", "this.")) or head != recv or
                                      lang == "java"):
                owner = self.records[cls]  # fields live with the type, maybe in another file
                t = self.types.get((owner["file"], owner["q"], _strip_self(head)))
            if t:
                hit = self.type_class(file, t)
                if hit and hit[0] == "ext":
                    return None
                if hit:
                    m = self.member(hit[1], name)
                    return (m, "extracted") if m else None
        hit = self.lookup(file, recv)
        if hit:
            if hit[0] == "ext":
                return None
            if hit[0] == "mod":
                for target in hit[1]:
                    nid = self.top[target].get(name)
                    if nid:
                        return nid, "extracted"
                return None
            if hit[0] == "sym":
                rec = self.records.get(hit[1], {})
                if rec.get("kind") == "class":
                    m = self.member(hit[1], name)
                    return (m, "extracted") if m else None
        if self.scope(file).get(recv.split(".")[0], ("",))[0] == "ext":
            return None
        return self.guess(name, functions_only=False, recv=recv)

    def guess(self, name: str, functions_only: bool, recv: str = "") -> tuple[str, str] | None:
        """Name-only match, marked inferred. Only for callee names unique in the repo and not
        common words; a method on an unknown receiver also needs the receiver's name to look
        like the class (`turn.content()` -> ModelTurn.content)."""
        if name in STOP or len(name) < 4 or name.startswith("__"):
            return None
        want_method = not functions_only
        cands = [n for n in self.by_name.get(name, [])
                 if (self.records[n]["kind"] == "method") == want_method]
        if len(cands) != 1:
            return None
        if recv:
            word = re.sub(r"[^a-z]", "", _strip_self(recv).split(".")[-1].lower())
            owner = re.sub(r"[^a-z]", "", (self.records[cands[0]]["parent"] or "").split(".")[-1].lower())
            if len(word) < 3 or not owner or (word not in owner and owner not in word):
                return None
        return cands[0], "inferred"

    def resolve_refs(self, file: str, f: dict, called: set[tuple]):
        for r in f["refs"]:
            src = self.sid(file, r["in"]) if r["in"] else file
            hit = self.scope(file).get(r["name"])
            if not hit or hit[0] != "sym" or (src, hit[1]) in called:
                continue
            self.edge(src, hit[1], "references", file, r["line"])

    def notes(self, file: str, f: dict):
        for n in f["notes"]:
            nid = f"{file}#L{n['line']}"
            self.node(nid, label=n["text"][:300], kind="rationale", file=file, line=n["line"])
            target = self.sid(file, n["in"]) if n["in"] else file
            self.edge(nid, target if target in self.nodes else file, "rationale_for", file,
                      n["line"])

    def run(self) -> dict:
        self.declare()
        self.attach_orphans()
        for file in self.facts:
            self.scope(file)
        self.resolve_bases()
        for file, f in self.facts.items():
            # One odd file or call must not fail the scan: skip it and count it.
            called: set[tuple] = set()
            for call in f["calls"]:
                try:
                    hit = self.resolve_call(file, f, call)
                except Exception:  # noqa: BLE001
                    self.errors += 1
                    continue
                if not hit:
                    continue
                src = self.sid(file, call["in"]) if call["in"] else file
                self.edge(src, hit[0], "calls", file, call["line"], hit[1])
                called.add((src, hit[0]))
            try:
                self.resolve_refs(file, f, called)
                self.notes(file, f)
            except Exception:  # noqa: BLE001
                self.errors += 1
        communities(self.nodes, self.edges)
        return {"nodes": list(self.nodes.values()), "edges": self.edges,
                "schema_version": SCHEMA_VERSION, "engine": "filament",
                "engine_version": f"{VERSION}.{codeparse.VERSION}", "directed": True}


def _strip_self(var: str) -> str:
    for p in ("self.", "this.", "cls."):
        if var.startswith(p):
            return var[len(p):]
    return var


# -- subsystems ---------------------------------------------------------------------------------------
def communities(nodes: dict[str, dict], edges: list[dict]) -> None:
    """Louvain clustering over modules and symbols (seeded, so stable between scans).
    Notes join the community of what they explain; external nodes stay unassigned."""
    import networkx as nx

    g = nx.Graph()
    code = {nid for nid, n in nodes.items() if n["kind"] == "code"}
    g.add_nodes_from(code)
    for e in edges:
        s, t = e["source"], e["target"]
        if s in code and t in code:
            w = 0.5 if e["relation"] in {"contains", "method"} else 1.0
            g.add_edge(s, t, weight=g.get_edge_data(s, t, {"weight": 0})["weight"] + w)
    if not g.number_of_nodes():
        return
    parts = nx.community.louvain_communities(g, weight="weight", seed=7)
    for cid, members in enumerate(sorted(parts, key=lambda p: (-len(p), min(p)))):
        for nid in members:
            nodes[nid]["community"] = cid
    for e in edges:
        if e["relation"] == "rationale_for" and e["target"] in nodes:
            nodes[e["source"]]["community"] = nodes[e["target"]]["community"]
