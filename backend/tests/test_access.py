"""Sign-in (OIDC), tokens, and who can see and do what."""

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from igniteai import access, auth
from igniteai.config import get_settings
from igniteai.db import Base, get_session, make_engine
from igniteai.main import create_app
from igniteai.models import Artifact, Membership, Project, RepoRecord, Team, User, Workspace
from igniteai.repos import system as sysmap

ISSUER = "https://sso.example.com"


@pytest.fixture
def env(monkeypatch):
    for k, v in {"IGNITE_AUTH_MODE": "login",
                 "IGNITE_AUTH_METHODS": '["local","ldap","oidc"]', "IGNITE_OIDC_ISSUER": ISSUER,
                 "IGNITE_OIDC_CLIENT_ID": "ignite", "IGNITE_SESSION_SECRET": "s3cret" * 6,
                 "IGNITE_ADMIN_EMAILS": '["boss@acme.com"]'}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    access._gh_cache.clear()
    from igniteai import authconfig

    authconfig._override = {}
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
    conf = c.get("/api/auth/config").json()
    assert conf["mode"] == "login" and conf["password"] and conf["sso_url"]
    r = c.get("/api/workspaces")
    assert r.status_code == 401
    assert c.get("/api/workspaces", headers={"Authorization": "Bearer ign_nope"}).status_code \
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
    assert "token" not in listed[0] and listed[0]["prefix"].startswith("ign_")
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

    r = c.get("/api/auth/oidc/login?next=/projects", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["ignite"]
    state, nonce = q["state"][0], q["nonce"][0]

    def fake_post(url, data, **_):
        assert url == conf["token_endpoint"] and data["code"] == "abc"
        assert data["code_verifier"]  # PKCE
        now = int(time.time())
        tok = jwt.encode({"iss": ISSUER, "aud": "ignite", "sub": "okta|dana", "iat": now,
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
    r = c.get("/api/auth/oidc/login", follow_redirects=False)
    r = c.get("/api/auth/callback?code=abc&state=forged", follow_redirects=False)
    assert "login_error" in r.headers["location"]


def test_bad_id_token_rejected():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    get_settings.cache_clear()
    import os
    os.environ["IGNITE_OIDC_CLIENT_ID"] = "ignite"
    try:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "ignite", "sub": "x", "iat": now, "exp": now + 60,
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
        os.environ.pop("IGNITE_OIDC_CLIENT_ID")
        get_settings.cache_clear()


def test_local_mode_is_unrestricted(client):
    ws = client.post("/api/workspaces", json={"name": "Solo"}).json()
    assert client.get("/api/auth/me").json()["via"] == "local"
    p = client.post(f"/api/workspaces/{ws['id']}/projects",
                    json={"name": "Mine", "restricted": True}).json()
    assert client.get(f"/api/projects/{p['id']}").status_code == 200


# -- local accounts ---------------------------------------------------------------------------
def test_first_admin_must_change_password(env):
    c, Session = env
    assert c.post("/api/auth/login", json={"username": "admin", "password": "nope"}
                  ).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    me = c.get("/api/auth/me").json()
    assert me["username"] == "admin" and me["must_change_password"] and me["is_admin"]
    # Nothing else until the password is changed.
    r = c.get("/api/workspaces")
    assert r.status_code == 403 and r.json()["detail"] == "password_change_required"
    assert c.post("/api/auth/password", json={"current": "admin", "new": "short"}
                  ).status_code == 422
    assert c.post("/api/auth/password", json={"current": "admin", "new": "administrator"}
                  ).status_code == 422
    assert c.post("/api/auth/password", json={"current": "wrong", "new": "a-much-better-one"}
                  ).status_code == 403
    old = dict(c.cookies)
    assert c.post("/api/auth/password", json={"current": "admin", "new": "a-much-better-one"}
                  ).status_code == 200
    assert c.get("/api/workspaces").status_code == 200
    # admin / admin no longer works, and the bootstrap doesn't recreate it.
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"username": "admin", "password": "admin"}
                  ).status_code == 401
    with Session() as s:
        assert len(s.query(User).filter(User.username == "admin").all()) == 1
    # Sessions from before the change are over (on every device).
    c.cookies.update(old)
    assert c.get("/api/auth/me").status_code == 401


def test_lockout_after_repeated_failures(env):
    c, _ = env
    for _ in range(5):
        c.post("/api/auth/login", json={"username": "admin", "password": "guess"})
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 401 and "locked" in r.json()["detail"]
    # Unknown names are throttled too, with the same message as a wrong password at first.
    first = c.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert first.json()["detail"] == "Wrong username or password"


def test_admin_creates_local_account_and_resets_password(world):
    c, Session, ids, H, U = world
    r = c.post(f"/api/workspaces/{ids['ws']}/people", headers=H["boss"],
               json={"local_username": "contractor1", "name": "Contractor"}).json()
    temp = r["temporary_password"]
    assert r["auth_source"] == "local" and r["must_change_password"]
    assert c.post(f"/api/workspaces/{ids['ws']}/people", headers=H["boss"],
                  json={"local_username": "contractor1"}).status_code == 409
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"username": "contractor1", "password": temp}
                  ).json()["must_change_password"]
    c.post("/api/auth/password", json={"current": temp, "new": "contractor-pass-123"})
    assert c.get(f"/api/workspaces/{ids['ws']}/projects").status_code == 200
    reset = c.post(f"/api/workspaces/{ids['ws']}/people/{r['id']}/reset-password",
                   headers=H["boss"]).json()["temporary_password"]
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"username": "contractor1",
                                           "password": "contractor-pass-123"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "contractor1", "password": reset}
                  ).status_code == 200
    # Only admins reset passwords.
    assert c.post(f"/api/workspaces/{ids['ws']}/people/{r['id']}/reset-password",
                  headers=H["alice"]).status_code == 403


