import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from app.agent import Agent, Outcome
from app.citations import CitationStream, Cited
from app.content import ContentSource, Document, Memo
from app.errors import CONTRACTS, ChatFailure
from app.logs import AppLogger, LogField, LogFlow, skip_entry_log
from app.model import AnswerModel, ModelFailure, Usage
from app.navigation import Turn
from app.prompt import Profile, Question, select_history
from app.settings import Settings
from app.store import Metrics, Started, Store, message_json
from app.usage import usage_json

# 답변 하나를 백그라운드 task로 돌리는 service. 모듈 이름으로 레이어를 정할 수 없어 직접 둔다
LOG_LAYER = "service"

TERMINAL = {"done", "error"}
CHUNK = 60
CANCELLED_EVENT = {"code": "CHAT_ANSWER_CANCELLED", "message": "The answer was stopped"}
# chat turn 종료 줄의 loresentry.result.outcome과 grounding 값(목록 밖은 other)
TURN_OUTCOMES = {"COMPLETE", "CANCELLED", "FAILED"}
GROUNDING = {"SUPPORTED", "NOT_ENOUGH"}


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]

    @skip_entry_log("Encodes one stream event; called for every delta")
    def encode(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.name}\ndata: {payload}\n\n".encode()


class Channel:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue()

    @skip_entry_log("Queues one stream event; called for every delta")
    def put(self, name: str, data: dict[str, Any]) -> None:
        self.queue.put_nowait(Event(name, data))

    @skip_entry_log("The stream body; the request lines cover it")
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


@skip_entry_log("Only builds a stream event")
def error_event(code: str) -> dict[str, str]:
    return {"code": code, "message": CONTRACTS[code].message}


@skip_entry_log("Only copies token counts")
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


