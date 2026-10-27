import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.navigation import (
    ANSWER_CHECK_SCHEMA,
    SUFFICIENCY_SCHEMA,
    AnswerCheck,
    Sufficiency,
)

log = logging.getLogger("ai_chat.model")

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

    def __add__(self, other: "Usage | None") -> "Usage":
        if other is None:
            return self
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_tokens + other.cached_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    ranking: list[str]


@dataclass(frozen=True)
class StepResult:
    call: ToolCall | None
    text: str
    items: list[dict[str, Any]]
    usage: Usage | None


class ModelFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AnswerModel(Protocol):
    name: str

    async def step(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str,
        cache_key: str,
    ) -> StepResult: ...

    async def sufficiency(
        self, instructions: str, text: str
    ) -> tuple[Sufficiency, Usage | None]: ...

    async def check_answer(
        self, instructions: str, text: str
    ) -> tuple[AnswerCheck, Usage | None]: ...


class SummaryModel(Protocol):
    name: str

    async def summarize(
        self, instructions: str, text: str
    ) -> tuple[str, Usage | None]: ...


class CheckModel(Protocol):
    name: str

    async def find(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        name: str,
        schema: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], Usage | None]: ...

    async def judge(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        name: str,
        schema: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], Usage | None]: ...


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


async def create(client: AsyncOpenAI, **options: Any) -> Any:
    begun = time.monotonic()
    try:
        response = await client.responses.create(
            store=False, truncation="disabled", **options
        )
    except APITimeoutError:
        raise ModelFailure("MODEL_TIMEOUT") from None
    except APIConnectionError:
        raise ModelFailure("MODEL_CONNECTION_FAILED") from None
    except APIStatusError as error:
        raise ModelFailure(status_code(error)) from None
    if response.status == "incomplete":
        raise ModelFailure("MODEL_INCOMPLETE")
    if response.status == "failed":
        raise ModelFailure("MODEL_FAILED")
    usage = usage_of(response.usage)
    log.info(
        "model call",
        extra={
            "model": options.get("model"),
            "kind": "tool" if options.get("tools") else "text",
            "effort": (options.get("reasoning") or {}).get("effort"),
            "ms": int((time.monotonic() - begun) * 1000),
            "input_tokens": usage.input_tokens if usage else None,
            "cached_tokens": usage.cached_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "reasoning_tokens": usage.reasoning_tokens if usage else None,
        },
    )
    return response


def effort(value: str) -> dict[str, Any]:
    return {"reasoning": {"effort": value}} if value else {}


def parse_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        raise ModelFailure("MODEL_INVALID_JSON") from None
    if not isinstance(value, dict):
        raise ModelFailure("MODEL_INVALID_JSON")
    return value


async def structured(
    client: AsyncOpenAI,
    instructions: str,
    data: str | list[dict[str, Any]],
    name: str,
    schema: dict[str, Any],
    **options: Any,
) -> tuple[dict[str, Any], Usage | None]:
    response = await create(
        client,
        instructions=instructions,
        input=data,
        text={
            "format": {
                "type": "json_schema",
                "name": name,
                "strict": True,
                "schema": schema,
            }
        },
        **options,
    )
    return parse_json(response.output_text or ""), usage_of(response.usage)


def tool_call(item: Any) -> ToolCall:
    if item.name != "request_documents":
        raise ModelFailure("MODEL_INVALID_TOOL_CALL")
    arguments = parse_json(item.arguments or "{}")
    ranking = arguments.get("ranking")
    if not isinstance(ranking, list):
        raise ModelFailure("MODEL_INVALID_TOOL_CALL")
    return ToolCall(item.call_id, item.name, [str(entry) for entry in ranking])


