"""Per-file facts for the code graph: symbols, imports, calls, references and design notes.

Ignite's own extractor (replaces running Graphify as a separate tool). It uses the same
building blocks Graphify does, tree-sitter grammars, but only for the languages Ignite reads
APIs from (Python, JavaScript/TypeScript, Java, Go) and only the facts the code graph needs.
Output is plain JSON so it can be cached per file by content hash; cross-file resolution happens
in `codeindex.py`.

Facts for one file:
  symbols  [{q, name, kind: class|function|method, line, end, parent, bases, recv_var}]
  imports  [{module, names: [[name, alias]], alias, line, level, in_function, kind, type_only?}]
  calls    [{in, recv, name, line}]        in = enclosing symbol q, or None for module level
  refs     [{in, name, line}]              names used but not called (types, Depends(fn), JSX)
  notes    [{text, line, in, doc}]         docstrings and NOTE/WHY/DECISION... comments
  types    [{in, var, type}]               declared/constructed types of params, fields, locals
  package  java package / go package name
"""

from __future__ import annotations

import re
from functools import cache

VERSION = 2  # bump when facts change shape, so caches are rebuilt

EXTENSIONS = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
              ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java",
              ".go": "go"}
NOTE_RX = re.compile(r"\b(NOTE|WHY|HACK|IMPORTANT|DECISION)\s*[:\-]", re.I)


@cache
def parser(lang: str):
    from tree_sitter import Language, Parser

    if lang == "python":
        import tree_sitter_python as m

        language = m.language()
    elif lang == "javascript":
        import tree_sitter_javascript as m

        language = m.language()
    elif lang in {"typescript", "tsx"}:
        import tree_sitter_typescript as m

        language = m.language_tsx() if lang == "tsx" else m.language_typescript()
    elif lang == "java":
        import tree_sitter_java as m

        language = m.language()
    elif lang == "go":
        import tree_sitter_go as m

        language = m.language()
    else:
        raise ValueError(lang)
    return Parser(Language(language))


def language_for(path: str) -> str | None:
    if path.endswith(".d.ts"):
        return None
    for ext, lang in EXTENSIONS.items():
        if path.endswith(ext):
            return lang
    return None


def available() -> bool:
    try:
        parser("python")
        return True
    except Exception:  # noqa: BLE001 - grammar packages missing
        return False


