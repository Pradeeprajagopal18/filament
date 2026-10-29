"""Thin wrapper over the Anthropic Messages API with token and cost accounting.

Calls the API directly (no agent framework). When no ANTHROPIC_API_KEY is set, callers
supply an offline response so the platform stays usable for demos and tests.
"""

from collections.abc import Callable
from dataclasses import dataclass

from filament.config import Settings, get_settings


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    offline: bool = False

    def cost_usd(self, settings: Settings | None = None) -> float | None:
        prices = (settings or get_settings()).pricing.get(self.model)
        if not prices:
            return None
        return round(
            self.input_tokens / 1e6 * prices.get("input", 0)
            + self.output_tokens / 1e6 * prices.get("output", 0),
            6,
        )


def approx_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token) for budgeting and offline accounting."""
    return max(1, len(text) // 4)


def complete(
    system: str,
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    offline_response: Callable[[], str] | None = None,
) -> LLMResult:
    settings = get_settings()
    model = model or settings.model_default

    if settings.offline:
        text = offline_response() if offline_response else "(offline: no ANTHROPIC_API_KEY set)"
        return LLMResult(
            text=text,
            model=f"{model} (offline)",
            input_tokens=approx_tokens(system + prompt),
            output_tokens=approx_tokens(text),
            offline=True,
        )

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens or settings.max_output_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in message.content if block.type == "text")
    return LLMResult(
        text=text,
        model=model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )
