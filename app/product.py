"""Answers about Lore Sentry itself: how to use it, where things are, what a feature does.

A turn is first sorted (`route`): a question about the author's work goes through
the document-reading loop in `app/agent.py`; a question about the tool is answered
here from the user guide (`app/guide.py`); a message that asks both gets both
answers. The product answer is written from the guide only, checked once against
it, and may end with up to three `<action id="…"/>` tags that the app shows as
shortcut buttons (the tour, a screen, the import dialog, a guide section).
"""

import logging
from collections.abc import Callable
from typing import Any

from app.agent import Outcome, add
from app.guide import (
    Guide,
    attr,
    guide_locale,
    guide_xml,
    load,
    parse_actions,
    strip_markers,
    with_markers,
)
from app.model import AnswerModel, ModelFailure, Usage
from app.navigation import AnswerCheck, Review, Sufficiency, review_xml
from app.prompt import FORMAT, Exchange, Profile, Question, text_block

log = logging.getLogger("ai_chat.product")

Notify = Callable[[dict[str, Any]], None]

KINDS = ("story", "product", "mixed")

ROUTE_INSTRUCTIONS = """You sort the latest message an author sent to the assistant inside Lore Sentry, a writing tool for web novel authors. You do not answer it. Return JSON {"kind": "story" | "product" | "mixed"}.
- story: about the author's own work: its characters, events, places, settings, continuity, timeline, titles, plot or wording, including requests to evaluate, summarize or improve their text, and questions that name people or events of the story.
- product: about Lore Sentry itself: how to use it or start, where a button, menu or screen is, what a feature does or does not do, why something did or did not happen after pressing something, supported file formats, import and export, saving, account, limits, prices, error or notice messages shown by the app, what this assistant can do ("can you…?", "is it possible to…?"), and greetings, thanks, tests or messages with no reference to the story (e.g. "hi", "ㅎㅇ", "테스트").
- mixed: asks both at once, for example how to use a feature on a named part of their story, or a story question followed by a question about the tool.
<features> lists the tool's features so you recognise their names: AI 챗(AI chat), AI의 설정 문서 편집 제안(refresh setting docs, 최신화), 설정 오류 점검(setting check), 관계도(relationship map, graph), 타임라인(timeline), 메모(notes), 검색(search), 가져오기(import), 내보내기(export), 버전 기록(version history), 잠금(lock), 휴지통(trash), 즐겨찾기(favorites), 속성 표(property table), 에피소드(episode), 프로젝트(project), 설정(settings), 도움말(help), 투어(tour), 온보딩(onboarding).
Use <earlier_questions> only to resolve what a short follow-up refers to. When in doubt between story and product, choose product only if the message is clearly about the tool rather than the text of the work."""

PRODUCT_INSTRUCTIONS = f"""You are the assistant inside Lore Sentry, a writing tool for web novel authors. In this turn the author asks about the tool itself: how to use it, where something is, what a feature does, or they greet you or test you. Answer from <guide> only.

# Materials
- <guide>: the user guide of Lore Sentry as shipped, in the author's language: an <overview>, then <topic>s with <section>s. Each has an id.
- <actions>: things the author can do with one press from your answer.
- <author>: who you talk to. Earlier turns of this conversation precede the question.

# How to answer
- Answer only what the guide supports. Name screens, buttons and menus exactly as the guide names them. Never invent features, limits, prices, shortcuts or file formats. What the guide marks as 준비 중 or coming soon is not available yet; say so.
- If the guide does not cover the question, say so plainly in one sentence, say what the nearest feature can do, and suggest 도움말 → 피드백 보내기 (Help → Send feedback). Do not guess.
- When the author reports that something did not happen (nothing changed, nothing appeared, a button did nothing), explain from the guide when it does happen, the most likely reason it did not, and what to do next.
- When the author asks what you can do or how to start, say you answer two kinds of questions: about their own work (characters, events, settings, with source documents attached) and about using Lore Sentry. Give two or three example questions about their work.
- Greetings or tests: greet in one sentence, say in one sentence what you can do, and offer one example question. Do not lecture.
- Give steps as "- " bullets in the order the author does them. 3 to 8 sentences or bullets in all. Answer in the language of the author's message; if unclear, use the guide's language.
- You cannot press buttons or change the author's documents or settings. Offer actions instead.

# Actions
After the answer, on a new line, add one to three <action id="..."/> tags, most useful first: the guide section that covers the answer (guide:<topic>#<section>), the screen to open (view:...), the import dialog (import), or the tour (tour) for an author who is new. Use only ids that <actions> allows and topic/section ids that appear in <guide>. Do not mention the tags in the text; the app shows them as buttons.

{FORMAT}
- Do not use <cite> tags here."""

