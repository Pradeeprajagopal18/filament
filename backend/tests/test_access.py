"""Sign-in (OIDC), tokens, and who can see and do what."""

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from filament import access, auth
from filament.config import get_settings
from filament.db import Base, get_session, make_engine
from filament.main import create_app
from filament.models import Artifact, Membership, Project, RepoRecord, Team, User, Workspace
from filament.repos import system as sysmap

ISSUER = "https://sso.example.com"


@pytest.fixture
def env(monkeypatch):
    for k, v in {"FILAMENT_AUTH_MODE": "oidc", "FILAMENT_OIDC_ISSUER": ISSUER,
                 "FILAMENT_OIDC_CLIENT_ID": "filament", "FILAMENT_SESSION_SECRET": "s3cret" * 6,
                 "FILAMENT_ADMIN_EMAILS": '["boss@acme.com"]'}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    access._gh_cache.clear()
    engine = make_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = create_app()
    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c, Session
    get_settings.cache_clear()


def _user(Session, email, admin=False, github=None):
    with Session() as s:
        u = User(sub=f"sub-{email}", email=email, name=email.split("@")[0], is_admin=admin,
                 github_login=github)
        s.add(u)
        s.flush()
        raw, _ = auth.new_token(s, u, "test")
        s.commit()
        return u.id, {"Authorization": f"Bearer {raw}"}


@pytest.fixture
def world(env):
    """A workspace with an open project, a restricted one, and a private repo."""
    c, Session = env
    boss, H_boss = _user(Session, "boss@acme.com", admin=True)
    alice, H_alice = _user(Session, "alice@acme.com")
    bob, H_bob = _user(Session, "bob@acme.com", github="bobgh")
    with Session() as s:
        ws = Workspace(name="Acme")
        s.add(ws)
        s.flush()
        team = Team(workspace_id=ws.id, name="Payments")
        s.add(team)
        s.flush()
        open_p = Project(workspace_id=ws.id, name="Web")
        secret = Project(workspace_id=ws.id, name="Payments core", restricted=True,
                         team_id=team.id)
        s.add_all([open_p, secret])
        s.flush()
        s.add(Membership(user_id=alice, scope="team", scope_id=team.id, role="contributor"))
        api = {"endpoints": [{"method": "POST", "path": "/charge"}]}
        web = RepoRecord(workspace_id=ws.id, project_id=open_p.id, ref="/src/web", name="web",
                         kinds=["frontend"], analysis={})
        pay = RepoRecord(workspace_id=ws.id, project_id=secret.id, ref="/src/pay", name="pay",
                         kinds=["backend"], analysis={"api": api})
        vault = RepoRecord(workspace_id=ws.id, project_id=open_p.id,
                           ref="https://github.com/acme/vault", name="vault", kinds=["backend"],
                           analysis={"api": api}, private=True)
        s.add_all([web, pay, vault])
        s.flush()
        issue = Artifact(project_id=open_p.id, type="issue", title="Add Apple Pay")
        s.add(issue)
        s.commit()
        ids = dict(ws=ws.id, team=team.id, open=open_p.id, secret=secret.id, web=web.id,
                   pay=pay.id, vault=vault.id, issue=issue.id)
    return c, Session, ids, {"boss": H_boss, "alice": H_alice, "bob": H_bob}, \
        {"boss": boss, "alice": alice, "bob": bob}


def test_sign_in_required(env):
    c, _ = env
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/config").json()["mode"] == "oidc"
    r = c.get("/api/workspaces")
    assert r.status_code == 401
    assert c.get("/api/workspaces", headers={"Authorization": "Bearer fil_nope"}).status_code \
        == 401


def test_restricted_project_hidden_from_non_members(world):
    c, _, ids, H, _ = world
    names = lambda h: {p["name"] for p in c.get(  # noqa: E731
        f"/api/workspaces/{ids['ws']}/projects", headers=h).json()}
    assert names(H["bob"]) == {"Web"}
    assert names(H["alice"]) == {"Web", "Payments core"}  # via the team
    assert names(H["boss"]) == {"Web", "Payments core"}
    # Unknown, not forbidden: existence doesn't leak.
    assert c.get(f"/api/projects/{ids['secret']}", headers=H["bob"]).status_code == 404
    assert c.get(f"/api/projects/{ids['secret']}/graph", headers=H["bob"]).status_code == 404
    assert c.get(f"/api/repos/{ids['pay']}", headers=H["bob"]).status_code == 404
    assert c.get(f"/api/repos/{ids['pay']}", headers=H["alice"]).status_code == 200


