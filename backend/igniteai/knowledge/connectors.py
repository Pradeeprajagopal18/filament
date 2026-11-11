"""Where documents come from. Each connector lists what's there (id, title, url, version) and
reads a document's sections when it is new or changed, so a sync only re-reads what moved.

* confluence: Confluence Cloud (https://acme.atlassian.net/wiki, email + API token) or Data
  Center / Server (https://confluence.acme.com, a personal access token). One or more spaces,
  optionally narrowed with CQL (labels, ancestors...).
* web: a docs site or wiki. Start URLs and/or a sitemap; follows links on the same site under
  the start path, up to max_pages.
* repo: Markdown and other docs inside onboarded repos (README, docs/, ADRs).
* upload: files an admin uploads (PDF, Word, Markdown, HTML, text); nothing to sync.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

from igniteai.knowledge.text import Section, html_sections, markdown_sections

SECRET_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


class ConnectorError(RuntimeError):
    pass


@dataclass
class Item:
    external_id: str
    title: str
    url: str | None
    version: str
    meta: dict = field(default_factory=dict)


def _secret(ref: str | None, what: str) -> str | None:
    if not ref:
        return None
    m = SECRET_REF.match(ref)
    if not m:
        raise ConnectorError(f"The {what} must be a reference such as ${{env:NAME}}")
    val = os.environ.get(m.group(1))
    if not val:
        raise ConnectorError(f"{m.group(1)} isn't set on the server (the {what})")
    return val


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# -- Confluence -----------------------------------------------------------------------------------
class Confluence:
    def __init__(self, config: dict):
        base = (config.get("base_url") or "").rstrip("/")
        if not base.startswith("https://"):
            raise ConnectorError("Confluence needs its https:// address")
        self.cloud = config.get("deployment") == "cloud" or "atlassian.net" in base
        if self.cloud and not base.endswith("/wiki"):
            base += "/wiki"
        self.base = base
        self.spaces = [s.strip() for s in config.get("spaces", []) if s.strip()]
        self.cql = (config.get("cql") or "").strip()
        if not self.spaces and not self.cql:
            raise ConnectorError("Choose at least one space key (or a CQL query)")
        token = _secret(config.get("token_ref"), "Confluence token")
        if not token:
            raise ConnectorError("Confluence needs an API token (as ${env:NAME})")
        if self.cloud:
            email = config.get("email")
            if not email:
                raise ConnectorError("Confluence Cloud needs the email the token belongs to")
            self.auth = (email, token)
            self.headers = {}
        else:
            self.auth = None
            self.headers = {"Authorization": f"Bearer {token}"}
        self.max_pages = int(config.get("max_pages") or 5000)

    def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx

        url = path if path.startswith("http") else self.base + path
        try:
            r = httpx.get(url, params=params, auth=self.auth, timeout=60,
                          headers={"Accept": "application/json", **self.headers})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Can't reach Confluence at {self.base}: {exc}") from exc
        if r.status_code in (401, 403):
            raise ConnectorError("Confluence refused the token (check it, and that its account "
                                 "can read the spaces)")
        if r.status_code == 404:
            raise ConnectorError(f"Not found on Confluence: {url}")
        if r.status_code >= 400:
            raise ConnectorError(f"Confluence answered {r.status_code}: {r.text[:200]}")
        return r.json()

    def query(self) -> str:
        parts = ["type=page"]
        if self.spaces:
            parts.append("space in (" + ",".join(f'"{s}"' for s in self.spaces) + ")")
        if self.cql:
            parts.append(f"({self.cql})")
        return " and ".join(parts)

    def list(self) -> Iterator[Item]:
        start, seen = 0, 0
        path, params = "/rest/api/content/search", {"cql": self.query(), "limit": 100,
                                                    "expand": "version,space"}
        while seen < self.max_pages:
            data = self._get(path, {**params, "start": start} if params else None)
            results = data.get("results", [])
            for page in results:
                seen += 1
                yield Item(external_id=str(page["id"]), title=page.get("title", ""),
                           url=self.base + page.get("_links", {}).get("webui", ""),
                           version=str(page.get("version", {}).get("number", "")),
                           meta={"space": page.get("space", {}).get("key")})
            nxt = data.get("_links", {}).get("next")
            if not results or not nxt:
                break
            if nxt.startswith("/rest/"):  # relative to the base
                path, params = nxt, None
            else:
                path, params = self.base + nxt.removeprefix("/wiki"), None
            start += len(results)

    def read(self, item: Item) -> tuple[str, list[Section]]:
        page = self._get(f"/rest/api/content/{item.external_id}",
                         {"expand": "body.storage,ancestors"})
        crumbs = [a.get("title", "") for a in page.get("ancestors", [])][-2:]
        _, sections, _ = html_sections(page.get("body", {}).get("storage", {}).get("value", ""))
        title = page.get("title", item.title)
        if crumbs:
            title = " > ".join([*crumbs, title])
        return title, sections


# -- websites -------------------------------------------------------------------------------------
class Website:
    def __init__(self, config: dict):
        self.urls = [u.strip() for u in config.get("urls", []) if u.strip()]
        self.sitemap = (config.get("sitemap") or "").strip()
        if not self.urls and not self.sitemap:
            raise ConnectorError("Give at least one start URL or a sitemap")
        for u in [*self.urls, self.sitemap]:
            if u and not u.startswith(("https://", "http://")):
                raise ConnectorError(f"Not a web address: {u}")
        self.max_pages = min(int(config.get("max_pages") or 200), 5000)
        self.exclude = [p.strip() for p in config.get("exclude", []) if p.strip()]
        first = self.urls[0] if self.urls else self.sitemap
        self.host = urlparse(first).netloc
        self.prefixes = [urlparse(u).path.rsplit("/", 1)[0] + "/" for u in self.urls] or ["/"]
        token = _secret(config.get("token_ref"), "site token")
        self.headers = {"User-Agent": "Ignite knowledge sync"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._pages: dict[str, tuple[str, list[Section]]] = {}

    def _fetch(self, url: str):
        import httpx

        try:
            r = httpx.get(url, headers=self.headers, timeout=30, follow_redirects=True)
        except httpx.HTTPError:
            return None
        if r.status_code >= 400 or urlparse(str(r.url)).netloc != self.host:
            return None
        return r

    def _allowed(self, url: str) -> bool:
        p = urlparse(url)
        if p.netloc != self.host or p.scheme not in {"http", "https"}:
            return False
        if re.search(r"\.(png|jpe?g|gif|svg|ico|css|js|zip|gz|pdf|mp4|woff2?)$", p.path, re.I):
            return False
        if any(x in url for x in self.exclude):
            return False
        return any(p.path.startswith(pre) for pre in self.prefixes) or not self.urls

    def _sitemap_urls(self) -> list[str]:
        r = self._fetch(self.sitemap)
        if r is None:
            raise ConnectorError(f"Couldn't read the sitemap {self.sitemap}")
        return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)

    def list(self) -> Iterator[Item]:
        queue = list(self.urls) + (self._sitemap_urls() if self.sitemap else [])
        seen: set[str] = set()
        while queue and len(self._pages) < self.max_pages:
            url = urldefrag(queue.pop(0))[0]
            if url in seen or not self._allowed(url):
                continue
            seen.add(url)
            r = self._fetch(url)
            if r is None or "html" not in r.headers.get("content-type", "html"):
                continue
            title, sections, links = html_sections(r.text)
            if self.urls:  # crawl from start pages; a sitemap already lists everything
                queue.extend(urljoin(url, href) for href in links)
            text = "\n".join(t for _, t in sections)
            if not text.strip():
                continue
            self._pages[url] = (title or url, sections)
            yield Item(external_id=url, title=title or url, url=url, version=_hash(text))
        if not self._pages and seen:
            raise ConnectorError("None of the pages could be read (are they behind a sign-in?)")

    def read(self, item: Item) -> tuple[str, list[Section]]:
        return self._pages[item.external_id]


# -- repo docs ------------------------------------------------------------------------------------
DOC_GLOBS = ["README*", "*.md", "docs/**", "doc/**", "adr/**", "**/adr/**", "**/README*",
             "runbooks/**", "CONTRIBUTING*", "ARCHITECTURE*"]
DOC_EXT = (".md", ".markdown", ".mdx", ".rst", ".adoc", ".txt")


class RepoDocs:
    """Reads docs from onboarded repos (one checkout per sync)."""

    def __init__(self, config: dict, repos: list):
        self.repos = repos
        self.globs = config.get("globs") or DOC_GLOBS
        if not repos:
            raise ConnectorError("Choose at least one onboarded repo")
        self._files: dict[str, tuple[str, list[Section]]] = {}

    def list(self) -> Iterator[Item]:
        from igniteai.readiness.scanner import checkout

        for repo in self.repos:
            try:
                with checkout(repo.ref) as root:
                    for path in sorted(root.rglob("*")):
                        rel = path.relative_to(root).as_posix()
                        if not path.is_file() or not rel.lower().endswith(DOC_EXT) \
                                or any(part.startswith(".") or part in {"node_modules",
                                                                        "vendor", "dist"}
                                       for part in rel.split("/")) \
                                or not any(fnmatch.fnmatch(rel, g) for g in self.globs) \
                                or path.stat().st_size > 2_000_000:
                            continue
                        text = path.read_text(errors="replace")
                        key = f"{repo.id}:{rel}"
                        self._files[key] = (f"{repo.name}/{rel}", markdown_sections(text))
                        url = _repo_url(repo.ref, rel)
                        yield Item(external_id=key, title=f"{repo.name}/{rel}", url=url,
                                   version=_hash(text), meta={"repo_id": repo.id})
            except ValueError as exc:
                raise ConnectorError(f"{repo.name}: {exc}") from exc

    def read(self, item: Item) -> tuple[str, list[Section]]:
        return self._files[item.external_id]


def _repo_url(ref: str, rel: str) -> str | None:
    m = re.match(r"^(?:https?://)?([^/]+)/([^/]+)/([^/.]+)", ref.removeprefix("git@").replace(
        ":", "/", 1) if ref.startswith("git@") else ref)
    return f"https://{m.group(1)}/{m.group(2)}/{m.group(3)}/blob/HEAD/{rel}" if m and "." in \
        m.group(1) else None


def connector(source, session=None):
    from sqlalchemy import select

    from igniteai.models import RepoRecord

    if source.kind == "confluence":
        return Confluence(source.config)
    if source.kind == "web":
        return Website(source.config)
    if source.kind == "repo":
        ids = source.config.get("repo_ids") or []
        repos = list(session.scalars(select(RepoRecord).where(RepoRecord.id.in_(ids)))) \
            if ids else []
        return RepoDocs(source.config, repos)
    raise ConnectorError(f"{source.kind} sources don't sync")