PRODUCT_CHECK_INSTRUCTIONS = """You review an answer about how to use Lore Sentry, a writing tool for web novel authors, against its user guide. You do not rewrite it.
<guide> is the guide, <conversation> the earlier turns, <question> the author's message and <answer> the answer. The answer is ok if every claim about the tool (what exists, where it is, what a button, menu or screen is called, what happens or does not happen, what is not available) is supported by the guide, and it does not present something the guide marks as 준비 중 or coming soon as available. <action> tags are not claims. An honest statement that the guide does not cover something is ok when the guide indeed does not. Greetings, an offer of help and example questions about the author's story need no support.
List each failure in `problems`, briefly and specifically, in the language of the question."""


def author_xml(profile: Profile) -> str:
    return (
        f'<author name="{attr(profile.display_name or "")}"'
        f' locale="{attr(profile.locale or "")}"/>'
    )


def route_input(question: Question, history: list[Exchange]) -> str:
    earlier = "\n".join(
        f"<question>{exchange.question.content}</question>" for exchange in history
    )
    return (
        f"<earlier_questions>\n{earlier}\n</earlier_questions>\n"
        f"<question>\n{question.content}\n</question>"
    )


def conversation_items(history: list[Exchange]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for exchange in history:
        items.append(
            {
                "role": "user",
                "content": [
                    text_block(f"<question>\n{exchange.question.content}\n</question>")
                ],
            }
        )
        items.append({"role": "assistant", "content": strip_markers(exchange.answer)})
    return items


def check_input(
    guide_text: str, question: Question, history: list[Exchange], answer: str
) -> str:
    conversation = "\n".join(
        f"<question>{exchange.question.content}</question>\n"
        f"<answer>{strip_markers(exchange.answer)}</answer>"
        for exchange in history
    )
    return (
        f"{guide_text}\n<conversation>\n{conversation}\n</conversation>\n"
        f"<question>\n{question.content}\n</question>\n<answer>\n{answer}\n</answer>"
    )


class ProductAgent:
    def __init__(self, model: AnswerModel):
        self.model = model

    async def route(
        self, question: Question, history: list[Exchange]
    ) -> tuple[str, Usage | None]:
        if question.quote is not None or question.intent != "ask":
            return "story", None
        if not question.content.strip():
            return "story", None
        try:
            kind, usage = await self.model.route(
                ROUTE_INSTRUCTIONS, route_input(question, history)
            )
        except (ModelFailure, KeyError, TypeError) as error:
            log.warning("route failed", extra={"error": str(error)})
            return "story", None
        return (kind if kind in KINDS else "story"), usage

    async def run(
        self,
        profile: Profile,
        question: Question,
        history: list[Exchange],
        outcome: Outcome | None = None,
        notify: Notify | None = None,
    ) -> Outcome:
        notify = notify or (lambda _: None)
        outcome = outcome or Outcome("PRODUCT", "")
        outcome.level = "PRODUCT"
        guide: Guide = load(guide_locale(question.content, profile.locale))
        guide_text = guide_xml(guide)
        notify({"kind": "guide"})
        items: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    text_block(guide_text, breakpoint=True),
                    text_block(author_xml(profile)),
                ],
            },
            *conversation_items(history),
            {
                "role": "user",
                "content": [
                    text_block(
                        f"<turn>\n<question>\n{question.content}\n</question>\n</turn>"
                    )
                ],
            },
        ]
        out = await self.model.write(
            PRODUCT_INSTRUCTIONS, items, f"guide:{guide.locale}"
        )
        outcome.steps += 1
        outcome.usage = add(outcome.usage, out.usage)
        if not out.text.strip():
            raise ModelFailure("MODEL_EMPTY_RESPONSE")
        text = out.text
        notify({"kind": "review", "step": outcome.steps})
        try:
            check, usage = await self.model.check_answer(
                PRODUCT_CHECK_INSTRUCTIONS,
                check_input(guide_text, question, history, text),
            )
        except (ModelFailure, KeyError, TypeError) as error:
            log.warning("product review failed", extra={"error": str(error)})
            check, usage = AnswerCheck(True, []), None
        outcome.judge_calls += 1
        outcome.usage = add(outcome.usage, usage)
        notify(
            {
                "kind": "reviewed",
                "step": outcome.steps,
                "passed": check.answer_ok,
                "missing": [],
                "problems": 0 if check.answer_ok else len(check.problems),
            }
        )
        if not check.answer_ok:
            review = Review(len(outcome.reviews) + 1, Sufficiency(True, []), check)
            outcome.reviews.append(review)
            items += out.items
            items.append({"role": "user", "content": [text_block(review_xml(review))]})
            notify({"kind": "final"})
            again = await self.model.write(
                PRODUCT_INSTRUCTIONS, items, f"guide:{guide.locale}"
            )
            outcome.steps += 1
            outcome.usage = add(outcome.usage, again.usage)
            if again.text.strip():
                text = again.text
        clean, actions = parse_actions(text, guide)
        outcome.text = with_markers(clean, actions)
        return outcome
