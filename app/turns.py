import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from app.citations import CitationStream, Cited
from app.content import ContentSource
from app.errors import CONTRACTS, ChatFailure
from app.model import AnswerModel, Finished, ModelFailure, TextDelta, Usage
from app.prompt import Profile, Question, build_prompt
from app.settings import Settings
from app.store import Metrics, Started, Store, message_json

log = logging.getLogger("ai_chat.turns")

TERMINAL = {"done", "error"}
CANCELLED_EVENT = {"code": "CHAT_ANSWER_CANCELLED", "message": "The answer was stopped"}


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]

    def encode(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.name}\ndata: {payload}\n\n".encode()


class Channel:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue()

    def put(self, name: str, data: dict[str, Any]) -> None:
        self.queue.put_nowait(Event(name, data))

    async def events(self, heartbeat: float) -> AsyncIterator[bytes]:
        while True:
            try:
                event = await asyncio.wait_for(self.queue.get(), heartbeat)
            except TimeoutError:
                yield b": ping\n\n"
                continue
            yield event.encode()
            if event.name in TERMINAL:
                return


class Stopped(Exception):
    pass


def error_event(code: str) -> dict[str, str]:
    return {"code": code, "message": CONTRACTS[code].message}


def with_usage(metrics: Metrics, usage: Usage | None) -> Metrics:
    if usage is None:
        return metrics
    return replace(
        metrics,
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )


