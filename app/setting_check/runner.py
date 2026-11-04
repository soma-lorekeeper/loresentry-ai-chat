import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.content import ChatMap, ContentSource, Document, Node
from app.errors import ChatFailure
from app.logs import AppLogger, LogField, LogFlow, skip_entry_log
from app.model import CheckModel, ModelFailure, Usage
from app.setting_check.findings import (
    Candidate,
    Reading,
    dedupe,
    fingerprint,
    judged,
    parse_candidates,
)
from app.setting_check.prompts import (
    CORE_INSTRUCTIONS,
    CORE_SCHEMA,
    JUDGE_INSTRUCTIONS,
    JUDGE_SCHEMA,
    WHOLE_INSTRUCTIONS,
    WHOLE_SCHEMA,
    Note,
    core_input,
    judge_input,
    whole_input,
)
from app.setting_check.store import CheckStore, Finding, Read
from app.settings import Settings

# 설정 오류 검사 1회를 백그라운드 task로 돌리는 service. 모듈 이름으로 레이어를 정할 수 없어 직접 둔다
LOG_LAYER = "service"

BATCH = 100
# setting check 종료 줄의 loresentry.result.outcome 값(목록 밖은 other)
RUN_OUTCOMES = {"DONE", "CANCELLED", "INTERRUPTED", "FAILED"}


class Stopped(Exception):
    pass


@dataclass
class Tally:
    usage: Usage = field(default_factory=lambda: Usage(0, 0, 0, 0, 0))
    manuscripts: int = 0
    settings: int = 0
    core_facts: int = 0
    found: int = 0
    verified: int = 0
    judged: int = 0
    findings: int = 0

    @skip_entry_log("Only adds token counts")
    def add(self, usage: Usage | None) -> None:
        self.usage = self.usage + usage


@skip_entry_log("Only finds a chapter on the map")
def chapter(chat_map: ChatMap, document_id: str) -> Node:
    for node in chat_map.manuscripts:
        if node.id == document_id:
            return node
    raise ChatFailure("SETTING_CHECK_DOCUMENT_NOT_FOUND")


@skip_entry_log("Only picks what fits the size limit")
def newest_fitting[T](items: list[T], room: int, size: Callable[[T], int]) -> list[T]:
    kept: list[T] = []
    for item in reversed(items):
        if size(item) > room:
            break
        kept.append(item)
        room -= size(item)
    kept.reverse()
    return kept


@skip_entry_log("Only runs calls in a task group")
async def together[T](calls: list[Coroutine[Any, Any, T]]) -> list[T]:
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(call) for call in calls]
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    return [task.result() for task in tasks]


@skip_entry_log("Only builds a finding")
def finding_of(reading: Reading, candidate: Candidate) -> Finding:
    source = reading.documents[candidate.source_id]
    return Finding(
        kind=candidate.kind,
        core=candidate.core,
        confidence=candidate.confidence,
        explanation=candidate.explanation,
        quote=candidate.quote,
        quote_focus=candidate.quote_focus,
        source_document_id=source.id,
        source_title=source.title,
        source_doc_type=source.doc_type,
        source_ordinal=source.ordinal if source.is_manuscript else None,
        source_quote=candidate.source_quote,
        source_focus=candidate.source_focus,
        fingerprint=fingerprint(
            candidate.kind, source.id, candidate.source_quote, candidate.quote
        ),
    )


@skip_entry_log("Only counts what was read")
def read_of(reading: Reading) -> Read:
    ordinals = [doc.ordinal for doc in reading.earlier if doc.ordinal is not None]
    return Read(
        manuscripts=len(reading.earlier),
        from_ordinal=min(ordinals, default=None),
        to_ordinal=max(ordinals, default=None),
        settings=len(reading.settings),
        core_facts=reading.core_facts,
    )