def _t(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _line(node) -> int:
    return node.start_point[0] + 1


def _end(node) -> int:
    return node.end_point[0] + 1


class _Facts:
    def __init__(self):
        self.symbols: list[dict] = []
        self.imports: list[dict] = []
        self.calls: list[dict] = []
        self.refs: list[dict] = []
        self.notes: list[dict] = []
        self.types: list[dict] = []
        self.type_ranges: list[tuple[int, int]] = []
        self.package: str | None = None

    def var(self, scope, name: str, type_text: str):
        name, type_text = (name or "").strip(), (type_text or "").strip()
        if name and type_text and len(type_text) < 200:
            self.types.append({"in": scope, "var": name, "type": type_text})

    def sym(self, q, name, kind, node, parent=None, bases=None, recv_var=None):
        self.symbols.append({"q": q, "name": name, "kind": kind, "line": _line(node),
                             "end": _end(node), "parent": parent, "bases": bases or [],
                             "recv_var": recv_var})

    def to_dict(self) -> dict:
        for imp in self.imports:  # imports only for type checkers don't run (or form cycles)
            if any(a <= imp["line"] <= b for a, b in self.type_ranges):
                imp["type_only"] = True
        return {"symbols": self.symbols, "imports": self.imports, "calls": self.calls,
                "refs": _dedupe(self.refs), "notes": self.notes, "types": self.types,
                "package": self.package}


def _dedupe(refs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in refs:
        k = (r["in"], r["name"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _comment_note(f: _Facts, node, scope):
    text = _t(node).strip()
    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if NOTE_RX.search(line):
            f.notes.append({"text": line, "line": _line(node) + i, "in": scope, "doc": False})


# -- python ------------------------------------------------------------------------------------
def _py(root, f: _Facts):
    def callee(fn):
        if fn.type == "identifier":
            return "", _t(fn)
        if fn.type == "attribute":
            return _t(fn.child_by_field_name("object")), _t(fn.child_by_field_name("attribute"))
        return None, None

    def docstring(body, scope):
        first = next((c for c in body.children if c.type not in {"comment"}), None) if body else None
        if first is not None and first.type == "expression_statement" and first.children and \
                first.children[0].type == "string":
            text = _t(first.children[0]).strip("\"' \n")
            line = next((x.strip() for x in text.splitlines() if x.strip()), "")
            if line:
                f.notes.append({"text": line, "line": _line(first), "in": scope, "doc": True})

    def walk(node, scope, cls, in_fn):
        t = node.type
        if t in {"function_definition", "class_definition"}:
            name = _t(node.child_by_field_name("name"))
            q = f"{scope}.{name}" if scope else name
            body = node.child_by_field_name("body")
            for c in node.children:  # comments between the signature and the body
                if c.type == "comment":
                    _comment_note(f, c, q)
            if t == "class_definition":
                sup = node.child_by_field_name("superclasses")
                bases = [_t(c) for c in (sup.named_children if sup else [])
                         if c.type in {"identifier", "attribute"}]
                f.sym(q, name, "class", node, scope, bases)
                docstring(body, q)
                for c in body.children if body else []:
                    walk(c, q, q, False)
                return
            kind = "method" if cls and scope == cls else "function"
            f.sym(q, name, kind, node, scope)
            docstring(body, q)
            params = node.child_by_field_name("parameters")
            for prm in params.named_children if params else []:
                ty, val = prm.child_by_field_name("type"), prm.child_by_field_name("value")
                if ty is not None:
                    idents(ty, q)
                    pname = next((c for c in prm.children if c.type == "identifier"), None)
                    f.var(q, _t(pname), _t(ty))
                if val is not None:
                    walk(val, q, None, True)
            rt = node.child_by_field_name("return_type")
            if rt is not None:
                idents(rt, q)
            for c in body.children if body else []:
                walk(c, q, None, True)
            return
        if t == "decorated_definition":
            for c in node.children:
                walk(c, scope, cls, in_fn)
            return
        if t in {"import_statement", "import_from_statement"}:
            _py_import(node, f, in_fn)
            return
        if t == "call":
            fn = node.child_by_field_name("function")
            recv, name = callee(fn)
            if name:
                f.calls.append({"in": scope, "recv": recv, "name": name, "line": _line(node)})
            if fn is not None and fn.type == "attribute":
                walk(fn.child_by_field_name("object"), scope, cls, in_fn)
            args = node.child_by_field_name("arguments")
            for c in args.children if args is not None else []:
                v = c.child_by_field_name("value") if c.type == "keyword_argument" else c
                if v is not None and v.type == "identifier":
                    f.refs.append({"in": scope, "name": _t(v), "line": _line(v)})
                walk(c, scope, cls, in_fn)
            return
        if t == "comment":
            _comment_note(f, node, scope)
            return
        if t == "type":
            idents(node, scope)
            return
        if t == "if_statement" and _t(node.child_by_field_name("condition")).endswith("TYPE_CHECKING"):
            f.type_ranges.append((_line(node), _end(node)))
        if t == "assignment":
            left, right, ty = (node.child_by_field_name(k) for k in ("left", "right", "type"))
            if left is not None and left.type in {"identifier", "attribute"}:
                if ty is not None:
                    f.var(scope, _t(left), _t(ty))
                elif right is not None and right.type == "call":
                    fn = right.child_by_field_name("function")
                    f.var(_owner(scope, _t(left)), _t(left), _t(fn))
        for c in node.children:
            walk(c, scope, cls, in_fn)

    def _owner(scope, target):
        """self.x = Client() in a method types x for the whole class."""
        if target.startswith(("self.", "cls.")) and scope and "." in scope:
            return scope.rsplit(".", 1)[0]
        return scope

    def idents(node, scope):
        for x in _descend(node, {"identifier"}):
            f.refs.append({"in": scope, "name": _t(x), "line": _line(x)})

    docstring(root, None)
    for c in root.children:
        walk(c, None, None, False)


def _py_import(node, f: _Facts, in_fn: bool):
    line = _line(node)
    if node.type == "import_statement":
        for c in node.named_children:
            if c.type == "dotted_name":
                f.imports.append({"module": _t(c), "names": [], "alias": None, "line": line,
                                  "level": 0, "in_function": in_fn, "kind": "import"})
            elif c.type == "aliased_import":
                f.imports.append({"module": _t(c.child_by_field_name("name")), "names": [],
                                  "alias": _t(c.child_by_field_name("alias")), "line": line,
                                  "level": 0, "in_function": in_fn, "kind": "import"})
        return
    mod = node.child_by_field_name("module_name")
    text = _t(mod)
    level = len(text) - len(text.lstrip("."))
    names = []
    for c in node.named_children:
        if mod is not None and c.start_byte == mod.start_byte:
            continue
        if c.type == "dotted_name":
            names.append([_t(c), _t(c)])
        elif c.type == "aliased_import":
            names.append([_t(c.child_by_field_name("name")), _t(c.child_by_field_name("alias"))])
        elif c.type == "wildcard_import":
            names.append(["*", "*"])
    f.imports.append({"module": text.lstrip("."), "names": names, "alias": None, "line": line,
                      "level": level, "in_function": in_fn, "kind": "from"})


# -- javascript / typescript ---------------------------------------------------------------------
JS_FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
JS_CLASSES = {"class_declaration", "abstract_class_declaration", "interface_declaration", "class"}


def _js(root, f: _Facts):
    def callee(fn):
        if fn is None:
            return None, None
        if fn.type == "identifier":
            return "", _t(fn)
        if fn.type == "member_expression":
            return _t(fn.child_by_field_name("object")), _t(fn.child_by_field_name("property"))
        return None, None

    def define(q, name, kind, node, scope, value_node=None):
        f.sym(q, name, kind, node, scope)
        body = (value_node or node).child_by_field_name("body")
        params = (value_node or node).child_by_field_name("parameters")
        if params is not None:
            types(params, q)
            for prm in params.named_children:
                walk(prm, q, None, True)
        rt = (value_node or node).child_by_field_name("return_type")
        if rt is not None:
            types(rt, q)
        if body is not None:
            walk(body, q, None, True)

    def walk(node, scope, cls, in_fn):
        t = node.type
        if t in JS_CLASSES and node.child_by_field_name("name") is not None:
            name = _t(node.child_by_field_name("name"))
            q = f"{scope}.{name}" if scope else name
            bases = []
            for c in node.children:
                if c.type in {"class_heritage", "extends_type_clause", "extends_clause"}:
                    for x in _descend(c, {"identifier", "type_identifier", "member_expression"}):
                        bases.append(_t(x))
            f.sym(q, name, "class", node, scope, bases)
            body = node.child_by_field_name("body")
            for c in body.children if body else []:
                walk(c, q, q, False)
            return
        if t in {"function_declaration", "generator_function_declaration"}:
            name = _t(node.child_by_field_name("name"))
            define(f"{scope}.{name}" if scope else name, name, "function", node, scope)
            return
        if t == "method_definition" and cls:
            name = _t(node.child_by_field_name("name"))
            define(f"{cls}.{name}", name, "method", node, cls)
            return
        if t in {"public_field_definition", "field_definition"} and cls:
            val = node.child_by_field_name("value")
            if val is not None and val.type in JS_FUNC_VALUES:
                name = _t(node.child_by_field_name("name") or node.child_by_field_name("property"))
                define(f"{cls}.{name}", name, "method", node, cls, val)
                return
        if t in {"required_parameter", "optional_parameter"}:
            pat, ty = node.child_by_field_name("pattern"), node.child_by_field_name("type")
            if pat is not None and ty is not None:
                f.var(scope, _t(pat).removeprefix("private ").removeprefix("public ")
                      .removeprefix("readonly ").strip(), _t(ty).lstrip(": "))
                acc = next((c for c in node.children if c.type == "accessibility_modifier"), None)
                if acc is not None and cls is None and scope and "." in scope:
                    f.var(scope.rsplit(".", 1)[0], _t(pat), _t(ty).lstrip(": "))
        if t in {"public_field_definition", "field_definition"} and cls:
            nm, ty = node.child_by_field_name("name"), node.child_by_field_name("type")
            val = node.child_by_field_name("value")
            if nm is not None and ty is not None:
                f.var(cls, _t(nm), _t(ty).lstrip(": "))
            elif nm is not None and val is not None and val.type == "new_expression":
                f.var(cls, _t(nm), _t(val.child_by_field_name("constructor")))
        if t == "variable_declarator":
            val, nm = node.child_by_field_name("value"), node.child_by_field_name("name")
            ty = node.child_by_field_name("type")
            if nm is not None and nm.type == "identifier":
                if ty is not None:
                    f.var(scope, _t(nm), _t(ty).lstrip(": "))
                elif val is not None and val.type == "new_expression":
                    f.var(scope, _t(nm), _t(val.child_by_field_name("constructor")))
            if val is not None and nm is not None and nm.type == "identifier":
                if val.type in JS_FUNC_VALUES:
                    name = _t(nm)
                    define(f"{scope}.{name}" if scope else name, name, "function", node, scope, val)
                    return
                if val.type == "call_expression" and _t(val.child_by_field_name("function")) == "require":
                    _js_require(val, f, in_fn, alias=_t(nm))
                    return
            if val is not None and nm is not None and nm.type == "object_pattern" and \
                    val.type == "call_expression" and _t(val.child_by_field_name("function")) == "require":
                _js_require(val, f, in_fn, names=[_t(x) for x in _descend(
                    nm, {"shorthand_property_identifier_pattern"})])
                return
        if t == "import_statement":
            _js_import(node, f, in_fn)
            return
        if t == "assignment_expression" and cls is None and scope and "." in scope:
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None and _t(left).startswith("this.") and \
                    right.type == "new_expression":
                f.var(scope.rsplit(".", 1)[0], _t(left)[5:], _t(right.child_by_field_name("constructor")))
        if t in {"call_expression", "new_expression"}:
            fn = node.child_by_field_name("function") or node.child_by_field_name("constructor")
            recv, name = callee(fn)
            if name == "require" and not recv:
                _js_require(node, f, in_fn)
                return
            if name:
                f.calls.append({"in": scope, "recv": recv, "name": name, "line": _line(node)})
            if fn is not None and fn.type == "member_expression":
                walk(fn.child_by_field_name("object"), scope, cls, in_fn)
            args = node.child_by_field_name("arguments")
            for c in args.children if args else []:
                if c.type == "identifier":
                    f.refs.append({"in": scope, "name": _t(c), "line": _line(c)})
                walk(c, scope, cls, in_fn)
            return
        if t in {"jsx_opening_element", "jsx_self_closing_element"}:
            nm = node.child_by_field_name("name")
            if nm is not None and _t(nm)[:1].isupper():
                f.refs.append({"in": scope, "name": _t(nm).split(".")[0], "line": _line(node)})
        if t in {"type_annotation", "type_arguments"}:
            types(node, scope)
            return
        if t == "comment":
            _comment_note(f, node, scope)
            if _t(node).startswith("/**"):
                nxt = node.next_named_sibling
                doc = _jsdoc(_t(node))
                if doc and nxt is not None:
                    f.notes.append({"text": doc, "line": _line(node), "in": ("@next", _line(nxt)),
                                    "doc": True})
            return
        for c in node.children:
            walk(c, scope, cls, in_fn)

    def types(node, scope):
        for x in _descend(node, {"type_identifier"}):
            f.refs.append({"in": scope, "name": _t(x), "line": _line(x)})

    for c in root.children:
        walk(c, None, None, False)


def _jsdoc(text: str) -> str:
    for raw in text.strip("/*").splitlines():
        line = raw.strip().lstrip("*").strip()
        if line and not line.startswith("@"):
            return line
    return ""


def _descend(node, types: set[str]):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            yield n
            continue
        stack.extend(reversed(n.children))


def _string(node) -> str:
    return _t(node).strip("'\"`")


def _js_import(node, f: _Facts, in_fn: bool):
    src = node.child_by_field_name("source")
    if src is None:
        return
    names, alias = [], None
    for clause in node.children:
        if clause.type != "import_clause":
            continue
        for c in clause.children:
            if c.type == "identifier":
                names.append(["default", _t(c)])
            elif c.type == "namespace_import":
                alias = _t(next((x for x in c.children if x.type == "identifier"), None))
            elif c.type == "named_imports":
                for s in c.named_children:
                    if s.type == "import_specifier":
                        nm, al = s.child_by_field_name("name"), s.child_by_field_name("alias")
                        names.append([_t(nm), _t(al or nm)])
    type_only = any(c.type == "type" for c in node.children)
    f.imports.append({"module": _string(src), "names": names, "alias": alias,
                      "line": _line(node), "level": 0, "in_function": in_fn, "kind": "from",
                      "type_only": type_only})


def _js_require(call, f: _Facts, in_fn: bool, alias: str | None = None, names=None):
    args = call.child_by_field_name("arguments")
    arg = next((c for c in args.named_children if c.type == "string"), None) if args else None
    if arg is None:
        return
    f.imports.append({"module": _string(arg), "names": [[n, n] for n in names or []],
                      "alias": alias, "line": _line(call), "level": 0, "in_function": in_fn,
                      "kind": "from" if names else "import"})


# -- java ----------------------------------------------------------------------------------------
JAVA_CLASSES = {"class_declaration", "interface_declaration", "enum_declaration",
                "record_declaration", "annotation_type_declaration"}


def _java(root, f: _Facts):
    def walk(node, scope, cls):
        t = node.type
        if t == "package_declaration":
            f.package = _t(next((c for c in node.named_children if "identifier" in c.type), None))
            return
        if t == "import_declaration":
            text = _t(node).removeprefix("import").strip().rstrip(";").strip()
            static = text.startswith("static ")
            text = text.removeprefix("static ").strip()
            f.imports.append({"module": text, "names": [], "alias": None, "line": _line(node),
                              "level": 0, "in_function": False,
                              "kind": "static" if static else "import"})
            return
        if t in JAVA_CLASSES:
            name = _t(node.child_by_field_name("name"))
            q = f"{scope}.{name}" if scope else name
            bases = [_t(x) for c in node.children if c.type in {"superclass", "super_interfaces",
                                                                 "extends_interfaces"}
                     for x in _descend(c, {"type_identifier", "scoped_type_identifier"})]
            f.sym(q, name, "class", node, scope, bases)
            for c in node.children:
                if c.type == "modifiers":
                    walk(c, q, q)
            body = node.child_by_field_name("body")
            for c in body.children if body else []:
                walk(c, q, q)
            return
        if t in {"method_declaration", "constructor_declaration"} and cls:
            name = _t(node.child_by_field_name("name"))
            q_body = f"{cls}.{name}"
            if not any(s["q"] == q_body for s in f.symbols):  # overloads share one node
                f.sym(q_body, name, "method", node, cls)
            for c in node.children:
                if c.type in {"formal_parameters", "type_identifier", "generic_type",
                              "scoped_type_identifier"}:
                    types(c, q_body)
                if c.type == "formal_parameters":
                    for prm in c.named_children:
                        walk(prm, q_body, None)
            body = node.child_by_field_name("body")
            if body is not None:
                walk(body, q_body, None)
            return
        if t == "method_invocation":
            obj = node.child_by_field_name("object")
            f.calls.append({"in": scope, "recv": _t(obj) if obj is not None else "",
                            "name": _t(node.child_by_field_name("name")), "line": _line(node)})
            for c in node.children:
                if c is not obj or (obj is not None and obj.type not in {"identifier", "this"}):
                    walk(c, scope, cls)
            return
        if t == "object_creation_expression":
            ty = node.child_by_field_name("type")
            f.calls.append({"in": scope, "recv": "", "name": _t(ty).split("<")[0].split(".")[-1],
                            "line": _line(node)})
        if t in {"line_comment", "block_comment"}:
            _comment_note(f, node, scope)
            if _t(node).startswith("/**"):
                nxt = node.next_named_sibling
                doc = _jsdoc(_t(node))
                if doc and nxt is not None:
                    f.notes.append({"text": doc, "line": _line(node), "in": ("@next", _line(nxt)),
                                    "doc": True})
            return
        if t in {"local_variable_declaration", "field_declaration", "formal_parameter"}:
            ty = node.child_by_field_name("type")
            owner = cls if t == "field_declaration" and cls else scope
            for d in [node] if t == "formal_parameter" else [
                    c for c in node.children if c.type == "variable_declarator"]:
                nm = d.child_by_field_name("name")
                if nm is not None and ty is not None:
                    f.var(owner, _t(nm), _t(ty))
        if t in {"local_variable_declaration", "field_declaration", "cast_expression",
                 "class_literal", "instanceof_expression"}:
            types(node, scope)
        for c in node.children:
            walk(c, scope, cls)

    def types(node, scope):
        for x in _descend(node, {"type_identifier"}):
            f.refs.append({"in": scope, "name": _t(x), "line": _line(x)})

    for c in root.children:
        walk(c, None, None)


# -- go ------------------------------------------------------------------------------------------
def _go(root, f: _Facts):
    def callee(fn):
        if fn is None:
            return None, None
        if fn.type == "identifier":
            return "", _t(fn)
        if fn.type == "selector_expression":
            return _t(fn.child_by_field_name("operand")), _t(fn.child_by_field_name("field"))
        return None, None

    def walk(node, scope):
        t = node.type
        if t == "package_clause":
            f.package = _t(next((c for c in node.named_children), None))
            return
        if t == "import_spec":
            path, name = node.child_by_field_name("path"), node.child_by_field_name("name")
            f.imports.append({"module": _string(path), "names": [],
                              "alias": _t(name) if name is not None else None,
                              "line": _line(node), "level": 0, "in_function": False,
                              "kind": "import"})
            return
        if t == "type_spec":
            ty = node.child_by_field_name("type")
            if ty is not None and ty.type in {"struct_type", "interface_type"}:
                name = _t(node.child_by_field_name("name"))
                bases = [_t(x) for x in _descend(ty, {"field_declaration"})
                         if x.child_by_field_name("name") is None]
                f.sym(name, name, "class", node, None, bases)
                types(ty, name)
                for fd in _descend(ty, {"field_declaration"}):
                    fty = fd.child_by_field_name("type")
                    for nm in fd.children:
                        if nm.type == "field_identifier" and fty is not None:
                            f.var(name, _t(nm), _t(fty))
                return
        if t == "function_declaration":
            name = _t(node.child_by_field_name("name"))
            f.sym(name, name, "function", node, None)
            body(node, name)
            return
        if t == "method_declaration":
            recv = node.child_by_field_name("receiver")
            rtype = next(iter(_descend(recv, {"type_identifier"})), None) if recv else None
            rvar = next(iter(_descend(recv, {"identifier"})), None) if recv else None
            cls = _t(rtype) if rtype is not None else None
            name = _t(node.child_by_field_name("name"))
            q = f"{cls}.{name}" if cls else name
            f.sym(q, name, "method" if cls else "function", node, cls,
                  recv_var=_t(rvar) if rvar is not None else None)
            body(node, q)
            return
        if t == "call_expression":
            fn = node.child_by_field_name("function")
            recv, name = callee(fn)
            if name:
                f.calls.append({"in": scope, "recv": recv, "name": name, "line": _line(node)})
            if fn is not None and fn.type == "selector_expression":
                walk(fn.child_by_field_name("operand"), scope)
            args = node.child_by_field_name("arguments")
            for c in args.children if args else []:
                if c.type == "identifier":
                    f.refs.append({"in": scope, "name": _t(c), "line": _line(c)})
                walk(c, scope)
            return
        if t in {"parameter_declaration", "var_spec"}:
            ty = node.child_by_field_name("type")
            for nm in node.children:
                if nm.type == "identifier" and ty is not None:
                    f.var(scope, _t(nm), _t(ty))
        if t == "short_var_declaration":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None and len(left.named_children) == 1:
                val = right.named_children[0] if right.named_children else None
                if val is not None and val.type == "unary_expression":
                    val = val.child_by_field_name("operand")
                if val is not None and val.type == "composite_literal":
                    f.var(scope, _t(left.named_children[0]), _t(val.child_by_field_name("type")))
        if t == "composite_literal":
            ty = node.child_by_field_name("type")
            if ty is not None:
                f.calls.append({"in": scope, "recv": "", "name": _t(ty).split(".")[-1].lstrip("*&"),
                                "line": _line(node)})
        if t == "comment":
            _comment_note(f, node, scope)
            return
        for c in node.children:
            walk(c, scope)

    def body(node, q):
        for field in ("parameters", "result"):
            c = node.child_by_field_name(field)
            if c is not None:
                types(c, q)
        for field in ("parameters", "receiver"):
            c = node.child_by_field_name(field)
            for prm in c.named_children if c is not None else []:
                walk(prm, q)
        b = node.child_by_field_name("body")
        if b is not None:
            walk(b, q)

    def types(node, scope):
        for x in _descend(node, {"type_identifier"}):
            f.refs.append({"in": scope, "name": _t(x), "line": _line(x)})

    for c in root.children:
        walk(c, None)
    # Doc comments directly above a func/type in Go are its description.
    for i, c in enumerate(root.children):
        if c.type == "comment" and i + 1 < len(root.children) and \
                root.children[i + 1].type in {"function_declaration", "method_declaration",
                                               "type_declaration"} and \
                _line(root.children[i + 1]) == _end(c) + 1:
            text = _t(c).lstrip("/ ").strip()
            if text and not NOTE_RX.search(text):
                f.notes.append({"text": text, "line": _line(c),
                                "in": ("@next", _line(root.children[i + 1])), "doc": True})


WALKERS = {"python": _py, "javascript": _js, "typescript": _js, "tsx": _js, "java": _java,
           "go": _go}


def extract(source: str | bytes, lang: str) -> dict:
    """Facts for one file. Parse errors are tolerated: tree-sitter recovers and we keep what
    parsed (error count is reported)."""
    data = source.encode() if isinstance(source, str) else source
    tree = parser(lang).parse(data)
    f = _Facts()
    WALKERS[lang](tree.root_node, f)
    out = f.to_dict()
    # Notes attached to "the next symbol" resolve to the symbol starting on that line.
    starts = {s["line"]: s["q"] for s in f.symbols}
    for n in out["notes"]:
        if isinstance(n["in"], tuple | list) and n["in"] and n["in"][0] == "@next":
            n["in"] = starts.get(n["in"][1])
        elif not n["doc"] and n["in"] is None:  # comment just above a def explains it
            n["in"] = starts.get(n["line"] + 1) or starts.get(n["line"] + 2)
    out["lang"] = lang
    out["errors"] = int(tree.root_node.has_error)
    return out
