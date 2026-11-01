"""What changed between two analyses of the same repo, and which changes break consumers."""

from __future__ import annotations

import re


def _ekey(ep: dict) -> str:
    return f"{ep['method']} {re.sub(r'{[^}]+}', '{}', ep['path'])}"


def _required_query(ep: dict) -> set[str]:
    return {p["name"] for p in ep.get("params") or [] if p.get("in") == "query" and p.get("required")}


def _schema_props(schema: dict | None, schemas: dict) -> tuple[set[str], set[str]]:
    """(properties, required) of an object schema, following one $ref."""
    if not schema:
        return set(), set()
    if "$ref" in schema:
        schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1]) or {}
    if schema.get("type") == "array":
        return _schema_props(schema.get("items"), schemas)
    return set((schema.get("properties") or {}).keys()), set(schema.get("required") or [])


def _ok_schema(ep: dict) -> dict | None:
    for code, r in sorted((ep.get("responses") or {}).items()):
        if code.startswith("2"):
            return r.get("schema")
    return None


def compare(old: dict | None, new: dict) -> dict:
    if not old:
        return {"first_scan": True, "summary": "First scan", "breaking": [], "counts": {}}
    out: dict = {"breaking": []}
    o_api, n_api = old.get("api") or {}, new.get("api") or {}
    o_eps = {_ekey(e): e for e in o_api.get("endpoints") or []}
    n_eps = {_ekey(e): e for e in n_api.get("endpoints") or []}
    o_sch, n_sch = o_api.get("schemas") or {}, n_api.get("schemas") or {}
    out["endpoints_added"] = [f"{n_eps[k]['method']} {n_eps[k]['path']}"
                              for k in sorted(set(n_eps) - set(o_eps))]
    out["endpoints_removed"] = [f"{o_eps[k]['method']} {o_eps[k]['path']}"
                                for k in sorted(set(o_eps) - set(n_eps))]
    for ep in out["endpoints_removed"]:
        out["breaking"].append({"endpoint": ep, "change": "endpoint removed"})
    changed = []
    for k in sorted(set(o_eps) & set(n_eps)):
        a, b = o_eps[k], n_eps[k]
        label = f"{b['method']} {b['path']}"
        reasons = []
        new_req = _required_query(b) - _required_query(a)
        if new_req:
            reasons.append(("new required query parameter " + ", ".join(sorted(new_req)), True))
        a_props, a_req = _schema_props((a.get("request") or {}).get("schema"), o_sch)
        b_props, b_req = _schema_props((b.get("request") or {}).get("schema"), n_sch)
        if b_req - a_req:
            reasons.append(("new required request field " + ", ".join(sorted(b_req - a_req)),
                            True))
        ra, _ = _schema_props(_ok_schema(a), o_sch)
        rb, _ = _schema_props(_ok_schema(b), n_sch)
        if ra - rb:
            reasons.append(("response field removed " + ", ".join(sorted(ra - rb)), True))
        if rb - ra:
            reasons.append(("response field added " + ", ".join(sorted(rb - ra)), False))
        if b_props - a_props and not (b_req - a_req):
            reasons.append(("optional request field added " + ", ".join(sorted(b_props - a_props)),
                            False))
        if b.get("auth") and not a.get("auth"):
            reasons.append(("now requires authentication", True))
        for p_old in a.get("params") or []:
            p_new = next((p for p in b.get("params") or [] if p["name"] == p_old["name"]), None)
            if p_new and (p_old.get("schema") or {}).get("type") and \
                    (p_new.get("schema") or {}).get("type") and \
                    p_old["schema"]["type"] != p_new["schema"]["type"]:
                reasons.append((f"parameter {p_old['name']} type "
                                f"{p_old['schema']['type']} → {p_new['schema']['type']}", True))
        if reasons:
            changed.append({"endpoint": label, "changes": [r for r, _ in reasons]})
            out["breaking"] += [{"endpoint": label, "change": r} for r, br in reasons if br]
    out["endpoints_changed"] = changed

    o_checks = {c["id"]: c["status"] for c in (old.get("readiness") or {}).get("checks", [])}
    n_checks = {c["id"]: (c["status"], c["title"])
                for c in (new.get("readiness") or {}).get("checks", [])}
    out["checks_fixed"] = [t for i, (s, t) in n_checks.items()
                           if s == "pass" and o_checks.get(i) == "fail"]
    out["checks_regressed"] = [t for i, (s, t) in n_checks.items()
                               if s == "fail" and o_checks.get(i) == "pass"]
    ok = set(old.get("profile", {}).get("kind_names") or [])
    nk = set(new.get("profile", {}).get("kind_names") or [])
    out["kinds_added"], out["kinds_removed"] = sorted(nk - ok), sorted(ok - nk)
    on = {n["id"]: n["label"] for n in (old.get("components") or {}).get("nodes", [])}
    nn = {n["id"]: n["label"] for n in (new.get("components") or {}).get("nodes", [])}
    out["connections_added"] = [nn[i] for i in sorted(set(nn) - set(on))]
    out["connections_removed"] = [on[i] for i in sorted(set(on) - set(nn))]
    op = {p["path"] for p in (old.get("ui") or {}).get("pages", [])}
    np_ = {p["path"] for p in (new.get("ui") or {}).get("pages", [])}
    out["pages_added"], out["pages_removed"] = sorted(np_ - op), sorted(op - np_)
    drift = n_api.get("drift") or {}
    out["contract_drift"] = (len(drift.get("in_code_not_in_contract") or []) +
                             len(drift.get("in_contract_not_in_code") or []))
    o_score = (old.get("readiness") or {}).get("score", 0)
    n_score = (new.get("readiness") or {}).get("score", 0)
    out["score_delta"] = round(n_score - o_score, 1)

    counts = {k: len(v) for k, v in out.items() if isinstance(v, list) and k != "breaking"}
    out["counts"] = counts
    parts = []
    if out["breaking"]:
        parts.append(f"{len(out['breaking'])} breaking API change"
                     f"{'s' * (len(out['breaking']) > 1)}")
    for key, one, many in (("endpoints_added", "endpoint added", "endpoints added"),
                           ("endpoints_removed", "endpoint removed", "endpoints removed"),
                           ("endpoints_changed", "endpoint changed", "endpoints changed"),
                           ("pages_added", "page added", "pages added"),
                           ("connections_added", "new connection", "new connections"),
                           ("checks_fixed", "check fixed", "checks fixed"),
                           ("checks_regressed", "check regressed", "checks regressed")):
        if counts.get(key):
            parts.append(f"{counts[key]} {one if counts[key] == 1 else many}")
    out["summary"] = ", ".join(parts) if parts else "No changes"
    out["changed"] = bool(parts)
    return out
