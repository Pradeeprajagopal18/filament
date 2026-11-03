"""Data model: tables, columns, keys and relationships from SQL DDL and ORM models.

Sources: CREATE TABLE / ALTER TABLE ... FOREIGN KEY in .sql files and migrations, SQLAlchemy
(declarative Column / mapped_column, ForeignKey, __tablename__), Django models (ForeignKey,
OneToOneField, ManyToManyField) and Prisma schemas. Each entity keeps its file and line.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filament.readiness.scanner import Repo

SKIP = {"node_modules", "dist", "build", "vendor", ".venv", "venv", "tests", "test", "fixtures"}

CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(?:\w+[`\"\]]?\.)?[`\"\[]?(\w+)[`\"\]]?\s*\(",
                    re.I)
ALTER_FK = re.compile(r"ALTER\s+TABLE\s+(?:ONLY\s+)?[`\"]?(?:\w+\.)?[`\"]?(\w+)[`\"]?\s+ADD\s+(?:CONSTRAINT\s+\w+\s+)?"
                      r"FOREIGN\s+KEY\s*\(\s*[`\"]?(\w+)[`\"]?\s*\)\s*REFERENCES\s+[`\"]?(?:\w+\.)?[`\"]?(\w+)", re.I)
SQL_SKIP = {"primary", "foreign", "constraint", "unique", "key", "index", "check", "exclude"}


def _sql_body(text: str, start: int) -> str:
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def _split_cols(body: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in body:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [c.strip() for c in out if c.strip()]


def _from_sql(f: str, text: str, entities: dict, rels: list) -> None:
    for m in CREATE.finditer(text):
        name = m.group(1).lower()
        body = _sql_body(text, m.end())
        fields, pks = [], set()
        for col in _split_cols(body):
            words = col.split()
            head = words[0].strip('`"[]').lower()
            if head in SQL_SKIP:
                pk = re.search(r"PRIMARY\s+KEY\s*\(([^)]*)\)", col, re.I)
                if pk:
                    pks |= {c.strip(' `"').lower() for c in pk.group(1).split(",")}
                fk = re.search(r"FOREIGN\s+KEY\s*\(\s*[`\"]?(\w+)[`\"]?\s*\)\s*REFERENCES\s+[`\"]?(?:\w+\.)?[`\"]?(\w+)",
                               col, re.I)
                if fk:
                    rels.append({"from": name, "to": fk.group(2).lower(), "field": fk.group(1).lower(),
                                 "kind": "many-to-one", "file": f})
                continue
            if len(words) < 2:
                continue
            ctype = words[1].strip(",").lower()
            ref = re.search(r"REFERENCES\s+[`\"]?(?:\w+\.)?[`\"]?(\w+)", col, re.I)
            field = {"name": head, "type": ctype, "pk": bool(re.search(r"PRIMARY\s+KEY", col, re.I)),
                     "nullable": not re.search(r"NOT\s+NULL|PRIMARY\s+KEY", col, re.I),
                     "fk": ref.group(1).lower() if ref else None}
            fields.append(field)
            if ref:
                rels.append({"from": name, "to": ref.group(1).lower(), "field": head,
                             "kind": "many-to-one", "file": f})
        for fl in fields:
            fl["pk"] = fl["pk"] or fl["name"] in pks
        entities.setdefault(name, {"name": name, "source": "sql", "file": f,
                                   "line": text.count("\n", 0, m.start()) + 1, "fields": fields})
    for m in ALTER_FK.finditer(text):
        rels.append({"from": m.group(1).lower(), "to": m.group(3).lower(), "field": m.group(2).lower(),
                     "kind": "many-to-one", "file": f})


PY_CLASS = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", re.M)
SA_COL = re.compile(r"^[ \t]{4}(\w+)[ \t]*(?::[ \t]*Mapped\[([^\n]*?)\])?[ \t]*=[ \t]*(?:sa\.|db\.)?"
                    r"(Column|mapped_column|relationship)\(([^\n]*)$", re.M)
DJ_FIELD = re.compile(r"^\s{4}(\w+)\s*=\s*models\.(\w+)\((.*)$", re.M)


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _from_python(f: str, text: str, entities: dict, rels: list, classes: dict) -> None:
    for m in PY_CLASS.finditer(text):
        end = re.search(r"^\S", text[m.end():], re.M)
        body = text[m.end(): m.end() + end.start()] if end else text[m.end():]
        bases = m.group(2)
        line = text.count("\n", 0, m.start()) + 1
        tbl = re.search(r"__tablename__\s*=\s*['\"](\w+)['\"]", body)
        if tbl or re.search(r"\b(Base|DeclarativeBase|db\.Model|SQLModel)\b", bases) and (
                "Column(" in body or "mapped_column(" in body):
            name = (tbl.group(1) if tbl else _camel_to_snake(m.group(1))).lower()
            classes[m.group(1)] = name
            fields = []
            for c in SA_COL.finditer(body):
                if c.group(3) == "relationship":
                    continue
                args = c.group(4)
                fk = re.search(r"ForeignKey\(\s*['\"](\w+)\.(\w+)['\"]", args)
                ctype = re.search(r"\b(Integer|String|Text|Boolean|DateTime|Float|Numeric|JSON|UUID|"
                                  r"BigInteger|Date|Enum|LargeBinary)\b", args)
                mapped = (c.group(2) or "").strip()
                fields.append({"name": c.group(1),
                               "type": (ctype.group(1) if ctype else mapped or "?").lower(),
                               "pk": "primary_key=True" in args,
                               "nullable": "nullable=True" in args or "None" in mapped or "Optional" in mapped,
                               "fk": fk.group(1).lower() if fk else None})
                if fk:
                    rels.append({"from": name, "to": fk.group(1).lower(), "field": c.group(1),
                                 "kind": "many-to-one", "file": f})
            entities.setdefault(name, {"name": name, "source": "sqlalchemy", "file": f, "line": line,
                                       "model": m.group(1), "fields": fields})
        elif re.search(r"\bmodels\.Model\b", bases):
            name = _camel_to_snake(m.group(1))
            classes[m.group(1)] = name
            fields = [{"name": "id", "type": "autofield", "pk": True, "nullable": False, "fk": None}]
            for d in DJ_FIELD.finditer(body):
                kind, args = d.group(2), d.group(3)
                target = re.match(r"\s*['\"]?([\w.]+)['\"]?", args)
                tname = target.group(1).split(".")[-1] if target else ""
                is_rel = kind in {"ForeignKey", "OneToOneField", "ManyToManyField"}
                fields.append({"name": d.group(1) + ("_id" if kind in {"ForeignKey", "OneToOneField"} else ""),
                               "type": kind.replace("Field", "").lower(), "pk": "primary_key=True" in args,
                               "nullable": "null=True" in args, "fk": tname if is_rel else None})
                if is_rel and tname:
                    rels.append({"from": name, "to": tname, "field": d.group(1),
                                 "kind": {"ForeignKey": "many-to-one", "OneToOneField": "one-to-one",
                                          "ManyToManyField": "many-to-many"}[kind], "file": f})
            entities.setdefault(name, {"name": name, "source": "django", "file": f, "line": line,
                                       "model": m.group(1), "fields": fields})


PRISMA_MODEL = re.compile(r"^model\s+(\w+)\s*\{(.*?)^\}", re.M | re.S)


def _from_prisma(f: str, text: str, entities: dict, rels: list) -> None:
    for m in PRISMA_MODEL.finditer(text):
        name = _camel_to_snake(m.group(1))
        fields = []
        for line in m.group(2).splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].startswith(("//", "@@")):
                continue
            ftype = parts[1]
            rel = re.search(r"@relation\([^)]*fields:\s*\[(\w+)\]", line)
            base = ftype.rstrip("?[]")
            if base[:1].isupper() and base not in {"String", "Int", "Boolean", "DateTime", "Float",
                                                   "Decimal", "Json", "BigInt", "Bytes"}:
                if rel:
                    rels.append({"from": name, "to": _camel_to_snake(base), "field": rel.group(1),
                                 "kind": "many-to-one", "file": f})
                continue
            fields.append({"name": parts[0], "type": base.lower(), "pk": "@id" in line,
                           "nullable": ftype.endswith("?"), "fk": None})
        entities.setdefault(name, {"name": name, "source": "prisma", "file": f,
                                   "line": text.count("\n", 0, m.start()) + 1, "fields": fields})


def extract(repo: Repo) -> dict:
    entities: dict[str, dict] = {}
    rels: list[dict] = []
    classes: dict[str, str] = {}
    for p in repo.files:
        if set(p.parts[:-1]) & SKIP:
            continue
        f = str(p)
        if p.suffix == ".sql":
            _from_sql(f, repo.read(f, 500_000), entities, rels)
        elif p.suffix == ".py":
            text = repo.read(f, 300_000)
            if "Column(" in text or "mapped_column(" in text or "models.Model" in text:
                _from_python(f, text, entities, rels, classes)
        elif p.suffix == ".prisma":
            _from_prisma(f, repo.read(f, 300_000), entities, rels)
    lowered = {k.lower(): v for k, v in classes.items()}
    for r in rels:  # Django/SQLAlchemy references by class name -> table name
        r["to"] = classes.get(r["to"], lowered.get(r["to"].lower(), r["to"])).lower()
        r["to"] = _camel_to_snake(r["to"]) if r["to"] not in entities else r["to"]
    for e in entities.values():
        for fl in e["fields"]:
            if fl.get("fk"):
                fl["fk"] = classes.get(fl["fk"], fl["fk"]).lower()
    for r in rels:  # mark FK columns declared through table constraints
        ent = entities.get(r["from"])
        for fl in (ent or {}).get("fields", []):
            if fl["name"] == r["field"] and not fl.get("fk"):
                fl["fk"] = r["to"]
    seen, uniq = set(), []
    for r in rels:
        key = (r["from"], r["to"], r["field"])
        if key not in seen and r["from"] in entities:
            seen.add(key)
            uniq.append(r)
    ents = sorted(entities.values(), key=lambda e: e["name"])
    return {"entities": ents[:200], "relations": uniq[:400],
            "mermaid": mermaid(ents[:60], uniq) if ents else ""}


def mermaid(entities: list[dict], rels: list[dict]) -> str:
    def ident(s: str) -> str:
        return re.sub(r"\W", "_", s) or "x"

    def ty(s: str) -> str:
        base = re.split(r"[(\s\[]", (s or "x").strip())[0]  # numeric(10,2) -> numeric
        return re.sub(r"[^\w]", "_", base)[:24] or "x"

    lines = ["erDiagram"]
    names = {e["name"] for e in entities}
    for e in entities:
        lines.append(f"  {ident(e['name'])} {{")
        for fl in e["fields"][:14]:
            key = " PK" if fl.get("pk") else (" FK" if fl.get("fk") else "")
            lines.append(f"    {ty(fl['type'])} {ident(fl['name'])}{key}")
        lines.append("  }")
    card = {"many-to-one": "}o--||", "one-to-one": "||--||", "many-to-many": "}o--o{"}
    for r in rels:
        if r["from"] in names and r["to"] in names:
            lines.append(f"  {ident(r['from'])} {card.get(r['kind'], '}o--||')} {ident(r['to'])} : "
                         f"{ident(r['field'])}")
    return "\n".join(lines)