class TurnRunner:
    def __init__(
        self,
        store: Store,
        content: ContentSource,
        model: AnswerModel,
        settings: Settings,
    ):
        self.store = store
        self.content = content
        self.model = model
        self.settings = settings
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.stopping = False

    def start(
        self,
        started: Started,
        user_id: UUID,
        profile: Profile,
        question: Question,
    ) -> Channel:
        channel = Channel()
        turn_id = started.turn_id
        task = asyncio.create_task(
            self.run(started, user_id, profile, question, channel)
        )
        self.tasks[turn_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(turn_id, None))
        return channel

    def stop(self, turn_ids: list[UUID]) -> None:
        for turn_id in turn_ids:
            task = self.tasks.get(turn_id)
            if task:
                task.cancel()

    async def drain(self) -> None:
        self.stopping = True
        running = list(self.tasks.values())
        if not running:
            return
        _, pending = await asyncio.wait(
            running, timeout=self.settings.shutdown_grace_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)

    async def watch(self, turn_id: UUID, main: asyncio.Task[None]) -> None:
        while True:
            await asyncio.sleep(self.settings.cancel_poll_seconds)
            try:
                status = await self.store.turn_status(turn_id)
            except Exception:
                log.warning("turn status check failed", extra={"turn_id": str(turn_id)})
                continue
            if status != "GENERATING":
                main.cancel()
                return

    async def run(
        self,
        started: Started,
        user_id: UUID,
        profile: Profile,
        question: Question,
        channel: Channel,
    ) -> None:
        turn_id = started.turn_id
        session_id = started.session["id"]
        project_id = started.session["project_id"]
        begun = time.monotonic()
        metrics = Metrics(model=self.model.name)
        watcher = asyncio.create_task(
            self.watch(turn_id, asyncio.current_task())  # type: ignore[arg-type]
        )
        outcome = "complete"
        try:
            channel.put("phase", {"phase": "gathering"})
            snapshot = await self.content.snapshot(user_id, project_id)
            metrics = replace(
                metrics,
                context_chars=snapshot.size,
                snapshot_ms=int((time.monotonic() - begun) * 1000),
            )
            if snapshot.size > self.settings.context_limit:
                raise ChatFailure("CHAT_CONTEXT_TOO_LARGE")
            history = await self.store.history(session_id, started.question["id"])
            prompt = build_prompt(
                snapshot,
                profile,
                history,
                question,
                recent_count=self.settings.recent_manuscripts,
                history_budget=max(
                    self.settings.history_floor,
                    self.settings.context_limit - snapshot.size,
                ),
            )
            channel.put("phase", {"phase": "reading"})
            parser = CitationStream(prompt.materials)
            usage: Usage | None = None
            async with asyncio.timeout(self.settings.turn_deadline_seconds):
                async for event in self.model.stream(prompt, str(project_id)):
                    if isinstance(event, Finished):
                        usage = event.usage
                        continue
                    if not isinstance(event, TextDelta) or not event.text:
                        continue
                    if metrics.first_token_ms is None:
                        metrics = replace(
                            metrics,
                            first_token_ms=int((time.monotonic() - begun) * 1000),
                        )
                        channel.put("phase", {"phase": "writing"})
                    self.forward(parser.feed(event.text), channel)
            self.forward(parser.finish(), channel)
            metrics = with_usage(metrics, usage)
            if not parser.content:
                raise ModelFailure("MODEL_EMPTY_RESPONSE")
            metrics = replace(metrics, total_ms=int((time.monotonic() - begun) * 1000))
            watcher.cancel()
            answer = await self.store.complete_turn(
                turn_id, session_id, parser.content, parser.citations, metrics
            )
            if answer is None:
                raise Stopped()
            channel.put(
                "done",
                {
                    "question": message_json(started.question),
                    "answer": message_json(answer),
                },
            )
        except (asyncio.CancelledError, Stopped):
            outcome = "stopped"
            await self.settle_stop(turn_id, metrics, begun)
            channel.put("error", CANCELLED_EVENT)
        except ChatFailure as failure:
            outcome = failure.code
            await self.settle_failure(turn_id, failure.code, metrics, begun)
            channel.put("error", error_event(failure.code))
        except ModelFailure as failure:
            outcome = failure.code
            await self.settle_failure(turn_id, failure.code, metrics, begun)
            channel.put("error", error_event("CHAT_MODEL_UNAVAILABLE"))
        except TimeoutError:
            outcome = "MODEL_DEADLINE"
            await self.settle_failure(turn_id, "MODEL_DEADLINE", metrics, begun)
            channel.put("error", error_event("CHAT_MODEL_UNAVAILABLE"))
        except Exception:
            outcome = "INTERNAL"
            log.exception("turn failed", extra={"turn_id": str(turn_id)})
            await self.settle_failure(turn_id, "INTERNAL", metrics, begun)
            channel.put("error", error_event("INTERNAL_ERROR"))
        finally:
            watcher.cancel()
            log.info(
                "turn finished",
                extra={
                    "turn_id": str(turn_id),
                    "session_id": str(session_id),
                    "outcome": outcome,
                    "model": metrics.model,
                    "context_chars": metrics.context_chars,
                    "input_tokens": metrics.input_tokens,
                    "cached_tokens": metrics.cached_tokens,
                    "output_tokens": metrics.output_tokens,
                    "reasoning_tokens": metrics.reasoning_tokens,
                    "snapshot_ms": metrics.snapshot_ms,
                    "first_token_ms": metrics.first_token_ms,
                    "total_ms": int((time.monotonic() - begun) * 1000),
                },
            )

    def forward(self, items: list[Any], channel: Channel) -> None:
        for item in items:
            if isinstance(item, Cited):
                channel.put(
                    "citation", {"index": item.index, "citation": item.citation}
                )
            else:
                channel.put("delta", {"text": item.text})

    async def settle_stop(self, turn_id: UUID, metrics: Metrics, begun: float) -> None:
        metrics = replace(metrics, total_ms=int((time.monotonic() - begun) * 1000))
        try:
            if self.stopping:
                await self.store.fail_turn(turn_id, "SHUTDOWN", metrics)
            else:
                await self.store.stop_turn(turn_id, metrics)
        except Exception:
            log.warning(
                "could not record stopped turn", extra={"turn_id": str(turn_id)}
            )

    async def settle_failure(
        self, turn_id: UUID, code: str, metrics: Metrics, begun: float
    ) -> None:
        metrics = replace(metrics, total_ms=int((time.monotonic() - begun) * 1000))
        try:
            await self.store.fail_turn(turn_id, code, metrics)
        except Exception:
            log.warning("could not record failed turn", extra={"turn_id": str(turn_id)})