@skip_entry_log("Only copies the agent outcome")
def finished(metrics: Metrics, outcome: Outcome) -> Metrics:
    return replace(
        with_usage(metrics, outcome.usage),
        steps=outcome.steps,
        judge_calls=outcome.judge_calls,
        grounding=outcome.level,
        rejections=len(outcome.reviews),
        reviews=[review.as_json() for review in outcome.reviews],
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
        self.agent = Agent(model, content, settings)
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.stopping = False

    @skip_entry_log("Creates the turn task, whose chat turn start line comes next")
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
        AppLogger.current().info(
            "chat turns stop", LogField.param("turn_ids", turn_ids)
        )
        for turn_id in turn_ids:
            task = self.tasks.get(turn_id)
            if task:
                task.cancel()

    async def drain(self) -> None:
        self.stopping = True
        running = list(self.tasks.values())
        AppLogger.current().info("chat turns drain", LogField.param("running", running))
        if not running:
            return
        _, pending = await asyncio.wait(
            running, timeout=self.settings.shutdown_grace_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)

    @skip_entry_log("Polls the turn status; logs only when a check fails")
    async def watch(self, turn_id: UUID, main: asyncio.Task[None]) -> None:
        while True:
            await asyncio.sleep(self.settings.cancel_poll_seconds)
            try:
                status = await self.store.turn_status(turn_id)
            except Exception as error:
                AppLogger.current().warn(
                    "chat turn status check failed",
                    LogField.error_type(type(error).__name__),
                    LogField.safe_stack_trace(error),
                )
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
        ids = (
            LogField.chat_turn_id(turn_id),
            LogField.chat_session_id(session_id),
            LogField.project_id(project_id),
        )
        # 답변 하나가 흐름 하나다: chat turn start, chat turn completed 또는 chat turn failed(우리 실패 코드가 error.type).
        # 이 task의 모든 줄(content·OpenAI 호출, 저장)에 turn·세션·프로젝트 ID가 붙는다. 예상하지 못한 예외는
        # LogFlow.task가 실패 줄(error, 메시지 없는 스택)로 남기고, task 밖으로는 내보내지 않는다
        # (내보내면 asyncio가 같은 예외를 한 번 더 찍는다)
        with suppress(Exception), LogFlow.task("chat turn", *ids) as task:
            task.add(*ids)
            begun = time.monotonic()
            metrics = Metrics(model=self.model.name)
            spent = Outcome("NOT_ENOUGH", "")
            watcher = asyncio.create_task(
                self.watch(turn_id, asyncio.current_task())  # type: ignore[arg-type]
            )
            outcome = "COMPLETE"
            try:
                channel.put("phase", {"phase": "gathering"})
                async with asyncio.timeout(self.settings.turn_deadline_seconds):
                    turn = await self.prepare(
                        started, user_id, project_id, profile, question
                    )
                    metrics = replace(
                        metrics,
                        snapshot_ms=int((time.monotonic() - begun) * 1000),
                    )
                    channel.put("phase", {"phase": "reading"})
                    result = await self.agent.run(
                        turn,
                        begun + self.settings.turn_deadline_seconds,
                        spent,
                        lambda activity: channel.put("activity", activity),
                    )
                    metrics = replace(
                        finished(metrics, result),
                        context_chars=turn.context_chars,
                        chars_read=turn.chars_read,
                        first_token_ms=int((time.monotonic() - begun) * 1000),
                    )
                    parser = self.deliver(result, turn, channel)
                if not parser.content:
                    raise ModelFailure("MODEL_EMPTY_RESPONSE")
                metrics = replace(
                    metrics, total_ms=int((time.monotonic() - begun) * 1000)
                )
                watcher.cancel()
                answer = await self.store.complete_turn(
                    turn_id,
                    session_id,
                    parser.content,
                    parser.citations,
                    metrics,
                    grounding={
                        "level": result.level,
                        "documents_read": len(turn.read),
                        "missing": (
                            result.reviews[-1].sufficiency.missing
                            if result.reviews and result.level != "SUPPORTED"
                            else []
                        ),
                    },
                    held=[(target, turn.kind_of(target)) for target in turn.read],
                )
                if answer is None:
                    raise Stopped()
                channel.put(
                    "done",
                    {
                        "question": message_json(started.question),
                        "answer": message_json(answer),
                        "usage": await self.usage(user_id),
                    },
                )
            except (asyncio.CancelledError, Stopped):
                outcome = "CANCELLED"
                await self.settle_stop(turn_id, with_usage(metrics, spent.usage), begun)
                channel.put("error", CANCELLED_EVENT)
            except ChatFailure as failure:
                outcome = "FAILED"
                task.fail(failure.code)
                await self.settle_failure(
                    turn_id, failure.code, with_usage(metrics, spent.usage), begun
                )
                channel.put("error", error_event(failure.code))
            except ModelFailure as failure:
                outcome = "FAILED"
                task.fail(failure.code)
                await self.settle_failure(
                    turn_id, failure.code, with_usage(metrics, spent.usage), begun
                )
                channel.put("error", error_event("CHAT_MODEL_UNAVAILABLE"))
            except TimeoutError:
                outcome = "FAILED"
                task.fail("MODEL_DEADLINE")
                await self.settle_failure(
                    turn_id, "MODEL_DEADLINE", with_usage(metrics, spent.usage), begun
                )
                channel.put("error", error_event("CHAT_MODEL_UNAVAILABLE"))
            except Exception:
                outcome = "FAILED"
                await self.settle_failure(
                    turn_id, "INTERNAL", with_usage(metrics, spent.usage), begun
                )
                channel.put("error", error_event("INTERNAL_ERROR"))
                raise
            finally:
                watcher.cancel()
                # 종료 줄의 결과 값. 걸린 시간은 event.duration, 토큰은 호출마다 openai 결과 줄에도 있다
                task.result(
                    LogField.result_code("outcome", outcome, TURN_OUTCOMES),
                    LogField.result_code("grounding", metrics.grounding, GROUNDING),
                    LogField.result("steps", metrics.steps),
                    LogField.result("judge_calls", metrics.judge_calls),
                    LogField.result("rejections", metrics.rejections),
                    LogField.result("chars_read", metrics.chars_read),
                    LogField.result("context_chars", metrics.context_chars),
                    LogField.result("input_tokens", metrics.input_tokens),
                    LogField.result("cached_tokens", metrics.cached_tokens),
                    LogField.result("output_tokens", metrics.output_tokens),
                    LogField.result("reasoning_tokens", metrics.reasoning_tokens),
                    LogField.result("snapshot_ms", metrics.snapshot_ms),
                    LogField.result("first_token_ms", metrics.first_token_ms),
                )

    async def prepare(
        self,
        started: Started,
        user_id: UUID,
        project_id: UUID,
        profile: Profile,
        question: Question,
    ) -> Turn:
        AppLogger.current().info("chat turn prepare")
        session_id = started.session["id"]
        chat_map = await self.content.map(user_id, project_id)
        held = await self.store.held(session_id)
        fetched = await self.content.documents(
            user_id,
            project_id,
            [target for target, kind in held if kind == "DOCUMENT"],
            [target for target, kind in held if kind == "MEMO"],
        )
        documents, memos, dropped = self.trim_held(held, fetched)
        await self.store.drop_held(session_id, dropped)
        summaries = await self.store.summaries(
            [node.id for node in (*chat_map.manuscripts, *chat_map.settings)]
        )
        history = await self.store.history(session_id, started.question["id"])
        return Turn(
            user_id=user_id,
            project_id=project_id,
            chat_map=chat_map,
            profile=profile,
            question=question,
            history=select_history(history, self.settings.history_max_chars),
            summaries=summaries,
            held_documents=documents,
            held_memos=memos,
        )

    @skip_entry_log("Only trims the held documents to the size limit")
    def trim_held(
        self, held: list[tuple[str, str]], fetched
    ) -> tuple[list[Document], list[Memo], list[str]]:
        found: dict[str, Document | Memo] = {
            item.id: item for item in (*fetched.documents, *fetched.memos)
        }
        kept = [found[target] for target, _ in held if target in found]
        dropped = [target for target, _ in held if target not in found]
        total = sum(item.chars for item in kept)
        while kept and total > self.settings.held_max_chars:
            oldest = kept.pop(0)
            total -= oldest.chars
            dropped.append(oldest.id)
        documents = [item for item in kept if isinstance(item, Document)]
        memos = [item for item in kept if isinstance(item, Memo)]
        return documents, memos, dropped

    @skip_entry_log("Only splits the finished answer into stream events")
    def deliver(self, outcome: Outcome, turn: Turn, channel: Channel) -> CitationStream:
        parser = CitationStream(turn.materials())
        channel.put("phase", {"phase": "writing"})
        text = outcome.text
        for start in range(0, len(text), CHUNK):
            self.forward(parser.feed(text[start : start + CHUNK]), channel)
        self.forward(parser.finish(), channel)
        return parser

    @skip_entry_log("Only queues stream events")
    def forward(self, items: list[Any], channel: Channel) -> None:
        for item in items:
            if isinstance(item, Cited):
                channel.put(
                    "citation", {"index": item.index, "citation": item.citation}
                )
            else:
                channel.put("delta", {"text": item.text})

    async def usage(self, user_id: UUID) -> dict[str, Any]:
        AppLogger.current().info("chat usage get")
        used = await self.store.used_today(user_id)
        return usage_json(used, self.settings.daily_credit_limit)

    async def settle_stop(self, turn_id: UUID, metrics: Metrics, begun: float) -> None:
        AppLogger.current().info(
            "chat turn stop record", LogField.param("shutdown", self.stopping)
        )
        metrics = replace(metrics, total_ms=int((time.monotonic() - begun) * 1000))
        try:
            if self.stopping:
                await self.store.fail_turn(turn_id, "SHUTDOWN", metrics)
            else:
                await self.store.stop_turn(turn_id, metrics)
        except Exception as error:
            AppLogger.current().warn(
                "chat turn stop record failed",
                LogField.error_type(type(error).__name__),
                LogField.safe_stack_trace(error),
            )

    async def settle_failure(
        self, turn_id: UUID, code: str, metrics: Metrics, begun: float
    ) -> None:
        AppLogger.current().info("chat turn failure record")
        metrics = replace(metrics, total_ms=int((time.monotonic() - begun) * 1000))
        try:
            await self.store.fail_turn(turn_id, code, metrics)
        except Exception as error:
            AppLogger.current().warn(
                "chat turn failure record failed",
                LogField.error_type(type(error).__name__),
                LogField.safe_stack_trace(error),
            )
