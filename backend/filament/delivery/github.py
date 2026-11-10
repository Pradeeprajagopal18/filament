"""Minimal GitHub REST client for the delivery stages: issues, pull requests, CI checks, comments.

Works with github.com and GitHub Enterprise Server (hosts in FILAMENT_GITHUB_HOSTS; the API is
https://<host>/api/v3). Uses a token from the environment (FILAMENT_GIT_TOKEN or GITHUB_TOKEN),
never stored, and only sends it to configured hosts. Every call is small and read-only except
`comment`, which agents may only use at autonomy L3+.
"""

from __future__ import annotations

import re

from filament import gitaccess

PR_URL = re.compile(r"https?://([\w.-]+)/([\w.-]+)/([\w.-]+)/pull/(\d+)")
REPO_REF = re.compile(r"(?:(?:https?://)?([\w.-]+\.[\w.-]+)[/:])?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


class GitHubError(RuntimeError):
    pass


def token() -> str | None:
    return gitaccess.token()


def _check_host(host: str) -> str:
    """Only configured hosts get the token (and our API calls)."""
    if host not in gitaccess.hosts():
        raise GitHubError(f"{host} is not a configured GitHub host. Add it to "
                          f"FILAMENT_GITHUB_HOSTS, e.g. '[\"github.com\",\"{host}\"]'")
    return host


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "filament"}
    if token():
        h["Authorization"] = f"Bearer {token()}"
    return h


def _get(host: str, path: str, params: dict | None = None):
    import httpx

    try:
        r = httpx.get(f"{gitaccess.api_base(host)}{path}", headers=_headers(),
                      params=params or {}, timeout=20)
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub unreachable: {exc}") from exc
    if r.status_code == 404:
        raise GitHubError(f"Not found on GitHub: {path} (private repos need FILAMENT_GIT_TOKEN)")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub {r.status_code}: {r.text[:200]}")
    return r.json()


def parse_pr(url: str) -> tuple[str, str, str, int]:
    """(host, owner, repo, number) from a pull request URL on a configured host."""
    m = PR_URL.search(url or "")
    if not m:
        raise GitHubError(f"Not a GitHub pull request URL: {url}")
    return _check_host(m.group(1).lower()), m.group(2), m.group(3), int(m.group(4))


def parse_repo(ref: str) -> tuple[str, str, str]:
    """(host, owner, repo) from owner/repo, host/owner/repo or a URL (default host: the first)."""
    ref = (ref or "").strip()
    if ref.startswith("git@"):
        ref = ref[4:].replace(":", "/", 1)
    m = REPO_REF.search(ref)
    if not m:
        raise GitHubError(f"Not a GitHub repo: {ref}")
    return _check_host((m.group(1) or gitaccess.hosts()[0]).lower()), m.group(2), m.group(3)


def pull_request(url: str) -> dict:
    """Title, body, branch, head commit and changed files (with patches) of a PR."""
    host, owner, repo, n = parse_pr(url)
    pr = _get(host, f"/repos/{owner}/{repo}/pulls/{n}")
    files = _get(host, f"/repos/{owner}/{repo}/pulls/{n}/files", {"per_page": 100})
    return {"url": url, "repo": f"{owner}/{repo}", "number": n, "title": pr.get("title", ""),
            "body": pr.get("body") or "", "branch": (pr.get("head") or {}).get("ref", ""),
            "head_sha": (pr.get("head") or {}).get("sha", ""), "state": pr.get("state", ""),
            "merged": bool(pr.get("merged")), "author": (pr.get("user") or {}).get("login", ""),
            "files": [{"filename": f["filename"], "status": f.get("status", "modified"),
                       "additions": f.get("additions", 0), "deletions": f.get("deletions", 0),
                       "patch": f.get("patch", "")} for f in files]}


def check_runs(url: str, sha: str | None = None) -> list[dict]:
    """CI check runs for a PR's head commit."""
    host, owner, repo, n = parse_pr(url)
    if not sha:
        sha = (_get(host, f"/repos/{owner}/{repo}/pulls/{n}").get("head") or {}).get("sha", "")
    data = _get(host, f"/repos/{owner}/{repo}/commits/{sha}/check-runs", {"per_page": 100})
    return [{"name": c.get("name", ""), "status": c.get("status", ""),
             "conclusion": c.get("conclusion"),
             "summary": ((c.get("output") or {}).get("summary") or
                         (c.get("output") or {}).get("title") or "")[:600],
             "url": c.get("html_url", "")} for c in data.get("check_runs", [])]


def issues(ref: str, state: str = "open", labels: str = "", limit: int = 20) -> list[dict]:
    host, owner, repo = parse_repo(ref)
    params = {"state": state, "per_page": min(limit, 100)}
    if labels:
        params["labels"] = labels
    data = _get(host, f"/repos/{owner}/{repo}/issues", params)
    return [{"number": i["number"], "title": i.get("title", ""), "body": i.get("body") or "",
             "url": i.get("html_url", ""), "labels": [lb["name"] for lb in i.get("labels", [])],
             "author": (i.get("user") or {}).get("login", ""), "repo": f"{owner}/{repo}"}
            for i in data if "pull_request" not in i][:limit]


def comment(url: str, body: str) -> dict:
    import httpx

    if not token():
        raise GitHubError("Posting to GitHub needs FILAMENT_GIT_TOKEN in the environment")
    host, owner, repo, n = parse_pr(url)
    r = httpx.post(f"{gitaccess.api_base(host)}/repos/{owner}/{repo}/issues/{n}/comments",
                   headers=_headers(),
                   json={"body": body}, timeout=20)
    if r.status_code >= 400:
        raise GitHubError(f"GitHub {r.status_code}: {r.text[:200]}")
    return {"url": r.json().get("html_url", "")}