def test_password_hashing():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct" not in h
    assert auth.check_password("correct horse", h)
    assert not auth.check_password("Correct horse", h)
    assert not auth.check_password("x", None) and not auth.check_password("x", "md5$abc")


# -- LDAP -------------------------------------------------------------------------------------
@pytest.fixture
def directory(monkeypatch):
    import ldap3

    server = ldap3.Server("mock_ldap")
    seed = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    seed.strategy.add_entry("cn=svc,ou=apps,dc=acme,dc=com", {"userPassword": "svc-pass"})
    seed.strategy.add_entry("uid=erin,ou=people,dc=acme,dc=com", {
        "uid": "erin", "userPassword": "erin-ldap-pass", "mail": "Erin@acme.com",
        "displayName": "Erin Diaz", "objectClass": "person",
        "memberOf": ["cn=eng-payments,ou=groups,dc=acme,dc=com"]})
    seed.strategy.add_entry("uid=evil*,ou=people,dc=acme,dc=com", {"uid": "evil*",
                                                                   "userPassword": "x"})

    def connect(_server, user, password):
        conn = ldap3.Connection(server, user=user, password=password,
                                client_strategy=ldap3.MOCK_SYNC)
        conn.strategy.connection.server.dit = seed.strategy.connection.server.dit
        return conn if conn.bind() else None

    monkeypatch.setattr(auth, "_ldap_connect", connect)
    monkeypatch.setattr(auth, "_ldap_server", lambda: server)
    for k, v in {"IGNITE_LDAP_URL": "ldaps://ldap.acme.com", "IGNITE_LDAP_BASE_DN":
                 "ou=people,dc=acme,dc=com", "IGNITE_LDAP_BIND_DN": "cn=svc,ou=apps,dc=acme,dc=com",
                 "IGNITE_LDAP_BIND_PASSWORD": "svc-pass",
                 "IGNITE_LDAP_USER_FILTER": "(uid={username})"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_ldap_sign_in_and_groups(world, directory):
    c, Session, ids, H, _ = world
    with Session() as s:
        s.get(Team, ids["team"]).sso_group = "eng-payments"  # a group by its CN
        s.commit()
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"username": "erin", "password": "wrong"}
                  ).status_code == 401
    assert c.post("/api/auth/login", json={"username": "erin", "password": ""}
                  ).status_code == 401  # never an anonymous bind
    r = c.post("/api/auth/login", json={"username": "erin", "password": "erin-ldap-pass"})
    assert r.status_code == 200 and not r.json()["must_change_password"]
    me = c.get("/api/auth/me").json()
    assert me["email"] == "erin@acme.com" and me["auth_source"] == "ldap"
    names = {p["name"] for p in c.get(f"/api/workspaces/{ids['ws']}/projects").json()}
    assert "Payments core" in names  # via her directory group
    # Directory passwords aren't changed in Ignite.
    assert c.post("/api/auth/password", json={"current": "erin-ldap-pass", "new": "x" * 20}
                  ).status_code == 400
    # Filter injection: a '*' in the name is escaped, so it can't match other entries.
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"username": "*", "password": "x"}).status_code == 401


