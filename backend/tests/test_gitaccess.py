"""Git access: tokens only reach configured hosts, git never prompts, failures say what to do
(never a bare 500), GitHub Enterprise hosts work for the GitHub API."""

import subprocess

import pytest

from filament import gitaccess
from filament.config import get_settings
from filament.delivery import github
from filament.readiness import scanner


@pytest.fixture
def ghe(monkeypatch):
    monkeypatch.setattr(get_settings(), "github_hosts", ["github.com", "github.acme.internal"])
    monkeypatch.setenv("FILAMENT_GIT_TOKEN", "tok123")


def test_token_only_for_configured_hosts(ghe):
    assert gitaccess.authed("https://github.com/acme/api") == \
        "https://x-access-token:tok123@github.com/acme/api"
    assert "tok123" in gitaccess.authed("https://github.acme.internal/acme/api")
    assert gitaccess.authed("https://gitlab.example.com/acme/api") == \
        "https://gitlab.example.com/acme/api"
    assert gitaccess.authed("git@github.com:acme/api.git") == "git@github.com:acme/api.git"
    assert gitaccess.authed("https://me@github.com/acme/api") == "https://me@github.com/acme/api"
    assert gitaccess.clone_url("acme/api") == "https://github.com/acme/api"
    assert gitaccess.clone_url("github.acme.internal/acme/api") == \
        "https://github.acme.internal/acme/api"


def test_git_never_prompts():
    env = gitaccess.env()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_errors_explain_what_to_do(ghe):
    msg = gitaccess.explain("https://github.acme.internal/acme/api",
                            "fatal: could not read Username: terminal prompts disabled tok123")
    assert "FILAMENT_GIT_TOKEN" in msg and "tok123" not in msg
    other = gitaccess.explain("https://git.other.corp/acme/api", "Authentication failed")
    assert "FILAMENT_GITHUB_HOSTS" in other  # token isn't sent there yet
    assert "single sign-on" in gitaccess.explain("acme/api", "remote: ... SAML SSO enforced")
    assert "ssh -T git@github.com" in gitaccess.explain("git@github.com:acme/api.git",
                                                        "Host key verification failed.")
    assert "HTTPS_PROXY" in gitaccess.explain("acme/api", "Could not resolve host: github.com")


def _onboard(client, ws, repo):
    return client.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": repo})


def test_onboard_failures_are_readable(client, ws, monkeypatch, tmp_path):
    def fail(exc):
        def run(*a, **kw):
            raise exc
        return run

    monkeypatch.setattr(scanner.subprocess, "run", fail(subprocess.CalledProcessError(
        128, "git", stderr=b"fatal: could not read Username for 'https://github.com': "
                           b"terminal prompts disabled")))
    r = _onboard(client, ws, "acme/private-api")
    assert r.status_code == 400 and "FILAMENT_GIT_TOKEN" in r.json()["detail"]

    monkeypatch.setattr(scanner.subprocess, "run", fail(subprocess.TimeoutExpired("git", 600)))
    r = _onboard(client, ws, "acme/huge-monorepo")
    assert r.status_code == 400 and "local folder" in r.json()["detail"]

    monkeypatch.setattr(scanner.subprocess, "run", fail(FileNotFoundError("git")))
    assert "git is not installed" in _onboard(client, ws, "acme/x").json()["detail"]

    monkeypatch.undo()
    import filament.api.repos as repos_api

    def boom(ref, spec_url=None):
        raise RuntimeError("unexpected layout")
    monkeypatch.setattr(repos_api, "analyze", boom)
    r = _onboard(client, ws, str(tmp_path))
    assert r.status_code == 500
    assert "RuntimeError: unexpected layout" in r.json()["detail"]


def test_clone_over_git_with_no_prompt_env(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], check=True)
    with scanner.checkout(f"file://{src}") as root:
        assert (root / "app.py").exists()


def test_github_enterprise_api(ghe, monkeypatch):
    seen = []

    class R:
        status_code = 200

        def json(self):
            return [] if "issues" in seen[-1] else {"check_runs": []}

    monkeypatch.setattr("httpx.get", lambda url, **kw: seen.append(url) or R())
    github.issues("github.acme.internal/acme/api")
    assert seen[-1] == "https://github.acme.internal/api/v3/repos/acme/api/issues"
    github.issues("acme/api")
    assert seen[-1] == "https://api.github.com/repos/acme/api/issues"
    assert github.parse_pr("https://github.acme.internal/acme/api/pull/7") == \
        ("github.acme.internal", "acme", "api", 7)
    with pytest.raises(github.GitHubError, match="FILAMENT_GITHUB_HOSTS"):
        github.parse_pr("https://github.evil.example/acme/api/pull/7")