def test_private_repo_mirrors_github(world, monkeypatch):
    c, Session, ids, H, U = world
    repos = lambda h: {r["name"] for r in c.get(  # noqa: E731
        f"/api/workspaces/{ids['ws']}/repos", headers=h).json()}
    # Alice has no GitHub login and no explicit role on the Web project: can't tell, so hidden.
    assert repos(H["alice"]) == {"web", "pay"}
    calls = []

    def fake(ref, login):
        calls.append((ref, login))
        return login == "bobgh"

    monkeypatch.setattr(access, "_github_can_read", fake)
    assert repos(H["bob"]) == {"web", "vault"}
    assert calls == [("https://github.com/acme/vault", "bobgh")]
    # Fallback when GitHub can't say: an explicit role on the repo's project.
    monkeypatch.setattr(access, "_github_can_read", lambda ref, login: None)
    with Session() as s:
        s.add(Membership(user_id=U["alice"], scope="project", scope_id=ids["open"],
                         role="viewer"))
        s.commit()
    assert "vault" in repos(H["alice"])


def test_system_map_shows_hidden_repos_without_details(world):
    c, _, ids, H, _ = world
    m = c.get(f"/api/workspaces/{ids['ws']}/system-map", headers=H["bob"]).json()
    nodes = {n["label"]: n for n in m["nodes"] if n["kind"] == "repo"}
    assert set(nodes) == {"web", "pay", "vault"}  # the whole system is on the map
    assert nodes["pay"]["restricted"] and nodes["pay"]["endpoints"] == 0
    assert nodes["pay"]["grade"] is None
    assert not nodes["web"].get("restricted")
    assert "restricted" in m["mermaid"]
    full = c.get(f"/api/workspaces/{ids['ws']}/system-map", headers=H["boss"]).json()
    assert not any(n.get("restricted") for n in full["nodes"])


def test_redact_strips_endpoints_and_evidence():
    m = {"nodes": [{"id": "a", "label": "a", "kind": "repo", "kinds": ["frontend"], "grade": "B",
                    "endpoints": 0, "pages": 3},
                   {"id": "b", "label": "b", "kind": "repo", "kinds": ["backend"], "grade": "A",
                    "endpoints": 4, "pages": 0}],
         "edges": [{"source": "a", "target": "b", "kind": "calls", "confidence": "extracted",
                    "endpoints": ["POST /charge"], "evidence": [{"file": "a.ts", "line": 3}]},
                   {"source": "b", "target": "a", "kind": "configures", "endpoints": [],
                    "confidence": "inferred", "evidence": [{"file": "b/secret.yaml"}]}],
         "unresolved": [{"repo_id": "a", "target_id": "b", "path": "/x"}], "consumers": {}}
    r = sysmap.redact(m, {"b"})
    e1, e2 = r["edges"]
    assert e1["endpoints"] == [] and e1["evidence"] == [{"file": "a.ts", "line": 3}]
    assert e2["evidence"] == []  # read from the hidden repo's files
    assert r["unresolved"] == [] and "consumers" not in r
    assert sysmap.redact(m, set()) is m


def test_roles_limit_writes_and_admin_routes(world):
    c, Session, ids, H, U = world
    with Session() as s:
        s.add(Membership(user_id=U["bob"], scope="project", scope_id=ids["open"], role="viewer"))
        s.commit()
    body = {"type": "doc", "title": "x", "content": "y"}
    assert c.post(f"/api/projects/{ids['open']}/artifacts", json=body,
                  headers=H["bob"]).status_code == 403
    assert c.post(f"/api/projects/{ids['open']}/artifacts", json=body,
                  headers=H["alice"]).status_code == 201  # open project: contributor
    assert c.get(f"/api/workspaces/{ids['ws']}/config", headers=H["alice"]).status_code == 403
    assert c.get(f"/api/workspaces/{ids['ws']}/people", headers=H["alice"]).status_code == 403
    assert c.post("/api/workspaces", json={"name": "X"}, headers=H["alice"]).status_code == 403
    assert c.get(f"/api/workspaces/{ids['ws']}/config", headers=H["boss"]).status_code == 200
    audit = c.get(f"/api/workspaces/{ids['ws']}/audit", headers=H["boss"]).json()
    assert any(a["action"] == "access.denied" and a["actor"] == "bob@acme.com" for a in audit)


