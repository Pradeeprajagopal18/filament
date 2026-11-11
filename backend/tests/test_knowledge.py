"""Knowledge sources (RAG): reading documents, syncing, search, access, and use in Ask/agents."""
# ruff: noqa: F811 - the signed-in fixtures are imported from test_access

import io
import zipfile

import pytest
from test_access import env, world  # noqa: F401 - signed-in fixtures

from igniteai.knowledge import connectors
from igniteai.knowledge import embed as emb
from igniteai.knowledge import service as ks
from igniteai.knowledge.text import chunk, file_sections, html_sections, markdown_sections

RUNBOOK = """# Payments runbook

Intro text about the payments service.

## Rollback

To roll back a bad release, run `deploy rollback --to previous` and watch the error budget.
Page the on-call if the burn rate stays above 6x for an hour.

## Database failover

Promote the replica with `pgctl promote` and update the DSN secret.
"""


def _docx(paragraphs):
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="Heading{lvl}"/></w:pPr><w:r><w:t>{t}</w:t></w:r></w:p>'
        if lvl else f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for lvl, t in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


# -- reading documents ------------------------------------------------------------------------
def test_html_sections_follow_headings_tables_and_code():
    html = """<html><head><title>Deploys</title><script>x=1</script></head><body>
    <nav>menu</nav><h1>Deploys</h1><p>How we ship.</p><h2>Canary</h2><p>5% first.</p>
    <table><tr><th>Step</th><th>Owner</th></tr><tr><td>Promote</td><td>RM</td></tr></table>
    <ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter>
    <ac:plain-text-body><![CDATA[deploy --canary 5]]></ac:plain-text-body></ac:structured-macro>
    <h2>Rollback</h2><p>Use the button.</p></body></html>"""
    title, sections, _ = html_sections(html)
    assert title == "Deploys"
    paths = [p for p, _ in sections]
    assert ["Deploys", "Canary"] in paths and ["Deploys", "Rollback"] in paths
    canary = next(t for p, t in sections if p == ["Deploys", "Canary"])
    assert "Promote | RM" in canary.replace(" |  | ", " | ") or "Promote" in canary
    assert "deploy --canary 5" in canary and "menu" not in canary and "x=1" not in canary


