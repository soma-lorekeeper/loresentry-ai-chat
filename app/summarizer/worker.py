import asyncio
import logging
import pathlib
import time

from app.content import doc_type
from app.model import ModelFailure, SummaryModel
from app.prompt import attr
from app.summarizer.fingerprint import changed, fingerprint, threshold
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import Job, SummaryStore

log = logging.getLogger("ai_chat.summarizer")

SUMMARY_INSTRUCTIONS = """Summarize this document from a web novel project so that someone can decide, without reading it, whether it answers a question about the story.
Write in the language of the document. First 2 to 4 sentences on what it says or what happens, in order. Then one line naming the people, places, objects and terms that appear, with the other names used for them. Use names exactly as written. No evaluation, no guesses, nothing from outside the document. At most 500 characters."""


def document_input(job: Job) -> str:
    return (
        f'<document kind="{doc_type(job.folder_code)}" title="{attr(job.title)}">\n'
        f"{job.body}\n</document>"
    )


class Worker:
    def __init__(
        self,
        store: SummaryStore,
        model: SummaryModel,
        settings: SummarizerSettings,
    ):
        self.store = store
        self.model = model
        self.settings = settings
        self.gate = asyncio.Semaphore(settings.workers)

    async def process(self, job: Job) -> str:
        body = job.body.strip()
        current = fingerprint(job.body)
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
                job, body, current, "body", self.settings.prompt_version
            )
            return "copied"
        summary, usage = await self.model.summarize(
            SUMMARY_INSTRUCTIONS, document_input(job)
        )
        await self.store.save(
            job, summary, current, self.model.name, self.settings.prompt_version
        )
        log.info(
            "summary written",
            extra={
                "document_id": job.document_id,
                "sequence": job.sequence,
                "revision_no": job.revision_no,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
            },
        )
        return "summarized"

    async def guarded(self, job: Job) -> str:
        async with self.gate:
            try:
                return await self.process(job)
            except ModelFailure as failure:
                again = await self.store.retry(job, self.settings.max_attempts)
                log.warning(
                    "summary failed",
                    extra={
                        "document_id": job.document_id,
                        "sequence": job.sequence,
                        "code": failure.code,
                        "retry": again,
                    },
                )
                return "failed"

    async def tick(self) -> list[str]:
        jobs = await self.store.claim(
            self.settings.idle_seconds,
            self.settings.max_per_tick,
            self.settings.lease_seconds,
        )
        return list(await asyncio.gather(*(self.guarded(job) for job in jobs)))

    async def run(self, stop: asyncio.Event) -> None:
        heartbeat = pathlib.Path(self.settings.heartbeat_path)
        while not stop.is_set():
            begun = time.monotonic()
            results = await self.tick()
            heartbeat.touch()
            if results:
                log.info(
                    "summary tick",
                    extra={outcome: results.count(outcome) for outcome in set(results)},
                )
            wait = self.settings.tick_seconds - (time.monotonic() - begun)
            try:
                await asyncio.wait_for(stop.wait(), max(wait, 0.1))
            except TimeoutError:
                pass
