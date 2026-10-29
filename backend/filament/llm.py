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


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ModelTurn:
    """One assistant turn in a tool-use loop."""

    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    model: str
    input_tokens: int
    output_tokens: int
    offline: bool = False

    def assistant_content(self) -> list[dict]:
        """The turn as Messages API content blocks, to append to the conversation."""
        blocks: list[dict] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for c in self.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
        return blocks

    def cost_usd(self, settings: Settings | None = None) -> float | None:
        return LLMResult(self.text, self.model, self.input_tokens, self.output_tokens).cost_usd(
            settings
        )


# An offline policy decides the next turn from the conversation so far (used without an API
# key, and in tests). It must return a ModelTurn with tool calls, or no tool calls to stop.
OfflinePolicy = Callable[[list[dict]], ModelTurn]


def chat(
    system: str,
    messages: list[dict],
    tools: list[dict],
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    offline_policy: OfflinePolicy | None = None,
) -> ModelTurn:
    """One step of a tool-use conversation with the Messages API."""
    settings = get_settings()
    model = model or settings.model_default

    if settings.offline:
        if offline_policy is None:
            return ModelTurn("(offline)", [], "end_turn", f"{model} (offline)", 0, 0, True)
        turn = offline_policy(messages)
        turn.model = f"{model} (offline)"
        turn.offline = True
        turn.input_tokens = approx_tokens(system + str(messages)[-20_000:])
        turn.output_tokens = approx_tokens(str(turn.assistant_content()))
        return turn

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens or settings.max_output_tokens,
        system=system,
        tools=tools,
        messages=messages,
    )
    text = "".join(b.text for b in message.content if b.type == "text")
    calls = [ToolCall(b.id, b.name, dict(b.input)) for b in message.content if b.type == "tool_use"]
    return ModelTurn(
        text=text,
        tool_calls=calls,
        stop_reason=message.stop_reason or "end_turn",
        model=model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
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
