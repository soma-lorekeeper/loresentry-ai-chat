import asyncio

from app.navigation import AnswerCheck
from app.product import (
    PRODUCT_CHECK_INSTRUCTIONS,
    PRODUCT_INSTRUCTIONS,
    ROUTE_INSTRUCTIONS,
    ProductAgent,
)
from app.prompt import Exchange, Profile, Question
from tests.support import PRODUCT_ANSWER, FakeModel

QUESTION = Question("쓰던 원고는 어떻게 가져와?", "ask", None)


def run(agent: ProductAgent, question=QUESTION, history=None, profile=None):
    return asyncio.run(agent.run(profile or Profile(), question, history or []))


def test_a_usage_question_is_answered_from_the_guide_with_shortcuts():
    model = FakeModel()
    outcome = run(ProductAgent(model))

    assert outcome.level == "PRODUCT"
    assert outcome.steps == 1 and outcome.judge_calls == 1
    assert outcome.text == (
        "쓰던 원고는 사이드바의 ‘가져오기’로 올려요. 텍스트(.txt)와 마크다운(.md)만 받아요.\n"
        "[[action:import]][[action:guide:writing#import]]"
    )
    first = model.write_inputs[0]
    guide_block = first[0]["content"][0]
    assert guide_block["text"].startswith('<guide locale="ko">')
    assert guide_block["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "<question>\n쓰던 원고는 어떻게 가져와?" in first[-1]["content"][0]["text"]
    assert model.check_inputs[0].startswith('<guide locale="ko">')
    assert "<answer>" in model.check_inputs[0]


def test_a_failed_check_gets_one_rewrite():
    model = FakeModel(
        writes=['docx도 돼요. <action id="import"/>', PRODUCT_ANSWER],
        checks=[AnswerCheck(False, ["docx는 지원하지 않는다"])],
    )
    outcome = run(ProductAgent(model))

    assert outcome.steps == 2 and outcome.judge_calls == 1
    assert outcome.text.startswith("쓰던 원고는 사이드바의")
    review = model.write_inputs[1][-1]["content"][0]["text"]
    assert review.startswith('<review n="1"') and "docx는 지원하지 않는다" in review
    assert outcome.reviews[0].check.problems == ["docx는 지원하지 않는다"]


def test_english_questions_read_the_english_guide():
    model = FakeModel(writes=['Press Import in the sidebar. <action id="import"/>'])
    outcome = run(
        ProductAgent(model),
        Question("How do I import my chapters?", "ask", None),
        profile=Profile(locale="en"),
    )

    assert model.write_inputs[0][0]["content"][0]["text"].startswith(
        '<guide locale="en">'
    )
    assert outcome.text == "Press Import in the sidebar.\n[[action:import]]"


def test_unknown_actions_are_dropped_and_history_loses_its_markers():
    model = FakeModel(writes=['답이에요. <action id="view:nope"/>'])
    history = [
        Exchange(
            Question("안녕", "ask", None),
            "안녕하세요.\n[[action:tour]]",
            [],
        )
    ]
    outcome = run(ProductAgent(model), history=history)

    assert outcome.text == "답이에요."
    items = model.write_inputs[0]
    assert items[1]["content"][0]["text"] == "<question>\n안녕\n</question>"
    assert items[2] == {"role": "assistant", "content": "안녕하세요."}


def test_routing_skips_quotes_and_selection_intents():
    model = FakeModel(routes=["product"])
    agent = ProductAgent(model)

    quoted = Question("", "ask", {"file_id": "x", "title": "t", "text": "q"})
    assert asyncio.run(agent.route(quoted, [])) == ("story", None)
    explain = Question("설명해 줘", "explain", None)
    assert asyncio.run(agent.route(explain, [])) == ("story", None)
    assert model.route_inputs == []

    assert asyncio.run(agent.route(QUESTION, [])) == ("product", None)
    assert model.route_inputs[0].endswith(
        "<question>\n쓰던 원고는 어떻게 가져와?\n</question>"
    )


def test_routing_failures_and_odd_verdicts_fall_back_to_the_story_loop():
    model = FakeModel(routes=["weird"])
    agent = ProductAgent(model)
    model.fail_route = "MODEL_TIMEOUT"

    assert asyncio.run(agent.route(QUESTION, [])) == ("story", None)
    assert asyncio.run(agent.route(QUESTION, [])) == ("story", None)


def test_instructions_mention_the_rules_the_frontend_relies_on():
    assert "<action id=" in PRODUCT_INSTRUCTIONS
    assert "Do not use <cite> tags" in PRODUCT_INSTRUCTIONS
    assert '"story" | "product" | "mixed"' in ROUTE_INSTRUCTIONS
    assert "<action> tags are not claims" in PRODUCT_CHECK_INSTRUCTIONS