class CheckRunner:
    def __init__(
        self,
        store: CheckStore,
        content: ContentSource,
        model: CheckModel,
        settings: Settings,
    ):
        self.store = store
        self.content = content
        self.model = model
        self.settings = settings
        self.tasks: dict[UUID, tuple[asyncio.Task[None], UUID, UUID]] = {}
        self.stopping = False

    @skip_entry_log("Creates the run task, whose setting check start line comes next")
    def start(
        self,
        run_id: UUID,
        user_id: UUID,
        project_id: UUID,
        document_id: str,
        locale: str,
        chat_map: ChatMap,
    ) -> None:
        task = asyncio.create_task(
            self.run(run_id, user_id, project_id, document_id, locale, chat_map)
        )
        self.tasks[run_id] = (task, user_id, project_id)
        task.add_done_callback(lambda _: self.tasks.pop(run_id, None))

    def stop(self, run_id: UUID) -> None:
        AppLogger.current().info(
            "setting check stop", LogField.setting_check_run_id(run_id)
        )
        running = self.tasks.get(run_id)
        if running:
            running[0].cancel()

    def stop_for(self, user_id: UUID, project_id: UUID | None = None) -> None:
        AppLogger.current().info(
            "setting checks stop for owner", LogField.project_id(project_id)
        )
        for task, owner, project in list(self.tasks.values()):
            if owner == user_id and project_id in (None, project):
                task.cancel()

    async def drain(self) -> None:
        self.stopping = True
        running = [task for task, _, _ in self.tasks.values()]
        AppLogger.current().info(
            "setting checks drain", LogField.param("running", running)
        )
        if not running:
            return
        _, pending = await asyncio.wait(
            running, timeout=self.settings.shutdown_grace_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)

    async def run(
        self,
        run_id: UUID,
        user_id: UUID,
        project_id: UUID,
        document_id: str,
        locale: str,
        chat_map: ChatMap,
    ) -> None:
        ids = (
            LogField.setting_check_run_id(run_id),
            LogField.project_id(project_id),
            LogField.document_id(document_id),
        )
        # 검사 1회가 흐름 하나다: setting check start, setting check completed 또는 setting check failed(우리 실패
        # 코드가 error.type). 이 task의 모든 줄에 run·프로젝트·문서 ID가 붙는다. 예상하지 못한 예외는 LogFlow.task가
        # 실패 줄(error, 메시지 없는 스택)로 남기고, task 밖으로는 내보내지 않는다(내보내면 asyncio가 한 번 더 찍는다)
        with suppress(Exception), LogFlow.task("setting check", *ids) as task:
            task.add(*ids)
            tally = Tally()
            outcome = "DONE"
            try:
                async with asyncio.timeout(
                    self.settings.setting_check_deadline_seconds
                ):
                    reading = await self.read(
                        user_id, project_id, document_id, chat_map
                    )
                    read = read_of(reading)
                    tally.manuscripts = read.manuscripts
                    tally.settings = read.settings
                    tally.core_facts = read.core_facts
                    if not await self.store.advance(
                        run_id, reading.target.title, reading.target.revision_no, read
                    ):
                        raise Stopped()
                    findings = await self.check(
                        run_id, project_id, reading, locale, tally
                    )
                if not await self.store.finish(run_id, findings, tally.usage):
                    raise Stopped()
                tally.findings = len(findings)
            except (asyncio.CancelledError, Stopped):
                outcome = await self.settle_stop(run_id, tally)
            except ChatFailure as failure:
                outcome = "FAILED"
                task.fail(failure.code)
                await self.settle_failure(run_id, failure.code, tally)
            except ModelFailure as failure:
                outcome = "FAILED"
                task.fail(failure.code)
                await self.settle_failure(run_id, "CHAT_MODEL_UNAVAILABLE", tally)
            except TimeoutError:
                outcome = "FAILED"
                task.fail("DEADLINE")
                await self.settle_failure(run_id, "CHAT_MODEL_UNAVAILABLE", tally)
            except Exception:
                outcome = "FAILED"
                await self.settle_failure(run_id, "INTERNAL_ERROR", tally)
                raise
            finally:
                # 종료 줄의 결과 값. 걸린 시간은 event.duration, 토큰은 호출마다 openai 결과 줄에도 있다
                task.result(
                    LogField.result_code("outcome", outcome, RUN_OUTCOMES),
                    LogField.result("manuscripts", tally.manuscripts),
                    LogField.result("settings", tally.settings),
                    LogField.result("core_facts", tally.core_facts),
                    LogField.result("candidates", tally.found),
                    LogField.result("verified", tally.verified),
                    LogField.result("judged", tally.judged),
                    LogField.result("findings", tally.findings),
                    LogField.result("input_tokens", tally.usage.input_tokens),
                    LogField.result("cached_tokens", tally.usage.cached_tokens),
                    LogField.result("output_tokens", tally.usage.output_tokens),
                    LogField.result("reasoning_tokens", tally.usage.reasoning_tokens),
                )

    async def read(
        self, user_id: UUID, project_id: UUID, document_id: str, chat_map: ChatMap
    ) -> Reading:
        AppLogger.current().info("setting check documents read")
        target = chapter(chat_map, document_id)
        settings = list(chat_map.settings)
        earlier = sorted(
            (
                node
                for node in chat_map.manuscripts
                if node.ordinal is not None
                and target.ordinal is not None
                and node.ordinal < target.ordinal
            ),
            key=lambda node: node.ordinal or 0,
        )
        limit = self.settings.setting_check_max_chars
        room = limit - target.chars - sum(node.chars for node in settings)
        if room < 0:
            raise ChatFailure("CHAT_CONTEXT_TOO_LARGE")
        earlier = newest_fitting(earlier, room, lambda node: node.chars)
        fetched = await self.fetch(
            user_id,
            project_id,
            [
                target.id,
                *(node.id for node in earlier),
                *(node.id for node in settings),
            ],
        )
        document = fetched.get(target.id)
        if document is None or not document.is_manuscript:
            raise ChatFailure("SETTING_CHECK_DOCUMENT_NOT_FOUND")
        sources = [fetched[node.id] for node in settings if node.id in fetched]
        room = limit - document.chars - sum(doc.chars for doc in sources)
        if room < 0:
            raise ChatFailure("CHAT_CONTEXT_TOO_LARGE")
        before = newest_fitting(
            [fetched[node.id] for node in earlier if node.id in fetched],
            room,
            lambda doc: doc.chars,
        )
        return Reading(document, tuple(before), tuple(sources))

    async def fetch(
        self, user_id: UUID, project_id: UUID, ids: list[str]
    ) -> dict[str, Document]:
        AppLogger.current().info(
            "setting check documents fetch", LogField.param("document_ids", ids)
        )
        found: dict[str, Document] = {}
        for start in range(0, len(ids), BATCH):
            fetched = await self.content.documents(
                user_id, project_id, ids[start : start + BATCH], []
            )
            found.update((doc.id, doc) for doc in fetched.documents)
        return found

    async def check(
        self,
        run_id: UUID,
        project_id: UUID,
        reading: Reading,
        locale: str,
        tally: Tally,
    ) -> list[Finding]:
        AppLogger.current().info("setting check candidates check")
        cache_key = f"setting-check:{project_id}"
        found = await self.find(reading, locale, tally, cache_key)
        tally.found = len(found)
        verified = dedupe(
            [kept for kept in (reading.verify(item) for item in found) if kept]
        )
        tally.verified = len(verified)
        if not verified:
            return []
        if not await self.store.stage(run_id, "JUDGING"):
            raise Stopped()
        notes = await self.store.notes(
            project_id, sorted({candidate.source_id for candidate in verified})
        )
        gate = asyncio.Semaphore(max(1, self.settings.setting_check_judge_concurrency))
        results = await together(
            [
                self.judge(
                    reading,
                    candidate,
                    notes.get(candidate.source_id, []),
                    locale,
                    tally,
                    gate,
                    cache_key,
                )
                for candidate in verified
            ]
        )
        kept = reading.order([result for result in results if result is not None])
        return [finding_of(reading, candidate) for candidate in kept]

    async def find(
        self, reading: Reading, locale: str, tally: Tally, cache_key: str
    ) -> list[Candidate]:
        AppLogger.current().info(
            "setting check candidates find",
            LogField.param("core_pass", bool(reading.core_facts)),
        )

        async def whole() -> list[Candidate]:
            value, usage = await self.model.find(
                WHOLE_INSTRUCTIONS,
                whole_input(reading, locale),
                "setting_check_candidates",
                WHOLE_SCHEMA,
                cache_key,
            )
            tally.add(usage)
            return parse_candidates(value, reading.by_short, core=False)

        async def core() -> list[Candidate]:
            value, usage = await self.model.find(
                CORE_INSTRUCTIONS,
                core_input(reading, locale),
                "setting_check_core_candidates",
                CORE_SCHEMA,
                cache_key,
            )
            tally.add(usage)
            return parse_candidates(value, reading.by_short, core=True)

        passes = [core(), whole()] if reading.core_facts else [whole()]
        return [candidate for result in await together(passes) for candidate in result]

    async def judge(
        self,
        reading: Reading,
        candidate: Candidate,
        notes: list[Note],
        locale: str,
        tally: Tally,
        gate: asyncio.Semaphore,
        cache_key: str,
    ) -> Candidate | None:
        AppLogger.current().info("setting check candidate judge")
        async with gate:
            value, usage = await self.model.judge(
                JUDGE_INSTRUCTIONS,
                judge_input(reading, candidate, notes, locale),
                "setting_check_judgement",
                JUDGE_SCHEMA,
                cache_key,
            )
        tally.add(usage)
        tally.judged += 1
        return judged(candidate, value)

    async def settle_stop(self, run_id: UUID, tally: Tally) -> str:
        AppLogger.current().info(
            "setting check stop record", LogField.param("shutdown", self.stopping)
        )
        try:
            if self.stopping:
                await self.store.fail(run_id, "INTERRUPTED", tally.usage)
                return "INTERRUPTED"
            await self.store.record_usage(run_id, tally.usage)
        except Exception as error:
            AppLogger.current().warn(
                "setting check stop record failed",
                LogField.error_type(type(error).__name__),
                LogField.safe_stack_trace(error),
            )
        return "CANCELLED"

    async def settle_failure(self, run_id: UUID, code: str, tally: Tally) -> None:
        AppLogger.current().info("setting check failure record")
        try:
            await self.store.fail(run_id, code, tally.usage)
        except Exception as error:
            AppLogger.current().warn(
                "setting check failure record failed",
                LogField.error_type(type(error).__name__),
                LogField.safe_stack_trace(error),
            )
