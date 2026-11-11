"""How Ignite reaches git hosts: credentials, GitHub Enterprise hosts, no prompts, clear errors.

- The token (IGNITE_GIT_TOKEN or GITHUB_TOKEN) is only ever read from the environment and is
  only sent to the hosts in IGNITE_GITHUB_HOSTS (github.com by default; add your GitHub
  Enterprise host, e.g. IGNITE_GITHUB_HOSTS='["github.com","github.acme.internal"]').
- git never prompts: a missing credential fails at once with a reason instead of hanging until
  the timeout. SSH URLs use your keys and agent, in batch mode.
- Errors say what to do (token, SSO authorization, SSH key, host key, timeout).
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from igniteai.config import get_settings


def token() -> str | None:
    return os.environ.get("IGNITE_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN")


def hosts() -> list[str]:
    return [h.lower().strip() for h in get_settings().github_hosts if h.strip()]


def clone_url(repo: str) -> str:
    """A URL git can clone: owner/repo shorthands become https URLs on the first host."""
    if "://" in repo or repo.startswith("git@"):
        return repo
    first = repo.split("/", 1)[0].lower()
    if "." in first:  # host/owner/repo
        return f"https://{repo}"
    return f"https://{hosts()[0]}/{repo}"


def host_of(url: str) -> str:
    if url.startswith("git@"):
        return url[4:].split(":", 1)[0].lower()
    return (urlparse(url).hostname or "").lower()


def authed(url: str) -> str:
    """Add the token to https URLs on a configured GitHub host; leave everything else alone."""
    t = token()
    has_user = "@" in url.split("/")[2] if url.startswith("https://") else True
    if t and not has_user and host_of(url) in hosts():
        return url.replace("https://", f"https://x-access-token:{t}@", 1)
    return url


def env() -> dict:
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"  # fail instead of waiting for a username/password
    e["GCM_INTERACTIVE"] = "never"  # Git Credential Manager: no pop-up
    e.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # no passphrase/host-key prompts
    return e


def api_base(host: str) -> str:
    return "https://api.github.com" if host in {"github.com", "www.github.com"} else \
        f"https://{host}/api/v3"


def scrub(text: str) -> str:
    t = token()
    return text.replace(t, "***") if t else text


HINTS = [
    (r"terminal prompts disabled|could not read Username|Authentication failed|"
     r"Invalid username or password|returned error: 40[13]",
     "The host asked for credentials. Set IGNITE_GIT_TOKEN (a token that can read this "
     "repo){host}, or use an SSH URL like git@{h}:org/repo.git, or point Ignite at a "
     "local clone."),
    (r"SAML|SSO|organization has enabled or enforced",
     "Your organization uses single sign-on: authorize the token for the org on GitHub "
     "(Settings → Tokens → Configure SSO)."),
    (r"Repository not found|not found|404",
     "The repo wasn't found with these credentials. Check the URL, and that the token (or your "
     "SSH key) can see private repos in this org{host}."),
    (r"Permission denied \(publickey\)",
     "SSH refused your key. Load it with ssh-add, or check it's added to your GitHub account."),
    (r"Host key verification failed",
     "SSH doesn't know this host yet. Run `ssh -T git@{h}` once in a terminal to trust it."),
    (r"Could not resolve host|Connection timed out|Failed to connect|proxy",
     "Couldn't reach {h}. On an office network, set HTTPS_PROXY (and NO_PROXY for internal "
     "hosts if they bypass the proxy)."),
    (r"SSL certificate problem|self[- ]signed certificate|unable to get local issuer",
     "TLS inspection on your network: point git at your company's root certificate "
     "(git config --global http.sslCAInfo /path/to/company-ca.pem)."),
]


def explain(repo: str, stderr: str) -> str:
    """A short reason plus what to do, for a failed git command."""
    url = clone_url(repo)
    h = host_of(url) or "your git host"
    host_note = "" if h in hosts() else (
        f", and add {h} to IGNITE_GITHUB_HOSTS so the token is used there")
    clean = scrub(stderr).strip().splitlines()
    detail = next((ln for ln in reversed(clean) if ln.strip()), "")[:200]
    for rx, hint in HINTS:
        if re.search(rx, stderr, re.I):
            return f"Could not clone {repo}: {detail}. {hint.format(h=h, host=host_note)}"
    return f"Could not clone {repo}: {detail or 'git failed'}"
