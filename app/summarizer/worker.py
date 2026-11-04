import asyncio
import pathlib
import time

from app.content import doc_type
from app.logs import AppLogger, LogField, LogFlow, LogScope, skip_entry_log
from app.manuscript import (
    UNREADABLE_REASONS,
    KeyUnavailable,
    ProjectKeys,
    Unreadable,
    seal,
    unseal,
)
from app.model import ModelFailure, SummaryModel
from app.prompt import attr
from app.summarizer.fingerprint import changed, fingerprint, threshold
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import Job, SummaryStore

KEY_RETRY_SECONDS = 60
# 회차 하나의 loresentry.schedule.name. 요약 작업 하나의 줄에도 붙여, 회차와 작업을 함께 찾는다
SCHEDULE = "summary-tick"
# 작업 하나의 결과. 작업 종료 줄의 loresentry.result.outcome이고, 회차 종료 줄에는 결과마다 건수가 남는다
OUTCOMES = (
    "summarized",
    "unchanged",
    "empty",
    "copied",
    "failed",
    "deferred",
    "unreadable",
)

SUMMARY_INSTRUCTIONS = """Summarize this document from a web novel project so that someone can decide, without reading it, whether it answers a question about the story.
Write in the language of the document. First 2 to 4 sentences on what it says or what happens, in order. Then one line naming the people, places, objects and terms that appear, with the other names used for them. Use names exactly as written. No evaluation, no guesses, nothing from outside the document. At most 500 characters."""


@skip_entry_log("Only builds the model input")
def document_input(job: Job, text: str) -> str:
    return (
        f'<document kind="{doc_type(job.folder_code)}" title="{attr(job.title)}">\n'
        f"{text}\n</document>"
    )


class Worker:
    def __init__(
        self,
        store: SummaryStore,
        model: SummaryModel,
        settings: SummarizerSettings,
        keys: ProjectKeys,
    ):
        self.store = store
        self.model = model
        self.settings = settings
        self.keys = keys
        self.gate = asyncio.Semaphore(settings.workers)

    @skip_entry_log("Opens the body in a worker thread; the KMS call logs its own line")
    def opened(self, job: Job) -> tuple[str, bytes]:
        key = self.keys.unwrap(job.project_id, job.wrapped_key)
        text = unseal(key, job.body_enc, job.project_id, job.document_id, "body_text")
        return text, key

    @skip_entry_log("Only encrypts the summary")
    def sealed(self, job: Job, key: bytes, summary: str) -> bytes:
        return seal(key, job.project_id, job.document_id, "summary", summary)

    @skip_entry_log(
        "Runs inside the document summary task of guarded, whose start line comes first"
    )
    async def process(self, job: Job) -> str:
        text, key = await asyncio.to_thread(self.opened, job)
        body = text.strip()
        current = fingerprint(text)
        previous = await self.store.previous(job.document_id)
        if (
            previous is not None
            and previous.prompt_version == self.settings.prompt_version
            and changed(previous.fingerprint, current)
            < threshold(
                len(body),
                self.settings.min_change_chars,
                self.settings.min_change_ratio,
            )
        ):
            await self.store.skip(job)
            return "unchanged"
        if not body:
            await self.store.drop_summary(job)
            return "empty"
        if len(body) <= self.settings.short_chars:
            await self.store.save(
                job,
                self.sealed(job, key, body),
                current,
                "body",
                self.settings.prompt_version,
            )
            return "copied"
        summary, usage = await self.model.summarize(
            SUMMARY_INSTRUCTIONS, document_input(job, text)
        )
        # 토큰 수는 openai call completed 줄에 남는다
        await self.store.save(
            job,
            self.sealed(job, key, summary),
            current,
            self.model.name,
            self.settings.prompt_version,
        )
        return "summarized"

    async def guarded(self, job: Job) -> str:
        """작업 하나. 행에 저장한 traceparent(그 변경을 가져온 이벤트의 trace)로 바인딩하고, document summary 흐름 하나로 남긴다.

        실패는 우리 분류가 error.type인 document summary failed(warn)다. 모델 실패는 다시 할지, 키 실패는 KMS가 준 코드,
        열지 못한 본문은 이유 코드를 함께 남긴다. 예상하지 못한 예외는 실패 줄(error, 메시지 없는 스택)을 남기고 회차로 올라간다.
        """
        async with self.gate:
            with (
                LogScope.stored(
                    job.traceparent,
                    LogField.document_id(job.document_id),
                    LogField.project_id(job.project_id),
                    LogField.schedule_name(SCHEDULE),
                ),
                LogFlow.task(
                    "document summary",
                    LogField.param("sequence", job.sequence),
                    LogField.param("revision_no", job.revision_no),
                    LogField.param("attempts", job.attempts),
                ) as task,
            ):
                try:
                    outcome = await self.process(job)
                except ModelFailure as failure:
                    again = await self.store.retry(job, self.settings.max_attempts)
                    task.fail(failure.code).result(LogField.result("retry", again))
                    outcome = "failed"
                except KeyUnavailable as failure:
                    await self.store.postpone(job, KEY_RETRY_SECONDS)
                    task.fail("KeyUnavailable").result(
                        LogField.error_code(failure.code)
                    )
                    outcome = "deferred"
                except Unreadable as failure:
                    await self.store.discard(job)
                    task.fail("Unreadable").result(
                        LogField.result_code("reason", str(failure), UNREADABLE_REASONS)
                    )
                    outcome = "unreadable"
                task.result(LogField.result_code("outcome", outcome, OUTCOMES))
                return outcome

    @skip_entry_log("Called on every summary tick; logs only when it claims jobs")
    async def tick(self) -> list[str]:
        jobs = await self.store.claim(
            self.settings.idle_seconds,
            self.settings.max_per_tick,
            self.settings.lease_seconds,
        )
        if jobs:
            # 이 회차의 첫 줄이라 schedule run start가 먼저 남는다
            AppLogger.current().info(
                "summary jobs claimed", LogField.result("jobs", len(jobs))
            )
        return list(await asyncio.gather(*(self.guarded(job) for job in jobs)))

    async def run(self, stop: asyncio.Event) -> None:
        AppLogger.current().info(
            "summary worker start",
            LogField.param("tick_seconds", self.settings.tick_seconds),
            LogField.param("workers", self.settings.workers),
        )
        heartbeat = pathlib.Path(self.settings.heartbeat_path)
        while not stop.is_set():
            begun = time.monotonic()
            # 회차 하나가 폴링 흐름 하나다. 맡을 작업이 있던 회차만 schedule run start·completed(결과별 건수)를 남긴다.
            # 예외로 끝난 회차는 실패 줄을 남기고 다음 회차에 다시 한다. 그때는 heartbeat를 남기지 않아,
            # 계속 실패하면 liveness가 파드를 다시 띄운다
            with LogFlow.polling(SCHEDULE) as tick:
                results = await self.tick()
                heartbeat.touch()
                tick.result(
                    *(
                        LogField.result(outcome, results.count(outcome))
                        for outcome in OUTCOMES
                    )
                )
            wait = self.settings.tick_seconds - (time.monotonic() - begun)
            try:
                await asyncio.wait_for(stop.wait(), max(wait, 0.1))
            except TimeoutError:
                pass
