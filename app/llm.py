"""LLM calls via the OpenAI Responses API. Model names come from config only (plan 4).

`FakeLLM` returns canned output so the whole app runs and tests pass without a key.
Every call reports token usage to the spend breaker (plan 10.6).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from app.settings import ROOT, get_settings

T = TypeVar("T", bound=BaseModel)


def prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")


class LLM(Protocol):
    def text(self, task: str, instructions: str, input: str | list[dict]) -> str: ...
    def json(self, task: str, instructions: str, input: str, model: type[T]) -> T: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LLMNotConfigured(RuntimeError):
    pass


class OpenAILLM:
    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        if not api_key:
            raise LLMNotConfigured("OPENAI_API_KEY is not set")
        self.client = OpenAI(api_key=api_key)

    def _model(self, task: str) -> str:
        return get_settings().cfg(f"models.{task}")

    def _opts(self, task: str) -> dict:
        """Per-task reasoning effort from config (reasoning models only)."""
        effort = get_settings().cfg(f"reasoning.{task}")
        return {"reasoning": {"effort": effort}} if effort else {}

    def _account(self, resp: Any) -> None:
        from app.security.caps import add_spend

        usage = getattr(resp, "usage", None)
        tokens = getattr(usage, "total_tokens", 0) or 0
        add_spend(tokens / 1000 * float(get_settings().cfg("caps.llm_usd_per_1k_tokens", 0.002)))

    def text(self, task: str, instructions: str, input: str | list[dict]) -> str:
        """`input` is one user message, or a list of {"role", "content"} turns."""
        resp = self.client.responses.create(
            model=self._model(task), instructions=instructions, input=input, **self._opts(task)
        )
        self._account(resp)
        return resp.output_text.strip()

    def json(self, task: str, instructions: str, input: str, model: type[T]) -> T:
        resp = self.client.responses.parse(
            model=self._model(task), instructions=instructions, input=input, text_format=model,
            **self._opts(task),
        )
        self._account(resp)
        parsed = resp.output_parsed
        if parsed is None:
            raise ValueError("model returned no parsed output")
        return parsed

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self.client.embeddings.create(model=self._model("embedding"), input=texts)
        return [d.embedding for d in resp.data]


class FakeLLM:
    """Deterministic stand-in. Tests can register handlers per task."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[str, str], Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def on(self, task: str, fn: Callable[[str, str], Any]) -> None:
        self.handlers[task] = fn

    def text(self, task: str, instructions: str, input: str | list[dict]) -> str:
        if isinstance(input, list):  # earlier turns, then the question last
            input = "\n".join(m["content"] for m in input)
        self.calls.append((task, input))
        if task in self.handlers:
            return self.handlers[task](instructions, input)
        return "Hmm. Tell me more about that?"

    def json(self, task: str, instructions: str, input: str, model: type[T]) -> T:
        self.calls.append((task, input))
        if task in self.handlers:
            out = self.handlers[task](instructions, input)
            return out if isinstance(out, model) else model.model_validate(out)
        from app.fakes import fake_json

        return model.model_validate(fake_json(task, input))

    def embed(self, texts: list[str]) -> list[list[float]]:
        dim = int(get_settings().cfg("models.embedding_dim", 1536))
        out = []
        for t in texts:
            v = [0.0] * dim
            for i, ch in enumerate(t.lower()[:400]):
                v[(ord(ch) * 31 + i) % dim] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


_llm: LLM | None = None


def llm() -> LLM:
    global _llm
    if _llm is None:
        s = get_settings()
        _llm = FakeLLM() if s.llm_backend == "fake" else OpenAILLM(s.openai_api_key)
    return _llm


def set_llm(x: LLM | None) -> None:
    global _llm
    _llm = x


class Timer:
    def __enter__(self) -> Timer:
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.ms = int((time.perf_counter() - self.t0) * 1000)
