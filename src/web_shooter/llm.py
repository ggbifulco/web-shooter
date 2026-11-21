"""Thin LLM layer: one method that returns a validated Pydantic object.

`AnthropicLLM` is the real implementation; `ScriptedLLM` replays canned answers so
every LLM-driven step can be tested offline.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output), used for the cost column of the benchmark.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def cost_usd(self, model: str) -> float:
        pin, pout = PRICES.get(model, (0.0, 0.0))
        return (self.input_tokens * pin + self.output_tokens * pout) / 1_000_000


class LLM(Protocol):
    model: str
    usage: Usage

    def generate(self, *, system: str, prompt: str, output: type[T]) -> T: ...


class AnthropicLLM:
    """Claude via the official SDK, with structured outputs parsed into Pydantic models."""

    def __init__(self, model: str | None = None, max_tokens: int = 16000):
        import anthropic

        self.model = model or os.environ.get("WEB_SHOOTER_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.usage = Usage()
        self._client = anthropic.Anthropic()

    def generate(self, *, system: str, prompt: str, output: type[T]) -> T:
        response = self._client.beta.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=output,
            # Server-side fallback: if a safety classifier declines, the API retries on a
            # suitable model instead of returning a refusal.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        self.usage.add(response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            raise LLMError("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("output truncated at max_tokens")
        if response.parsed_output is None:
            raise LLMError("no structured output in response")
        return response.parsed_output


@dataclass
class ScriptedLLM:
    """Test double: answers come from a list (or a function of the prompt)."""

    answers: list[Any] = field(default_factory=list)
    responder: Callable[[str, str, type[BaseModel]], Any] | None = None
    model: str = "scripted"
    usage: Usage = field(default_factory=Usage)
    prompts: list[str] = field(default_factory=list)

    def generate(self, *, system: str, prompt: str, output: type[T]) -> T:
        self.prompts.append(prompt)
        self.usage.add(len(system + prompt) // 4, 50)
        if self.responder is not None:
            answer = self.responder(system, prompt, output)
        elif self.answers:
            answer = self.answers.pop(0)
        else:
            raise LLMError("ScriptedLLM ran out of answers")
        if isinstance(answer, output):
            return answer
        return output.model_validate(answer)


def default_llm(model: str | None = None) -> LLM | None:
    """Return a real LLM if credentials are configured, else None."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    return AnthropicLLM(model=model)
