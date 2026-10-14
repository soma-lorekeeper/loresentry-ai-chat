from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.prompt import Prompt

TEXT_EVENTS = {"response.output_text.delta", "response.refusal.delta"}
QUOTA_CODES = {
    "insufficient_quota",
    "billing_hard_limit_reached",
    "usage_limit_reached",
}


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Finished:
    usage: Usage | None


class ModelFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AnswerModel(Protocol):
    name: str

    def stream(
        self, prompt: Prompt, cache_key: str
    ) -> AsyncIterator[TextDelta | Finished]: ...


def status_code(error: APIStatusError) -> str:
    body = error.body if isinstance(error.body, dict) else {}
    body = body.get("error", body)
    code = body.get("code") if isinstance(body, dict) else None
    if code in QUOTA_CODES:
        return "MODEL_QUOTA_EXHAUSTED"
    if error.status_code in {401, 403}:
        return "MODEL_AUTH_FAILED"
    if error.status_code == 429:
        return "MODEL_RATE_LIMITED"
    if error.status_code >= 500 or error.status_code in {408, 409}:
        return "MODEL_UNAVAILABLE"
    return "MODEL_REQUEST_INVALID"


def usage_of(usage: Any) -> Usage | None:
    if usage is None:
        return None
    inputs = getattr(usage, "input_tokens_details", None)
    outputs = getattr(usage, "output_tokens_details", None)
    return Usage(
        input_tokens=usage.input_tokens,
        cached_tokens=getattr(inputs, "cached_tokens", 0) or 0,
        cache_write_tokens=getattr(inputs, "cache_write_tokens", 0) or 0,
        output_tokens=usage.output_tokens,
        reasoning_tokens=getattr(outputs, "reasoning_tokens", 0) or 0,
    )


class OpenAIModel:
    def __init__(
        self,
        client: AsyncOpenAI,
        name: str,
        reasoning_effort: str,
        max_output_tokens: int,
        timeout_seconds: float,
    ):
        self.client = client
        self.name = name
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds

    async def stream(
        self, prompt: Prompt, cache_key: str
    ) -> AsyncIterator[TextDelta | Finished]:
        options: dict[str, Any] = {}
        if self.reasoning_effort:
            options["reasoning"] = {"effort": self.reasoning_effort}
        try:
            events = await self.client.responses.create(
                model=self.name,
                instructions=prompt.instructions,
                input=prompt.input,
                max_output_tokens=self.max_output_tokens,
                prompt_cache_key=cache_key,
                store=False,
                truncation="disabled",
                stream=True,
                timeout=self.timeout_seconds,
                **options,
            )
            async for event in events:
                if event.type in TEXT_EVENTS:
                    yield TextDelta(event.delta)
                elif event.type == "response.completed":
                    yield Finished(usage_of(event.response.usage))
                elif event.type == "response.incomplete":
                    raise ModelFailure("MODEL_INCOMPLETE")
                elif event.type == "response.failed":
                    raise ModelFailure("MODEL_FAILED")
                elif event.type == "error":
                    raise ModelFailure("MODEL_STREAM_ERROR")
        except APITimeoutError:
            raise ModelFailure("MODEL_TIMEOUT") from None
        except APIConnectionError:
            raise ModelFailure("MODEL_CONNECTION_FAILED") from None
        except APIStatusError as error:
            raise ModelFailure(status_code(error)) from None


class UnconfiguredModel:
    def __init__(self, name: str):
        self.name = name

    async def stream(
        self, prompt: Prompt, cache_key: str
    ) -> AsyncIterator[TextDelta | Finished]:
        raise ModelFailure("MODEL_NOT_CONFIGURED")
        yield