# -- sign-in settings from the web app --------------------------------------------------------
def test_admin_changes_sign_in_methods(world, directory, monkeypatch):
    c, Session, ids, H, U = world
    for k in ("IGNITE_LDAP_URL", "IGNITE_LDAP_BASE_DN"):
        monkeypatch.delenv(k)
    get_settings.cache_clear()
    monkeypatch.setenv("IGNITE_AUTH_METHODS", '["local"]')
    get_settings.cache_clear()
    view = c.get("/api/auth/settings", headers=H["boss"]).json()
    assert view["fields"]["auth_methods"] == {"value": ["local"], "source": "environment"}
    assert c.get("/api/auth/settings", headers=H["alice"]).status_code == 403
    # LDAP can't go on half-configured, and secrets are only references.
    r = c.put("/api/auth/settings", headers=H["boss"], json={"auth_methods": ["local", "ldap"]})
    assert r.status_code == 422 and "URL" in r.json()["detail"]
    r = c.put("/api/auth/settings", headers=H["boss"], json={"ldap_bind_password": "hunter2"})
    assert r.status_code == 422 and "${env:" in r.json()["detail"]
    monkeypatch.setenv("LDAP_SVC_PW", "svc-pass")
    form = {"auth_methods": ["local", "ldap"], "ldap_url": "ldaps://ldap.acme.com",
            "ldap_base_dn": "ou=people,dc=acme,dc=com",
            "ldap_bind_dn": "cn=svc,ou=apps,dc=acme,dc=com",
            "ldap_bind_password": "${env:LDAP_SVC_PW}"}
    t = c.post("/api/auth/settings/test-ldap", headers=H["boss"],
               json={"settings": form, "username": "erin", "password": "erin-ldap-pass"}).json()
    assert t["ok"] and "Erin Diaz" in t["message"]
    r = c.put("/api/auth/settings", headers=H["boss"], json=form).json()
    assert r["fields"]["ldap_url"]["source"] == "saved"
    assert r["fields"]["ldap_bind_password"] == {"value": "${env:LDAP_SVC_PW}",
                                                 "source": "saved", "set": True,
                                                 "resolves": True}
    # The saved settings take effect straight away: Erin signs in through the directory.
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"username": "erin", "password": "erin-ldap-pass"}
                  ).status_code == 200
    audit = c.get(f"/api/workspaces/{ids['ws']}/audit", headers=H["boss"]).json()
    assert any(a["action"] == "auth.settings" for a in audit)
    assert "svc-pass" not in str(audit)


def test_cannot_lock_admins_out(world, monkeypatch):
    c, Session, ids, H, U = world
    monkeypatch.setenv("IGNITE_AUTH_METHODS", '["local","oidc"]')
    get_settings.cache_clear()
    # No admin has ever signed in through LDAP or SSO, so local accounts must stay on.
    r = c.put("/api/auth/settings", headers=H["boss"], json={"auth_methods": []})
    assert r.status_code == 422
    r = c.put("/api/auth/settings", headers=H["boss"], json={
        "auth_methods": ["oidc"], "oidc_issuer": ISSUER, "oidc_client_id": "ignite"})
    assert r.status_code == 422 and "lock" in r.json()["detail"]
    with Session() as s:
        boss = s.get(User, U["boss"])
        boss.auth_source, boss.last_login_at = "oidc", __import__("datetime").datetime.now()
        s.commit()
    r = c.put("/api/auth/settings", headers=H["boss"], json={
        "auth_methods": ["oidc"], "oidc_issuer": ISSUER, "oidc_client_id": "ignite"})
    assert r.status_code == 200
    assert c.get("/api/auth/config").json()["password"] is False
    assert c.post("/api/auth/login", json={"username": "admin", "password": "admin"}
                  ).status_code == 400
    # null puts a value back to the environment.
    r = c.put("/api/auth/settings", headers=H["boss"], json={"auth_methods": None})
    assert r.status_code == 200, r.text
    r = r.json()
    assert r["fields"]["auth_methods"]["source"] == "environment"


