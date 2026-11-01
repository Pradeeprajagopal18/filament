"""Runtime configuration, read from environment variables (prefix FILAMENT_)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FILAMENT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./filament.db"

    # Model routing: a large model for design/requirements work, a small one for routine steps.
    model_default: str = "claude-sonnet-5-5"
    model_small: str = "claude-haiku-5-5"
    max_output_tokens: int = 4096

    # Anthropic key is read from the standard ANTHROPIC_API_KEY variable (no prefix).
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # Optional pricing in USD per million tokens, e.g.
    # FILAMENT_PRICING='{"claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}'
    # When a model has no entry, cost is left empty rather than guessed.
    pricing: dict[str, dict[str, float]] = {}

    # Agent loop guardrails: hard limits the model cannot override.
    max_agent_turns: int = 12
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
    # Verifies GitHub push webhooks (X-Hub-Signature-256) when set.
    github_webhook_secret: str | None = None

    @property
    def offline(self) -> bool:
        """True when no API key is configured: agents use a deterministic local stub."""
        return not self.anthropic_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
