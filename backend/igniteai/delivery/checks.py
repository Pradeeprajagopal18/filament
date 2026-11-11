"""Deterministic checks behind the delivery agents. No model: same input, same answer.

Agents call these through tools and add judgement on top (wording, priorities, questions);
offline runs and tests use them directly.

- triage_score: is an issue ready to build? (problem, acceptance criteria, scope, repro, size)
- handoff_brief: the task an external coding agent (Claude Code, Codex, Cursor) receives
- test_plan / classify_checks / retry_decision: QA
- review_findings: first-pass PR review scorecard
- release_plan: flagged, staged rollout with rollback criteria
- burn_rates / slo_status: SRE error-budget burn
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from igniteai.context import keywords

# -- triage --------------------------------------------------------------------------------------
AC_RX = re.compile(r"(acceptance criteria|^\s*[-*]\s*\[[ x]\]|\bgiven\b.*\bwhen\b.*\bthen\b|"
                   r"definition of done|expected (result|behaviou?r))", re.I | re.M)
REPRO_RX = re.compile(r"(steps to reproduce|repro|^\s*\d+\.\s)", re.I | re.M)
SCOPE_RX = re.compile(r"(out of scope|non-goals?|not in scope|scope:)", re.I)
BUG_RX = re.compile(r"\b(bug|error|crash|fails?|broken|500|exception|regression)\b", re.I)


def triage_score(title: str, body: str, labels: list[str] | None = None,
                 repos: list[str] | None = None, known_terms: set[str] | None = None,
                 others: list[tuple[str, str]] | None = None) -> dict:
    """Readiness 0-100 with the checks behind it. `others` = (id, title) of existing issues."""
    labels = [lb.lower() for lb in labels or []]
    text = f"{title}\n{body}"
    is_bug = "bug" in labels or bool(BUG_RX.search(title))
    words = len(body.split())
    terms = keywords(text)
    touches = sorted((known_terms or set()) & terms)
    repo_hit = [r for r in repos or [] if r.lower().split("/")[-1] in text.lower()]
    checks = [
        ("problem", "Explains the problem", words >= 25,
         f"{words} words" if words else "No description"),
        ("acceptance", "Has acceptance criteria", bool(AC_RX.search(body)),
         "Checklist or given/when/then found" if AC_RX.search(body) else
         "Add a checklist of what 'done' means"),
        ("scope", "Scope is bounded", bool(SCOPE_RX.search(body)) or words < 250,
         "Short and focused" if words < 250 else "Long: say what is out of scope"),
        ("location", "Points at code we know", bool(touches or repo_hit),
         ("Mentions " + ", ".join((repo_hit + touches)[:5])) if touches or repo_hit else
         "Name the service, endpoint or screen it affects"),
    ]
    if is_bug:
        checks.append(("repro", "Has steps to reproduce", bool(REPRO_RX.search(body)),
                       "Steps found" if REPRO_RX.search(body) else "Add steps to reproduce"))
    weights = {"problem": 25, "acceptance": 30, "scope": 10, "location": 20, "repro": 15}
    total = sum(weights[c[0]] for c in checks)
    got = sum(weights[c[0]] for c in checks if c[2])
    score = round(100 * got / total)
    dupes = []
    for oid, otitle in others or []:
        a, b = keywords(title), keywords(otitle)
        if a and b and len(a & b) / len(a | b) >= 0.6:
            dupes.append(oid)
    size = "S" if words < 80 else "M" if words < 250 else "L"
    status = "ready" if score >= 70 and not dupes else "needs_info" if score >= 40 else "not_ready"
    return {"score": score, "status": status, "kind": "bug" if is_bug else "feature", "size": size,
            "checks": [{"id": i, "label": lb, "ok": ok, "detail": d} for i, lb, ok, d in checks],
            "missing": [lb for _, lb, ok, _ in checks if not ok], "possible_duplicates": dupes,
            "touches": touches[:10]}


# -- coding handoff --------------------------------------------------------------------------------
def handoff_brief(title: str, goal: str, acceptance: list[str], repo: str | None,
                  symbols: list[dict], endpoints: list[str], consumers: list[str],
                  conventions: list[str], tests: list[str], task_id: str,
                  target: str = "claude_code", retry: dict | None = None,
                  branch: str | None = None) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    branch = branch or f"ignite/{task_id}-{slug}"
    lines = [f"# {title}", "", goal.strip(), ""]
    if retry:
        lines += [f"## Retry {retry['attempt']}: CI is failing", "",
                  *[f"- **{f['name']}** ({f['kind']}): {f['summary'] or 'failed'}"
                    for f in retry["failures"]], "", "Fix these, then push to the same branch.", ""]
    if acceptance:
        lines += ["## Acceptance criteria", "", *[f"- [ ] {a}" for a in acceptance], ""]
    if repo:
        lines += ["## Where", "", f"Repo: `{repo}`  ", f"Branch: `{branch}`", ""]
    if symbols:
        lines += ["## Code to start from", "",
                  *[f"- `{s['label']}` ({s.get('file')}:{s.get('line')})" for s in symbols[:12]],
                  ""]
    if endpoints:
        lines += ["## API surface this touches", "", *[f"- `{e}`" for e in endpoints], ""]
    if consumers:
        lines += ["Other repos call these endpoints: " + ", ".join(consumers) +
                  ". Keep the contract backward compatible.", ""]
    if conventions:
        lines += ["## Conventions", "", *[f"- {c}" for c in conventions[:10]], ""]
    lines += ["## Before opening the PR", "", *[f"- {t}" for t in tests or ["Run the test suite"]],
              (f"- Push the fix to `{branch}`; the open pull request picks it up." if retry else
               f"- Open the PR from `{branch}` and put `Ignite-Task: {task_id}` in the "
               "description so Ignite links it."), ""]
    brief = "\n".join(lines)
    prompt = (f"Implement Ignite task {task_id}. Use the Ignite MCP tools (code_explain, "
              f"code_impact) when you need context.\n\n{brief}")
    return {"branch": branch, "target": target, "brief": brief, "prompt": prompt,
            "commands": {"claude_code": f'claude "$(ign delivery handoff {task_id} --prompt)"',
                         "codex": f'codex "$(ign delivery handoff {task_id} --prompt)"',
                         "cursor": f"ign delivery handoff {task_id} --prompt | pbcopy  "
                                   "# then paste into Cursor"}}


def _word_stem(word: str) -> str:
    w = word.lower()
    for suffix in ("ing", "ed", "es", "s", "e"):
        if len(w) > 4 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


START_STOP = {"the", "and", "for", "with", "when", "into", "that", "this", "from", "our", "need",
              "needs", "should", "can", "will", "without", "call", "calling", "back", "make"}


def start_symbols(nodes: list[dict], title: str, body: str, limit: int = 6) -> list[dict]:
    """Where a coding agent should start: callables whose names share word stems with the
    issue (title words count double)."""
    def stems(text):
        words = re.findall(r"[a-z]{3,}", text.lower())
        return {_word_stem(w) for w in words if w not in START_STOP}
    weights = {s: 1.0 for s in stems(body)} | {s: 2.0 for s in stems(title)}
    scored = []
    for n in nodes:
        if n.get("kind") != "code" or not n.get("callable") or not n.get("file"):
            continue
        name = re.sub(r"\(\)$", "", n["label"]).split(".")[-1]
        toks = {_word_stem(x) for x in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", name) if len(x) >= 3}
        score = sum(weights.get(x, 0) for x in toks)
        if score >= 2:
            scored.append((-score, n["file"].count("test"), n["label"], n))
    return [x[3] for x in sorted(scored, key=lambda x: x[:3])[:limit]]


# -- QA --------------------------------------------------------------------------------------------
KIND_RX = [("lint", re.compile(r"lint|ruff|eslint|oxlint|flake8|prettier|format", re.I)),
           ("security", re.compile(r"codeql|snyk|security|trivy|dependabot|secret", re.I)),
           ("build", re.compile(r"build|compile|docker|tsc|typecheck|type-check", re.I)),
           ("test", re.compile(r"test|pytest|jest|vitest|junit|spec|e2e|ci", re.I))]


def classify_checks(runs: list[dict]) -> dict:
    failed, passed, pending = [], [], []
    for r in runs:
        kind = next((k for k, rx in KIND_RX if rx.search(r["name"])), "other")
        item = {"name": r["name"], "kind": kind, "summary": r.get("summary", ""),
                "url": r.get("url", "")}
        if r.get("status") != "completed":
            pending.append(item)
        elif r.get("conclusion") in {"success", "neutral", "skipped"}:
            passed.append(item)
        else:
            failed.append(item)
    state = "failing" if failed else "pending" if pending else "passing" if passed else "none"
    return {"state": state, "failed": failed, "passed": passed, "pending": pending}


def retry_decision(state: str, attempt: int, max_attempts: int) -> dict:
    if state == "passing":
        return {"action": "done", "reason": "All checks pass"}
    if state in {"pending", "none"}:
        return {"action": "wait", "reason": "Checks have not finished"}
    if attempt >= max_attempts:
        return {"action": "escalate",
                "reason": f"Still failing after {attempt} attempts; a person needs to look"}
    return {"action": "retry", "reason": f"Attempt {attempt + 1} of {max_attempts}"}


def test_plan(acceptance: list[str], endpoints: list[str], changed: list[dict],
              tested_files: set[str]) -> list[dict]:
    cases = [{"kind": "acceptance", "title": a, "covers": "story"} for a in acceptance]
    cases += [{"kind": "contract", "title": f"{e} keeps its documented request and responses",
               "covers": e} for e in endpoints]
    for s in changed:
        if s.get("label") == (s.get("file") or "").rsplit("/", 1)[-1]:
            continue  # the module itself, not a unit
        if s.get("file") and not any(_stem(s["file"]) in t for t in tested_files):
            cases.append({"kind": "unit", "title": f"Unit test for {s['label']}",
                          "covers": f"{s['file']}:{s.get('line')}"})
    return cases


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].split(".")[0]


# -- review ----------------------------------------------------------------------------------------
SECRET_RX = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC )?PRIVATE KEY|ghp_[A-Za-z0-9]{30,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[bp]-[A-Za-z0-9-]{10,}|"
    r"(password|secret|api_?key|token)\s*[:=]\s*['\"][^'\"${}]{8,}['\"])", re.I)
DEBUG_RX = re.compile(r"^\+\s*(console\.log\(|print\(|debugger;|fmt\.Println\(|System\.out\.print)")
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|Test)\.\w+$|"
                       r"test_\w+\.py$")
DEP_FILES = re.compile(r"(package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|pom\.xml|"
                       r"build\.gradle|Cargo\.toml)$")
MIGRATION = re.compile(r"(migrations?/|alembic/|\.sql$|db/migrate/)")


def review_findings(files: list[dict], impact: dict | None) -> dict:
    """Scorecard for a PR from its files (with patches) and the code-graph change impact."""
    findings: list[dict] = []

    def add(severity, title, detail, file=None, line=None):
        findings.append({"severity": severity, "title": title, "detail": detail, "file": file,
                         "line": line})

    src = [f for f in files if not TEST_FILE.search(f["filename"]) and not DEP_FILES.search(
        f["filename"]) and f["filename"].endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go",
                                                   ".java", ".kt", ".rb", ".cs"))]
    tests = [f for f in files if TEST_FILE.search(f["filename"])]
    changed_lines = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    for f in files:
        line_no = 0
        for raw in (f.get("patch") or "").splitlines():
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            if hunk:
                line_no = int(hunk.group(1)) - 1
                continue
            if raw.startswith("-"):
                continue
            line_no += 1
            if not raw.startswith("+"):
                continue
            if SECRET_RX.search(raw):
                add("blocker", "Possible secret committed",
                    "Remove it, rotate it, and load it from the environment instead.",
                    f["filename"], line_no)
            elif DEBUG_RX.search(raw) and not TEST_FILE.search(f["filename"]):
                add("low", "Debug output left in", raw[1:].strip()[:120], f["filename"], line_no)
            elif re.search(r"\b(TODO|FIXME|XXX)\b", raw):
                add("info", "New TODO", raw[1:].strip()[:120], f["filename"], line_no)
    if impact:
        eps, users = impact.get("endpoints", []), impact.get("consumers", [])
        if users:
            add("high", "Changes endpoints other repos call",
                "; ".join(f"{c['repo']}: {', '.join(c['endpoints'])}" for c in users) +
                ". Check the contract stays compatible and tell those owners.")
        elif eps:
            add("medium", "Changes API endpoints", ", ".join(eps))
        for h in impact.get("hotspots_touched", []):
            add("medium", "Touches a hotspot", f"{h} is called from many places; test callers.")
    if src and not tests:
        n = len(src)
        add("medium", "No test changes",
            f"{n} source file{'' if n == 1 else 's'} changed without a test change.")
    if any(MIGRATION.search(f["filename"]) for f in files):
        add("medium", "Includes a data migration",
            "Confirm it is backward compatible and has a rollback.")
    if any(DEP_FILES.search(f["filename"]) for f in files):
        add("info", "Dependency changes", ", ".join(f["filename"] for f in files
                                                    if DEP_FILES.search(f["filename"])))
    if changed_lines > 400:
        add("low", "Large change", f"{changed_lines} lines; consider splitting it.")
    order = ["blocker", "high", "medium", "low", "info"]
    findings.sort(key=lambda x: order.index(x["severity"]))
    counts = {s: sum(1 for x in findings if x["severity"] == s) for s in order}
    verdict = ("request_changes" if counts["blocker"] else
               "needs_attention" if counts["high"] or counts["medium"] >= 2 else "looks_good")
    return {"verdict": verdict, "risk": (impact or {}).get("risk", "unknown"), "counts": counts,
            "findings": findings, "files": len(files), "changed_lines": changed_lines,
            "tests_changed": len(tests)}


def review_markdown(card: dict, pr_title: str) -> str:
    label = {"request_changes": "Changes requested", "needs_attention": "Needs attention",
             "looks_good": "Looks good"}[card["verdict"]]
    lines = [f"## Ignite first-pass review: {label}", "",
             f"**{pr_title}** · risk {card['risk']} · {card['files']} "
             f"file{'' if card['files'] == 1 else 's'}, {card['changed_lines']} lines", ""]
    if not card["findings"]:
        lines.append("No findings. A person still approves the PR.")
    for f in card["findings"]:
        where = f" (`{f['file']}:{f['line']}`)" if f.get("file") and f.get("line") else (
            f" (`{f['file']}`)" if f.get("file") else "")
        lines.append(f"- **{f['severity']}** {f['title']}{where}: {f['detail']}")
    return "\n".join(lines)


# -- release ---------------------------------------------------------------------------------------
def release_plan(title: str, prs: list[dict], slos: list[dict], risk: str) -> dict:
    flag = "release_" + re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40]
    steps = [5, 25, 50, 100] if risk == "high" else [10, 50, 100] if risk == "medium" else [25, 100]
    consumers = sorted({c for p in prs for c in p.get("consumers", [])})
    return {
        "flag": flag, "risk": risk,
        "steps": [{"percent": p, "status": "pending", "hold_minutes": 30 if p < 100 else 0}
                  for p in steps],
        "rollback_when": [f"{s['title']}: error budget burning faster than 14x for 5 minutes"
                          for s in slos] or ["Error rate doubles versus the hour before rollout"],
        "notify": consumers, "prs": [p.get("url") or p.get("title") for p in prs],
        "stage": "planned", "auto_rollback": True,
    }


# -- SRE -------------------------------------------------------------------------------------------
WINDOWS = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1), "6h": timedelta(hours=6),
           "24h": timedelta(hours=24)}


def burn_rates(samples: list[dict], objective: float, at: datetime | None = None) -> dict:
    """Error-budget burn per window. samples = [{at, good, total}]; objective like 99.9."""
    at = at or datetime.now(UTC)
    budget = max(1e-9, 1 - objective / 100)
    out = {}
    for name, span in WINDOWS.items():
        good = total = 0
        for s in samples:
            ts = s["at"] if isinstance(s["at"], datetime) else datetime.fromisoformat(s["at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if at - span <= ts <= at:
                good += s["good"]
                total += s["total"]
        err = (1 - good / total) if total else 0.0
        out[name] = round(err / budget, 2) if total else None
    return out


def slo_status(rates: dict) -> dict:
    """Multi-window alerting (Google SRE workbook): fast burn pages, slow burn tickets."""
    fast = (rates.get("5m") or 0) >= 14.4 and (rates.get("1h") or 0) >= 14.4
    slow = (rates.get("1h") or 0) >= 6 and (rates.get("6h") or 0) >= 6
    if fast:
        return {"state": "breach", "severity": "page",
                "reason": "Burning the error budget over 14x too fast (5m and 1h)"}
    if slow:
        return {"state": "warning", "severity": "ticket",
                "reason": "Burning the error budget over 6x too fast (1h and 6h)"}
    return {"state": "ok", "severity": None, "reason": "Within budget"}