def test_checkpoints_need_their_role(world):
    c, _, ids, H, U = world
    url = f"/api/artifacts/{ids['issue']}/approve"
    ok = {"decision": "approved", "decided_by": "spoofed"}
    # Nobody holds product_owner yet: only a maintainer (or admin) may sign off.
    assert c.post(url, json=ok, headers=H["alice"]).status_code == 403
    r = c.put(f"/api/workspaces/{ids['ws']}/checkpoint-roles", headers=H["boss"],
              json={"user_id": U["alice"], "role": "product_owner", "project_id": ids["open"]})
    assert r.status_code == 201
    assert c.post(url, json=ok, headers=H["bob"]).status_code == 403
    assert c.post(url, json=ok, headers=H["alice"]).status_code == 200
    appr = c.get(f"/api/projects/{ids['open']}/approvals?status=approved",
                 headers=H["alice"]).json()
    assert appr[0]["decided_by"] == "alice@acme.com"  # recorded from the session, not the body


def test_project_owner_and_members(world):
    c, _, ids, H, U = world
    p = c.post(f"/api/workspaces/{ids['ws']}/projects", headers=H["bob"],
               json={"name": "Bob's", "restricted": True}).json()
    assert p["restricted"] is True
    acc = c.get(f"/api/projects/{p['id']}/access", headers=H["bob"]).json()
    assert acc["my_role"] == "owner"
    assert c.get(f"/api/projects/{p['id']}", headers=H["alice"]).status_code == 404
    c.put(f"/api/projects/{p['id']}/members", headers=H["bob"],
          json=[{"user_id": U["bob"], "role": "owner"}, {"user_id": U["alice"], "role": "viewer"}])
    assert c.get(f"/api/projects/{p['id']}", headers=H["alice"]).status_code == 200


def test_memory_follows_scope(world):
    c, _, ids, H, _ = world
    c.post(f"/api/workspaces/{ids['ws']}/memory", headers=H["alice"],
           json={"scope": "project", "scope_ref": ids["secret"], "content": "PCI zone rules"})
    assert c.post(f"/api/workspaces/{ids['ws']}/memory", headers=H["bob"],
                  json={"scope": "project", "scope_ref": ids["secret"], "content": "x"}
                  ).status_code == 404
    seen = lambda h: [m["content"] for m in c.get(  # noqa: E731
        f"/api/workspaces/{ids['ws']}/memory", headers=h).json()]
    assert seen(H["alice"]) == ["PCI zone rules"]
    assert seen(H["bob"]) == []
    # Org-wide memory from a non-admin waits for review.
    m = c.post(f"/api/workspaces/{ids['ws']}/memory", headers=H["bob"],
               json={"scope": "org", "content": "Use UTC everywhere"}).json()
    assert m["status"] == "proposed" and m["author"] == "bob@acme.com"


def test_runs_act_for_the_requester(world):
    c, _, ids, H, U = world
    r = c.post(f"/api/projects/{ids['open']}/runs", headers=H["alice"],
               json={"agent": "requirements", "goal": "Draft", "input": {"idea": "Apple Pay"},
                     "requested_by": "someone-else"}).json()
    assert r["requested_by"] == f"user:{U['alice']}"
    usage = c.get(f"/api/workspaces/{ids['ws']}/usage?by=person", headers=H["boss"]).json()
    assert usage[0]["key"] == "alice@acme.com"


def test_tokens_revoke(world):
    c, _, ids, H, _ = world
    made = c.post("/api/auth/tokens", headers=H["bob"], json={"name": "laptop"}).json()
    h = {"Authorization": f"Bearer {made['token']}"}
    assert c.get("/api/auth/me", headers=h).json()["email"] == "bob@acme.com"
    listed = c.get("/api/auth/tokens", headers=H["bob"]).json()
    assert "token" not in listed[0] and listed[0]["prefix"].startswith("fil_")
    c.delete(f"/api/auth/tokens/{made['id']}", headers=H["bob"])
    assert c.get("/api/auth/me", headers=h).status_code == 401