class OpenAIModel:
    def __init__(
        self,
        client: AsyncOpenAI,
        name: str,
        reasoning_effort: str,
        max_output_tokens: int,
        timeout_seconds: float,
        judge_name: str,
        judge_reasoning_effort: str,
        judge_max_output_tokens: int,
    ):
        self.client = client
        self.name = name
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.judge_name = judge_name
        self.judge_reasoning_effort = judge_reasoning_effort
        self.judge_max_output_tokens = judge_max_output_tokens

    async def step(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str,
        cache_key: str,
    ) -> StepResult:
        response = await create(
            self.client,
            model=self.name,
            instructions=instructions,
            input=items,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=False,
            include=["reasoning.encrypted_content"],
            max_output_tokens=self.max_output_tokens,
            prompt_cache_key=cache_key,
            timeout=self.timeout_seconds,
            **effort(self.reasoning_effort),
        )
        call = None
        for item in response.output:
            if item.type == "function_call":
                call = tool_call(item)
                break
        output = [item.model_dump(exclude_none=True) for item in response.output]
        text = "" if call else (response.output_text or "")
        return StepResult(call, text, output, usage_of(response.usage))

    async def _judge(
        self, instructions: str, text: str, name: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], Usage | None]:
        return await structured(
            self.client,
            instructions,
            text,
            name,
            schema,
            model=self.judge_name,
            max_output_tokens=self.judge_max_output_tokens,
            timeout=self.timeout_seconds,
            **effort(self.judge_reasoning_effort),
        )

    async def sufficiency(
        self, instructions: str, text: str
    ) -> tuple[Sufficiency, Usage | None]:
        value, usage = await self._judge(
            instructions, text, "sufficiency", SUFFICIENCY_SCHEMA
        )
        return (
            Sufficiency(bool(value["sufficient"]), [str(m) for m in value["missing"]]),
            usage,
        )

    async def check_answer(
        self, instructions: str, text: str
    ) -> tuple[AnswerCheck, Usage | None]:
        value, usage = await self._judge(
            instructions, text, "answer_check", ANSWER_CHECK_SCHEMA
        )
        return (
            AnswerCheck(bool(value["answer_ok"]), [str(p) for p in value["problems"]]),
            usage,
        )


class OpenAISummaryModel:
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

    async def summarize(self, instructions: str, text: str) -> tuple[str, Usage | None]:
        response = await create(
            self.client,
            model=self.name,
            instructions=instructions,
            input=text,
            max_output_tokens=self.max_output_tokens,
            timeout=self.timeout_seconds,
            **effort(self.reasoning_effort),
        )
        summary = (response.output_text or "").strip()
        if not summary:
            raise ModelFailure("MODEL_EMPTY_RESPONSE")
        return summary, usage_of(response.usage)


class OpenAICheckModel:
    def __init__(
        self,
        client: AsyncOpenAI,
        name: str,
        reasoning_effort: str,
        max_output_tokens: int,
        judge_name: str,
        judge_reasoning_effort: str,
        judge_max_output_tokens: int,
        timeout_seconds: float,
    ):
        self.client = client
        self.name = name
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.judge_name = judge_name
        self.judge_reasoning_effort = judge_reasoning_effort
        self.judge_max_output_tokens = judge_max_output_tokens
        self.timeout_seconds = timeout_seconds

    async def find(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        name: str,
        schema: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], Usage | None]:
        return await structured(
            self.client,
            instructions,
            items,
            name,
            schema,
            model=self.name,
            max_output_tokens=self.max_output_tokens,
            prompt_cache_key=cache_key,
            timeout=self.timeout_seconds,
            **effort(self.reasoning_effort),
        )

    async def judge(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        name: str,
        schema: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], Usage | None]:
        return await structured(
            self.client,
            instructions,
            items,
            name,
            schema,
            model=self.judge_name,
            max_output_tokens=self.judge_max_output_tokens,
            prompt_cache_key=cache_key,
            timeout=self.timeout_seconds,
            **effort(self.judge_reasoning_effort),
        )


class UnconfiguredModel:
    def __init__(self, name: str):
        self.name = name

    async def find(self, *_: Any) -> tuple[dict[str, Any], Usage | None]:
        raise ModelFailure("MODEL_NOT_CONFIGURED")

    async def judge(self, *_: Any) -> tuple[dict[str, Any], Usage | None]:
        raise ModelFailure("MODEL_NOT_CONFIGURED")

    async def step(self, *_: Any, **__: Any) -> StepResult:
        raise ModelFailure("MODEL_NOT_CONFIGURED")

    async def sufficiency(self, *_: Any) -> tuple[Sufficiency, Usage | None]:
        raise ModelFailure("MODEL_NOT_CONFIGURED")

    async def check_answer(self, *_: Any) -> tuple[AnswerCheck, Usage | None]:
        raise ModelFailure("MODEL_NOT_CONFIGURED")