def test_reset_auth_from_the_server(world, monkeypatch):
    from igniteai import authconfig

    c, Session, ids, H, U = world
    with Session() as s:
        authconfig.save(s, {"auth_methods": ["oidc"]}, "test")
        s.commit()
        authconfig.reset(s)
        s.commit()
    assert authconfig.cfg().auth_methods == ["local", "ldap", "oidc"]  # the environment again
    assert c.get("/api/auth/config").json()["methods"] == ["local", "ldap", "oidc"]


# -- features --------------------------------------------------------------------------------
def test_feature_catalog_matches_routes():
    from igniteai import features
    from igniteai.api import code, config, delivery, gateway, memory, repos, runs

    paths = {"/api" + r.path for m in (memory, runs, repos, gateway, code, delivery, config)
             for r in m.router.routes}
    for key, (_, _, routes) in features.CATALOG.items():
        for _, path in routes:
            assert path in paths, (key, path)


def test_admin_limits_features_to_teams_groups_people(world):
    c, Session, ids, H, U = world
    ws = ids["ws"]
    feats = {f["key"]: f for f in c.get(f"/api/workspaces/{ws}/features", headers=H["boss"]
                                        ).json()}
    assert feats["delivery"]["mode"] == "everyone"
    assert c.get(f"/api/workspaces/{ws}/features", headers=H["alice"]).status_code == 403
    # Delivery only for the Payments team (Alice) and Bob personally; Ask off for everyone.
    c.put(f"/api/workspaces/{ws}/features/delivery", headers=H["boss"],
          json={"mode": "some", "teams": [ids["team"]]})
    c.put(f"/api/workspaces/{ws}/features/ask", headers=H["boss"], json={"mode": "off"})
    url = f"/api/projects/{ids['open']}/delivery"
    assert c.get(url, headers=H["alice"]).status_code == 200
    r = c.get(url, headers=H["bob"])
    assert r.status_code == 403 and "Delivery pipeline" in r.json()["detail"]
    assert c.get(url, headers=H["boss"]).status_code == 200  # admins always
    c.put(f"/api/workspaces/{ws}/features/delivery", headers=H["boss"],
          json={"mode": "some", "teams": [ids["team"]], "users": [U["bob"]]})
    assert c.get(url, headers=H["bob"]).status_code == 200
    assert c.post(f"/api/workspaces/{ws}/ask", headers=H["alice"],
                  json={"question": "x"}).status_code == 403
    # Directory/SSO group names work too.
    c.put(f"/api/workspaces/{ws}/features/memory", headers=H["boss"],
          json={"mode": "some", "groups": ["Eng-Leads"]})
    assert c.get(f"/api/workspaces/{ws}/memory", headers=H["bob"]).status_code == 403
    with Session() as s:
        s.get(User, U["bob"]).groups = ["eng-leads"]
        s.commit()
    assert c.get(f"/api/workspaces/{ws}/memory", headers=H["bob"]).status_code == 200
    me = c.get("/api/auth/me", headers=H["alice"]).json()
    mine = next(w for w in me["workspaces"] if w["id"] == ws)["features"]
    assert "delivery" in mine and "ask" not in mine and "memory" not in mine
    # Back to everyone.
    c.put(f"/api/workspaces/{ws}/features/ask", headers=H["boss"], json={"mode": "everyone"})
    assert c.post(f"/api/workspaces/{ws}/ask", headers=H["alice"],
                  json={"question": "x"}).status_code != 403
