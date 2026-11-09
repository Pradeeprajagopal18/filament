"""Minimal GitHub REST client for the delivery stages: issues, pull requests, CI checks, comments.

Uses a token from the environment (FILAMENT_GIT_TOKEN or GITHUB_TOKEN), never stored. Every
call is small and read-only except `comment`, which agents may only use at autonomy L3+.
"""

from __future__ import annotations

import os
import re

API = "https://api.github.com"
PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
REPO_REF = re.compile(r"(?:github\.com[/:])?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


class GitHubError(RuntimeError):
    pass


def token() -> str | None:
    return os.environ.get("FILAMENT_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "filament"}
    if token():
        h["Authorization"] = f"Bearer {token()}"
    return h


def _get(path: str, params: dict | None = None):
    import httpx

    try:
        r = httpx.get(f"{API}{path}", headers=_headers(), params=params or {}, timeout=20)
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub unreachable: {exc}") from exc
    if r.status_code == 404:
        raise GitHubError(f"Not found on GitHub: {path} (private repos need FILAMENT_GIT_TOKEN)")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub {r.status_code}: {r.text[:200]}")
    return r.json()


def parse_pr(url: str) -> tuple[str, str, int]:
    m = PR_URL.search(url or "")
    if not m:
        raise GitHubError(f"Not a GitHub pull request URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def parse_repo(ref: str) -> tuple[str, str]:
    m = REPO_REF.search((ref or "").strip())
    if not m:
        raise GitHubError(f"Not a GitHub repo: {ref}")
    return m.group(1), m.group(2)


def pull_request(url: str) -> dict:
    """Title, body, branch, head commit and changed files (with patches) of a PR."""
    owner, repo, n = parse_pr(url)
    pr = _get(f"/repos/{owner}/{repo}/pulls/{n}")
    files = _get(f"/repos/{owner}/{repo}/pulls/{n}/files", {"per_page": 100})
    return {"url": url, "repo": f"{owner}/{repo}", "number": n, "title": pr.get("title", ""),
            "body": pr.get("body") or "", "branch": (pr.get("head") or {}).get("ref", ""),
            "head_sha": (pr.get("head") or {}).get("sha", ""), "state": pr.get("state", ""),
            "merged": bool(pr.get("merged")), "author": (pr.get("user") or {}).get("login", ""),
            "files": [{"filename": f["filename"], "status": f.get("status", "modified"),
                       "additions": f.get("additions", 0), "deletions": f.get("deletions", 0),
                       "patch": f.get("patch", "")} for f in files]}


def check_runs(url: str, sha: str | None = None) -> list[dict]:
    """CI check runs for a PR's head commit."""
    owner, repo, n = parse_pr(url)
    if not sha:
        sha = (_get(f"/repos/{owner}/{repo}/pulls/{n}").get("head") or {}).get("sha", "")
    data = _get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs", {"per_page": 100})
    return [{"name": c.get("name", ""), "status": c.get("status", ""),
             "conclusion": c.get("conclusion"),
             "summary": ((c.get("output") or {}).get("summary") or
                         (c.get("output") or {}).get("title") or "")[:600],
             "url": c.get("html_url", "")} for c in data.get("check_runs", [])]


def issues(ref: str, state: str = "open", labels: str = "", limit: int = 20) -> list[dict]:
    owner, repo = parse_repo(ref)
    params = {"state": state, "per_page": min(limit, 100)}
    if labels:
        params["labels"] = labels
    data = _get(f"/repos/{owner}/{repo}/issues", params)
    return [{"number": i["number"], "title": i.get("title", ""), "body": i.get("body") or "",
             "url": i.get("html_url", ""), "labels": [lb["name"] for lb in i.get("labels", [])],
             "author": (i.get("user") or {}).get("login", ""), "repo": f"{owner}/{repo}"}
            for i in data if "pull_request" not in i][:limit]


def comment(url: str, body: str) -> dict:
    import httpx

    if not token():
        raise GitHubError("Posting to GitHub needs FILAMENT_GIT_TOKEN in the environment")
    owner, repo, n = parse_pr(url)
    r = httpx.post(f"{API}/repos/{owner}/{repo}/issues/{n}/comments", headers=_headers(),
                   json={"body": body}, timeout=20)
    if r.status_code >= 400:
        raise GitHubError(f"GitHub {r.status_code}: {r.text[:200]}")
    return {"url": r.json().get("html_url", "")}