def test_markdown_docx_and_chunking():
    secs = markdown_sections(RUNBOOK)
    assert [p for p, _ in secs][1] == ["Payments runbook", "Rollback"]
    title, dsecs = file_sections("Onboarding.docx", _docx([(1, "Onboarding"), (0, "Get a laptop"),
                                                            (2, "Access"), (0, "Ask IT")]))
    assert title == "Onboarding" and dsecs[1] == (["Onboarding", "Access"], "Ask IT")
    long = [([], "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20)))]
    parts = chunk("Big", long, max_words=200, overlap_words=30)
    assert len(parts) > 4 and all(len(t.split()) <= 260 for _, t in parts)
    assert all(h == "Big" for h, _ in parts)
    with pytest.raises(ValueError):
        file_sections("photo.png", b"\x89PNG")


# -- uploads, search and Ask ------------------------------------------------------------------
def _upload_source(client, ws_id, name="Runbooks"):
    s = client.post(f"/api/workspaces/{ws_id}/knowledge/sources",
                    json={"name": name, "kind": "upload"}).json()
    files = [("files", ("runbook.md", RUNBOOK.encode(), "text/markdown")),
             ("files", ("onboarding.docx", _docx([(1, "Onboarding"), (0, "New hires get a "
                                                                        "laptop on day one")]),
                        "application/octet-stream")),
             ("files", ("bad.png", b"\x89PNG", "image/png"))]
    r = client.post(f"/api/knowledge/{s['id']}/files", files=files).json()
    return s, r


def test_upload_search_and_ask(client, project):
    ws, proj = project
    s, r = _upload_source(client, ws["id"])
    ok = [x for x in r["results"] if "passages" in x]
    assert len(ok) == 2 and "error" in r["results"][2]
    assert r["source"]["doc_count"] == 2 and r["source"]["chunk_count"] >= 3
    hits = client.post(f"/api/workspaces/{ws['id']}/knowledge/search",
                       json={"query": "how do I roll back a release"}).json()["results"]
    assert hits[0]["heading"] == "runbook > Payments runbook > Rollback"
    assert "deploy rollback" in hits[0]["text"] and hits[0]["match"] == "keyword"
    ans = client.post(f"/api/workspaces/{ws['id']}/ask",
                      json={"question": "How do we roll back a release?"}).json()
    assert ans["answered"] and any(c.startswith("knowledge:") for c in ans["citations"]), ans
    cited = next(c for c in ans["citations"] if c.startswith("knowledge:"))
    assert ans["sources"][cited]["source"] == "Runbooks"
    detail = client.get(f"/api/knowledge/{s['id']}").json()
    assert {d["title"] for d in detail["docs"]} == {"runbook", "onboarding"}
    assert detail["hits"] >= 1
    # Removing a document removes its passages from search.
    doc = next(d for d in detail["docs"] if d["title"] == "runbook")
    client.delete(f"/api/knowledge/{s['id']}/docs/{doc['id']}")
    assert client.post(f"/api/workspaces/{ws['id']}/knowledge/search",
                       json={"query": "rollback release"}).json()["results"] == []


def test_agents_get_knowledge_in_context_and_as_a_tool(client, project):
    from igniteai.agents.tools import CITATION, TOOLS
    from igniteai.context import build_context_pack
    from igniteai.db import get_session
    from igniteai.models import Project

    ws, proj = project
    _upload_source(client, ws["id"])
    session = next(client.app.dependency_overrides[get_session]())
    p = session.get(Project, proj["id"])
    pack = build_context_pack(session, p, "Plan the rollback steps for a bad release",
                              agent="requirements")
    assert any(i["kind"] == "knowledge" for i in pack["items"])

    class FakeAgent:
        name = "requirements"
        project = p

    FakeAgent.session = session
    out = TOOLS["search_knowledge"].handler(FakeAgent(), {"query": "database failover"})
    assert out["results"] and "pgctl promote" in out["results"][0]["text"]
    assert CITATION.search(f"see knowledge:{out['results'][0]['id']}")


def test_knowledge_settings_are_validated(client, ws):
    url = f"/api/workspaces/{ws['id']}/knowledge/sources"
    r = client.post(url, json={"name": "Wiki", "kind": "confluence",
                               "config": {"base_url": "https://acme.atlassian.net",
                                          "spaces": ["ENG"], "token_ref": "abc123"}})
    assert r.status_code == 422 and "${env:" in r.json()["detail"]
    r = client.post(url, json={"name": "Wiki", "kind": "confluence",
                               "config": {"base_url": "https://acme.atlassian.net",
                                          "token_ref": "${env:CONF_TOKEN}"}})
    assert r.status_code == 422  # no spaces
    assert client.post(url, json={"name": "x", "kind": "gopher"}).status_code == 422
    assert client.post(url, json={"name": "x", "kind": "repo",
                                  "config": {"repo_ids": ["nope"]}}).status_code == 422
    assert client.post(url, json={"name": "x", "kind": "upload",
                                  "agents": ["ghost"]}).status_code == 422


# -- Confluence -------------------------------------------------------------------------------
class _Resp:
    def __init__(self, data=None, status=200, text="", ctype="application/json", url=""):
        self._data, self.status_code, self.text = data, status, text
        self.headers, self.url = {"content-type": ctype}, url

    def json(self):
        return self._data


@pytest.fixture
def confluence(monkeypatch):
    pages = {"101": (1, "Release process", "<h1>Release</h1><p>Every release needs a release "
                     "manager sign-off and a canary.</p>"),
             "102": (3, "On-call", "<h2>Paging</h2><p>Use PagerDuty escalation policy "
                     "payments-primary.</p>")}
    calls = []

    def get(url, params=None, auth=None, headers=None, **_):
        calls.append((url, params, auth, headers))
        if url.endswith("/rest/api/content/search"):
            assert 'space in ("ENG")' in params["cql"]
            return _Resp({"results": [{"id": pid, "title": t, "version": {"number": v},
                                       "space": {"key": "ENG"},
                                       "_links": {"webui": f"/spaces/ENG/pages/{pid}"}}
                                      for pid, (v, t, _) in pages.items()], "_links": {}})
        pid = url.rsplit("/", 1)[-1]
        v, t, body = pages[pid]
        return _Resp({"id": pid, "title": t, "body": {"storage": {"value": body}},
                      "ancestors": [{"title": "Engineering"}]})

    import httpx
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setenv("CONF_TOKEN", "secret-token")
    return pages, calls


def test_confluence_sync_is_incremental(client, ws, confluence):
    pages, calls = confluence
    s = client.post(f"/api/workspaces/{ws['id']}/knowledge/sources", json={
        "name": "Eng wiki", "kind": "confluence",
        "config": {"base_url": "https://acme.atlassian.net", "spaces": ["ENG"],
                   "email": "bot@acme.com", "token_ref": "${env:CONF_TOKEN}"}}).json()
    t = client.post(f"/api/knowledge/{s['id']}/test").json()
    assert t["ok"] and t["sample"][0]["url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/101"
    r = client.post(f"/api/knowledge/{s['id']}/sync?wait=true").json()["report"]
    assert r["added"] == 2 and r["failed"] == []
    assert calls[0][2] == ("bot@acme.com", "secret-token")  # Cloud: email + API token
    hits = client.post(f"/api/workspaces/{ws['id']}/knowledge/search",
                       json={"query": "pagerduty escalation"}).json()["results"]
    assert hits[0]["title"] == "Engineering > On-call"
    assert hits[0]["url"].endswith("/pages/102")
    # Nothing changed: no page is re-read.
    calls.clear()
    r = client.post(f"/api/knowledge/{s['id']}/sync?wait=true").json()["report"]
    assert r["unchanged"] == 2 and len(calls) == 1
    # One page edited, one deleted.
    pages["101"] = (2, "Release process", "<p>Releases now need two approvals.</p>")
    del pages["102"]
    r = client.post(f"/api/knowledge/{s['id']}/sync?wait=true").json()["report"]
    assert r["updated"] == 1 and r["removed"] == 1
    assert client.post(f"/api/workspaces/{ws['id']}/knowledge/search",
                       json={"query": "pagerduty"}).json()["results"] == []
    src = client.get(f"/api/knowledge/{s['id']}").json()
    assert src["status"] == "ok" and src["doc_count"] == 1 and src["next_sync_at"]


def test_confluence_data_center_and_missing_token(client, ws, confluence, monkeypatch):
    pages, calls = confluence
    s = client.post(f"/api/workspaces/{ws['id']}/knowledge/sources", json={
        "name": "DC wiki", "kind": "confluence",
        "config": {"base_url": "https://confluence.acme.com", "spaces": ["ENG"],
                   "token_ref": "${env:CONF_TOKEN}"}}).json()
    client.post(f"/api/knowledge/{s['id']}/sync?wait=true")
    assert calls[0][0] == "https://confluence.acme.com/rest/api/content/search"
    assert calls[0][3]["Authorization"] == "Bearer secret-token"
    monkeypatch.delenv("CONF_TOKEN")
    client.post(f"/api/knowledge/{s['id']}/sync?wait=true")
    src = client.get(f"/api/knowledge/{s['id']}").json()
    assert src["status"] == "error" and "CONF_TOKEN" in src["last_error"]
    assert src["doc_count"] == 2  # what was indexed stays searchable


# -- websites and repo docs -------------------------------------------------------------------
def test_website_crawl_stays_on_the_docs_path(client, ws, monkeypatch):
    site = {"https://docs.acme.com/handbook/": '<title>Handbook</title><a href="ci.html">CI</a>'
            '<a href="/blog/x">blog</a><a href="https://evil.com/">x</a><p>Welcome.</p>',
            "https://docs.acme.com/handbook/ci.html": "<title>CI</title><h1>CI</h1><p>Builds "
            "run on Jenkins with the shared library pipeline-lib.</p>",
            "https://docs.acme.com/blog/x": "<p>Off topic</p>"}
    fetched = []

    def get(url, **_):
        fetched.append(url)
        return _Resp(text=site.get(url, ""), status=200 if url in site else 404,
                     ctype="text/html", url=url)

    import httpx
    monkeypatch.setattr(httpx, "get", get)
    s = client.post(f"/api/workspaces/{ws['id']}/knowledge/sources", json={
        "name": "Handbook", "kind": "web",
        "config": {"urls": ["https://docs.acme.com/handbook/"]}}).json()
    r = client.post(f"/api/knowledge/{s['id']}/sync?wait=true").json()["report"]
    assert r["added"] == 2
    assert "https://docs.acme.com/blog/x" not in fetched and "https://evil.com/" not in fetched
    hits = client.post(f"/api/workspaces/{ws['id']}/knowledge/search",
                       json={"query": "jenkins shared library"}).json()["results"]
    assert hits[0]["url"] == "https://docs.acme.com/handbook/ci.html"


def test_repo_docs_follow_repo_visibility(world, tmp_path):
    c, Session, ids, H, U = world
    from igniteai.models import RepoRecord

    for name in ("pay", "web"):
        root = tmp_path / name
        (root / "docs" / "adr").mkdir(parents=True)
        (root / "README.md").write_text(f"# {name}\n\nThe {name} service uses Kafka topics.")
        (root / "docs" / "adr" / "001.md").write_text("# ADR 1\n\nWe chose Postgres.")
        (root / "src.py").write_text("print('not a doc')")
    with Session() as s:
        for rid in ("pay", "web"):
            s.get(RepoRecord, ids[rid]).ref = str(tmp_path / rid)
        s.commit()
    src = c.post(f"/api/workspaces/{ids['ws']}/knowledge/sources", headers=H["boss"], json={
        "name": "Repo docs", "kind": "repo", "config": {"repo_ids": [ids["pay"], ids["web"]]}}
                 ).json()
    r = c.post(f"/api/knowledge/{src['id']}/sync?wait=true", headers=H["boss"]).json()["report"]
    assert r["added"] == 4
    seen = lambda h: {x["title"].split("/")[0] for x in c.post(  # noqa: E731
        f"/api/workspaces/{ids['ws']}/knowledge/search", headers=h,
        json={"query": "kafka topics postgres", "limit": 10}).json()["results"]}
    assert seen(H["boss"]) == {"pay", "web"}
    assert seen(H["bob"]) == {"web"}  # pay lives in a restricted project Bob can't see


# -- access -----------------------------------------------------------------------------------
def test_sources_limited_to_teams_and_agents(world):
    c, Session, ids, H, U = world
    s = c.post(f"/api/workspaces/{ids['ws']}/knowledge/sources", headers=H["boss"], json={
        "name": "Payments wiki", "kind": "upload", "audience": "some", "teams": [ids["team"]],
        "agents": ["sre"]}).json()
    c.post(f"/api/knowledge/{s['id']}/files", headers=H["boss"],
           files=[("files", ("runbook.md", RUNBOOK.encode(), "text/markdown"))])
    q = {"query": "database failover replica"}
    url = f"/api/workspaces/{ids['ws']}/knowledge/search"
    assert c.post(url, headers=H["alice"], json=q).json()["results"]  # on the team
    assert c.post(url, headers=H["bob"], json=q).json()["results"] == []
    # Only admins manage sources.
    assert c.get(f"/api/workspaces/{ids['ws']}/knowledge", headers=H["alice"]).status_code == 403
    assert c.post(f"/api/knowledge/{s['id']}/sync", headers=H["alice"]).status_code == 403
    # Agents: only the SRE agent may use this source.
    from igniteai import access

    with Session() as session:
        boss = access.from_user(session.get(__import__("igniteai.models").models.User,
                                            U["boss"]))
        assert ks.search(session, ids["ws"], q["query"], p=boss, agent="sre")
        assert ks.search(session, ids["ws"], q["query"], p=boss, agent="requirements") == []
    # Turning the feature off blocks search for non-admins.
    c.put(f"/api/workspaces/{ids['ws']}/features/knowledge", headers=H["boss"],
          json={"mode": "off"})
    assert c.post(url, headers=H["alice"], json=q).status_code == 403


# -- meaning search ---------------------------------------------------------------------------
class FakeEmbedder(emb.Embedder):
    """Maps words to a few 'concepts' so passages match questions that share no words."""

    name = "endpoint:fake"
    CONCEPTS = [{"rollback", "revert", "undo", "back"}, {"database", "postgres", "replica",
                                                         "failover", "db"},
                {"laptop", "computer", "hardware"}, {"page", "pager", "on-call", "escalate"}]

    def embed(self, texts):
        out = []
        for t in texts:
            words = set(t.lower().replace("`", " ").split())
            out.append([1.0 + len(words & c) * 5 for c in self.CONCEPTS])
        return out


def test_meaning_search_with_a_self_hosted_model(client, ws, monkeypatch):
    monkeypatch.setattr(emb, "for_index", lambda index: FakeEmbedder()
                        if index is not None and index.provider != "none" else None)
    r = client.put(f"/api/workspaces/{ws['id']}/knowledge/index?reembed=false", json={
        "provider": "endpoint", "endpoint_url": "http://ollama.internal:11434/v1",
        "model": "nomic-embed-text"})
    assert r.status_code == 200 and r.json()["provider"] == "endpoint"
    assert client.post(f"/api/workspaces/{ws['id']}/knowledge/index/test", json={
        "provider": "endpoint", "endpoint_url": "http://ollama.internal:11434/v1"}).json()["ok"]
    assert client.put(f"/api/workspaces/{ws['id']}/knowledge/index", json={
        "provider": "endpoint", "endpoint_url": "x"}).status_code == 422
    assert client.put(f"/api/workspaces/{ws['id']}/knowledge/index", json={
        "provider": "endpoint", "endpoint_url": "http://x", "api_key_ref": "sk-123"}
                      ).status_code == 422
    _upload_source(client, ws["id"])
    hits = client.post(f"/api/workspaces/{ws['id']}/knowledge/search",
                       json={"query": "undo revert"}).json()["results"]
    assert hits and "Rollback" in hits[0]["heading"] and hits[0]["match"] == "meaning"


def test_scheduled_sync(client, ws, confluence):
    from igniteai.db import get_session
    from igniteai.models import KnowledgeSource

    s = client.post(f"/api/workspaces/{ws['id']}/knowledge/sources", json={
        "name": "Eng wiki", "kind": "confluence", "sync_hours": 6,
        "config": {"base_url": "https://acme.atlassian.net", "spaces": ["ENG"],
                   "email": "bot@acme.com", "token_ref": "${env:CONF_TOKEN}"}}).json()
    session = next(client.app.dependency_overrides[get_session]())
    assert ks.sync_due(session) == 1
    assert ks.sync_due(session) == 0  # next one in 6 hours
    assert session.get(KnowledgeSource, s["id"]).doc_count == 2


def test_omni_search_tool(client, ws):
    _upload_source(client, ws["id"])
    r = client.post(f"/api/workspaces/{ws['id']}/mcp/omni", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ignite_search_knowledge", "arguments": {"query": "laptop day one"}}}
                    ).json()
    text = str(r["result"])
    assert "laptop" in text and "Onboarding" in text
    assert connectors.SECRET_REF.match("${env:X}")