def test_people_admin(world):
    c, Session, ids, H, U = world
    c.post(f"/api/workspaces/{ids['ws']}/people", headers=H["boss"],
           json={"email": "Carol@acme.com", "workspace_role": "admin", "github_login": "carolgh"})
    people = {p["email"]: p for p in c.get(f"/api/workspaces/{ids['ws']}/people",
                                           headers=H["boss"]).json()}
    assert people["carol@acme.com"]["workspace_role"] == "admin"
    r = c.patch(f"/api/workspaces/{ids['ws']}/people/{U['bob']}", headers=H["boss"],
                json={"active": False})
    assert r.json()["active"] is False
    assert c.get("/api/auth/me", headers=H["bob"]).status_code == 401


# -- the OIDC flow ----------------------------------------------------------------------------
def test_oidc_login_flow(world, monkeypatch):
    c, Session, ids, H, _ = world
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    conf = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/keys"}
    monkeypatch.setattr(auth, "discovery", lambda: conf)
    real_verify = auth.verify_id_token
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok, nonce, conf, key=None: real_verify(tok, nonce, conf,
                                                                       key=pub))
    pub = key.public_key()
    with Session() as s:
        s.get(Team, ids["team"]).sso_group = "eng-payments"
        s.commit()

    r = c.get("/api/auth/login?next=/projects", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["filament"]
    state, nonce = q["state"][0], q["nonce"][0]

    def fake_post(url, data, **_):
        assert url == conf["token_endpoint"] and data["code"] == "abc"
        assert data["code_verifier"]  # PKCE
        now = int(time.time())
        tok = jwt.encode({"iss": ISSUER, "aud": "filament", "sub": "okta|dana", "iat": now,
                          "exp": now + 300, "nonce": nonce, "email": "dana@acme.com",
                          "name": "Dana", "groups": ["eng-payments"]}, key, algorithm="RS256")

        class R:
            status_code = 200

            def json(self):
                return {"id_token": tok}
        return R()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    r = c.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"].endswith("/projects"), r.headers["location"]
    assert auth.SESSION_COOKIE in r.cookies
    me = c.get("/api/auth/me").json()  # the cookie is now on the client
    assert me["email"] == "dana@acme.com" and me["via"] == "session"
    # Her SSO group put her on the Payments team, which opens the restricted project.
    names = {p["name"] for p in c.get(f"/api/workspaces/{ids['ws']}/projects").json()}
    assert "Payments core" in names
    # A tampered state is refused.
    c.cookies.clear()
    r = c.get("/api/auth/login", follow_redirects=False)
    r = c.get("/api/auth/callback?code=abc&state=forged", follow_redirects=False)
    assert "login_error" in r.headers["location"]


def test_bad_id_token_rejected():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    get_settings.cache_clear()
    import os
    os.environ["FILAMENT_OIDC_CLIENT_ID"] = "filament"
    try:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "filament", "sub": "x", "iat": now, "exp": now + 60,
                  "nonce": "n"}
        good = jwt.encode(claims, key, algorithm="RS256")
        conf = {"issuer": ISSUER}
        assert auth.verify_id_token(good, "n", conf, key=key.public_key())["sub"] == "x"
        for tok, nonce in [(jwt.encode(claims, other, algorithm="RS256"), "n"), (good, "m"),
                           (jwt.encode({**claims, "aud": "evil"}, key, algorithm="RS256"), "n"),
                           (jwt.encode({**claims, "exp": now - 600}, key, algorithm="RS256"),
                            "n")]:
            with pytest.raises(auth.AuthError):
                auth.verify_id_token(tok, nonce, conf, key=key.public_key())
    finally:
        os.environ.pop("FILAMENT_OIDC_CLIENT_ID")
        get_settings.cache_clear()


def test_local_mode_is_unrestricted(client):
    ws = client.post("/api/workspaces", json={"name": "Solo"}).json()
    assert client.get("/api/auth/me").json()["via"] == "local"
    p = client.post(f"/api/workspaces/{ws['id']}/projects",
                    json={"name": "Mine", "restricted": True}).json()
    assert client.get(f"/api/projects/{p['id']}").status_code == 200
