import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.content import ContentSource
from app.model import AnswerModel, ModelFailure, Usage
from app.navigation import (
    ANSWER_CHECK_INSTRUCTIONS,
    NAVIGATE_INSTRUCTIONS,
    SUFFICIENCY_INSTRUCTIONS,
    AnswerCheck,
    Review,
    Sufficiency,
    Turn,
    context_xml,
    final_xml,
    review_xml,
)
from app.prompt import text_block
from app.settings import Settings

log = logging.getLogger("ai_chat.agent")

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "request_documents",
        "description": "Read documents or memos from the map, most necessary first.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ranking"],
            "properties": {
                "ranking": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "description": "Node ids in priority order.",
                }
            },
        },
    }
]


@dataclass(frozen=True)
class Picked:
    targets: list[str]
    skipped: list[str]
    unknown: list[str]
    held: list[str]


def pack(
    ranking: list[str],
    by_short: dict[str, str],
    chars: dict[str, int],
    held: set[str],
    cap: int,
) -> Picked:
    targets: list[str] = []
    skipped: list[str] = []
    unknown: list[str] = []
    kept: list[str] = []
    seen: set[str] = set()
    full = False
    for raw in ranking:
        alias = raw.strip()
        if alias in seen:
            continue
        seen.add(alias)
        target = by_short.get(alias)
        if target is None:
            unknown.append(alias)
            continue
        if target in held:
            kept.append(alias)
            continue
        cost = chars[target]
        if not full and cost <= cap:
            targets.append(target)
            cap -= cost
        elif not full and not targets:
            targets.append(target)
            full = True
        else:
            skipped.append(alias)
    return Picked(targets, skipped, unknown, kept)


@dataclass
class Outcome:
    level: str
    text: str
    reviews: list[Review] = field(default_factory=list)
    steps: int = 0
    judge_calls: int = 0
    usage: Usage | None = None


def add(total: Usage | None, usage: Usage | None) -> Usage | None:
    if total is None:
        return usage
    return total + usage


class Agent:
    def __init__(self, model: AnswerModel, content: ContentSource, settings: Settings):
        self.model = model
        self.content = content
        self.settings = settings

    async def read(self, turn: Turn, step: int, ranking: list[str]) -> str:
        picked = pack(
            ranking,
            turn.by_short,
            {node.id: node.chars for node in turn.chat_map.ordered},
            turn.held_ids,
            self.settings.step_max_chars,
        )
        documents = [t for t in picked.targets if turn.kind_of(t) == "DOCUMENT"]
        memos = [t for t in picked.targets if turn.kind_of(t) == "MEMO"]
        fetched = await self.content.documents(
            turn.user_id, turn.project_id, documents, memos
        )
        turn.hold(fetched)
        return context_xml(
            step, fetched, turn, picked.skipped, picked.unknown, picked.held
        )

    async def judge(
        self, turn: Turn, answer: str, last: Sufficiency | None
    ) -> tuple[Sufficiency, AnswerCheck, Usage | None, int]:
        checks: list[Any] = [
            self.model.check_answer(
                ANSWER_CHECK_INSTRUCTIONS, turn.answer_input(answer)
            )
        ]
        fresh = last is None or turn.read_since_check
        if fresh:
            checks.append(
                self.model.sufficiency(
                    SUFFICIENCY_INSTRUCTIONS, turn.sufficiency_input()
                )
            )
        results = await asyncio.gather(*checks)
        check, usage = results[0]
        if fresh:
            sufficiency, more = results[1]
            usage = add(usage, more)
        else:
            sufficiency = last
        turn.read_since_check = False
        return sufficiency, check, usage, len(checks)

    async def run(
        self, turn: Turn, deadline: float, outcome: Outcome | None = None
    ) -> Outcome:
        items = turn.base_input()
        cache_key = str(turn.project_id)
        outcome = outcome or Outcome("NOT_ENOUGH", "")
        sufficiency: Sufficiency | None = None
        idle = 0
        for step in range(1, self.settings.max_steps + 1):
            if deadline - time.monotonic() < self.settings.answer_reserve_seconds:
                break
            force = step == self.settings.max_steps or idle >= 2
            out = await self.model.step(
                NAVIGATE_INSTRUCTIONS,
                items,
                TOOLS,
                "none" if force else "auto",
                cache_key,
            )
            outcome.steps += 1
            outcome.usage = add(outcome.usage, out.usage)
            if out.call is not None:
                items += out.items
                before = len(turn.read)
                result = await self.read(turn, step, out.call.ranking)
                idle = idle + 1 if len(turn.read) == before else 0
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": out.call.id,
                        "output": result,
                    }
                )
                continue
            items += out.items
            if not out.text.strip():
                raise ModelFailure("MODEL_EMPTY_RESPONSE")
            try:
                sufficiency, check, usage, calls = await self.judge(
                    turn, out.text, sufficiency
                )
            except (ModelFailure, KeyError, TypeError) as error:
                log.warning("review failed", extra={"error": str(error)})
                outcome.text = out.text
                return outcome
            outcome.judge_calls += calls
            outcome.usage = add(outcome.usage, usage)
            if sufficiency.sufficient and check.answer_ok:
                outcome.level = "SUPPORTED"
                outcome.text = out.text
                return outcome
            review = Review(len(outcome.reviews) + 1, sufficiency, check)
            outcome.reviews.append(review)
            items.append({"role": "user", "content": [text_block(review_xml(review))]})
        final = await self.model.step(
            NAVIGATE_INSTRUCTIONS,
            items
            + [{"role": "user", "content": [text_block(final_xml(outcome.reviews))]}],
            TOOLS,
            "none",
            cache_key,
        )
        outcome.steps += 1
        outcome.usage = add(outcome.usage, final.usage)
        if not final.text.strip():
            raise ModelFailure("MODEL_EMPTY_RESPONSE")
        outcome.text = final.text
        if outcome.reviews and outcome.reviews[-1].sufficiency.sufficient:
            outcome.level = "SUPPORTED"
        return outcome
