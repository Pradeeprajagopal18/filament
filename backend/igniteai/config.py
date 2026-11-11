"""Runtime configuration, read from environment variables (prefix IGNITE_)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IGNITE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./igniteai.db"

    # Model routing: a large model for design/requirements work, a small one for routine steps.
    model_default: str = "claude-sonnet-5-5"
    model_small: str = "claude-haiku-5-5"
    max_output_tokens: int = 4096

    # Anthropic key is read from the standard ANTHROPIC_API_KEY variable (no prefix).
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # Optional pricing in USD per million tokens, e.g.
    # IGNITE_PRICING='{"claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}'
    # When a model has no entry, cost is left empty rather than guessed.
    pricing: dict[str, dict[str, float]] = {}

    # Agent loop guardrails: hard limits the model cannot override.
    max_agent_turns: int = 12
    # Access control. "none": no sign-in (a single local user who can do everything; for a
    # laptop). "oidc": everyone signs in with the company SSO and sees only what they may.
    #   "login" (default): everyone signs in, with any of auth_methods.
    #   "none": no sign-in, a single local user who can do everything (a laptop, tests).
    auth_mode: str = "login"
    # Any mix of "local" (accounts kept in Ignite), "ldap" (company directory) and "oidc" (SSO).
    auth_methods: list[str] = ["local"]
    # First sign-in on a new install: admin / admin, which must be changed straight away.
    bootstrap_admin_password: str = Field(default="admin", repr=False)
    password_min_length: int = 12
    max_failed_logins: int = 5               # then the account is locked for lockout_minutes
    lockout_minutes: int = 15
    # LDAP / Active Directory.
    ldap_url: str | None = None              # ldaps://ldap.acme.com:636
    ldap_start_tls: bool = False             # for ldap:// URLs
    ldap_ca_file: str | None = None          # CA bundle for a private certificate authority
    ldap_bind_dn: str | None = None          # service account used to look people up
    ldap_bind_password: str | None = Field(default=None, repr=False)
    ldap_base_dn: str | None = None          # dc=acme,dc=com
    ldap_user_filter: str = "(|(uid={username})(sAMAccountName={username})(mail={username}))"
    ldap_email_attr: str = "mail"
    ldap_name_attr: str = "displayName"
    ldap_group_attr: str = "memberOf"
    ldap_github_attr: str | None = None      # an attribute holding the GitHub username, if any
    ldap_timeout_s: int = 10
    oidc_issuer: str | None = None          # e.g. https://acme.okta.com/oauth2/default
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, repr=False)
    oidc_redirect_url: str | None = None    # default: <ui_url>/api/auth/callback
    oidc_scopes: str = "openid email profile groups"
    oidc_groups_claim: str = "groups"
    oidc_github_claim: str | None = None    # a claim holding the user's GitHub username, if any
    # Signs session cookies. If unset, a random one is created in data_dir and reused.
    session_secret: str | None = Field(default=None, repr=False)
    session_hours: int = 12
    admin_emails: list[str] = []            # first admins (bootstrap)
    # "member": everyone who signs in joins every workspace; "none": only people added by an admin.
    workspace_default_role: str = "member"
    ui_url: str = "http://localhost:5173"   # where to send people after signing in
    # Git hosts that get the IGNITE_GIT_TOKEN: github.com plus any GitHub Enterprise hosts,
    # e.g. IGNITE_GITHUB_HOSTS='["github.com","github.acme.internal"]'. Other hosts never see it.
    github_hosts: list[str] = ["github.com"]
    # Shallow clones of big internal repos can take a while.
    clone_timeout_s: int = 600
    # Delivery: how many times QA sends failing CI back to the coding agent before a person
    # steps in, and how often SLOs are checked by the scheduler.
    delivery_max_retries: int = 3
    slo_check_interval_minutes: int = 5
    run_token_budget: int = 200_000

    # Context control: keep each model call lean and grounded.
    tool_result_max_chars: int = 6_000  # per tool result returned to the model
    compaction_threshold_tokens: int = 30_000  # compact the conversation above this size
    keep_recent_tool_turns: int = 2  # tool results kept verbatim during compaction

    # Context engine budget (approximate tokens) for each run's context pack.
    context_budget_tokens: int = 12_000

    cors_origins: list[str] = ["http://localhost:5173"]

    # Launchpad: keep repo references current. Repos are rescanned on this interval (hours,
    # 0 disables) by the in-process scheduler, which checks for due repos every tick.
    repo_scan_interval_hours: int = 24
    repo_scheduler_enabled: bool = True
    repo_scheduler_tick_seconds: int = 300
    # Where Ignite keeps per-repo working data (e.g. the code graph's per-file cache).
    data_dir: str = "./.ignite-data"
    # Build a symbol-level code graph (tree-sitter, no LLM) during repo scans.
    code_graph: bool = True

    # MCP gateway: above this many enabled tools, agents get progressive discovery (discover,
    # list, schema, invoke) instead of every tool definition, to keep context small.
    mcp_discovery_threshold: int = 15
    mcp_sync_interval_hours: int = 24
    # Verifies GitHub push webhooks (X-Hub-Signature-256) when set.
    github_webhook_secret: str | None = None

    @property
    def offline(self) -> bool:
        """True when no API key is configured: agents use a deterministic local stub."""
        return not self.anthropic_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
