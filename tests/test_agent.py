import asyncio
import time
from uuid import uuid4

from app.agent import Agent, pack
from app.navigation import AnswerCheck, Sufficiency, Turn
from app.prompt import Profile, Question
from app.settings import Settings
from tests.support import CH1, CH2, CH3, LENA, FakeContent, FakeModel, answer, read

CHARS = {"a": 10, "b": 30, "c": 5, "big": 100}
BY_SHORT = {name: name for name in CHARS}


def test_pack_keeps_the_order_and_fills_what_is_left():
    picked = pack(["a", "b", "c"], BY_SHORT, CHARS, set(), 20)

    assert picked.targets == ["a", "c"]
    assert picked.skipped == ["b"]


def test_pack_reads_a_first_document_larger_than_the_limit_alone():
    picked = pack(["big", "a", "c"], BY_SHORT, CHARS, set(), 20)

    assert picked.targets == ["big"]
    assert picked.skipped == ["a", "c"]


def test_pack_reports_held_unknown_and_repeated_ids():
    picked = pack(["a", "zzz", "a", "b"], BY_SHORT, CHARS, {"a"}, 50)

    assert picked.targets == ["b"]
    assert picked.held == ["a"]
    assert picked.unknown == ["zzz"]


def make(model: FakeModel, **overrides) -> tuple[Agent, Turn, FakeContent]:
    content = FakeContent(uuid4(), uuid4())
    settings = Settings(**overrides)
    current = Turn(
        user_id=content.owner,
        project_id=content.project_id,
        chat_map=content.chat_map(),
        profile=Profile(),
        question=Question("북쪽 문은 누가 열었어?", "ask", None),
        history=[],
        summaries={},
        held_documents=[],
        held_memos=[],
    )
    return Agent(model, content, settings), current, content


def run(agent: Agent, current: Turn, seconds: float = 170.0):
    return asyncio.run(agent.run(current, time.monotonic() + seconds))


def test_a_passing_answer_ends_the_loop():
    model = FakeModel()
    agent, current, _ = make(model)
    outcome = run(agent, current)

    assert outcome.level == "SUPPORTED"
    assert outcome.steps == 2 and outcome.judge_calls == 2
    assert current.read == [CH3, CH2]


def test_a_rewrite_without_new_reads_reuses_the_sufficiency_verdict():
    model = FakeModel(
        [read(CH3), answer("틀린 답"), answer()],
        checks=[AnswerCheck(False, ["틀렸다"]), AnswerCheck(True, [])],
    )
    agent, current, _ = make(model)
    outcome = run(agent, current)

    assert outcome.level == "SUPPORTED"
    assert len(model.sufficiency_inputs) == 1
    assert len(model.check_inputs) == 2


def test_new_reads_ask_for_sufficiency_again():
    model = FakeModel(
        [read(CH2), answer("모자란 답"), read(CH3), answer()],
        sufficiency=[Sufficiency(False, ["문을 연 사람"]), Sufficiency(True, [])],
    )
    agent, current, _ = make(model)
    outcome = run(agent, current)

    assert outcome.level == "SUPPORTED"
    assert len(model.sufficiency_inputs) == 2
    assert "서윤은 등불을 내려놓고" in model.sufficiency_inputs[1]


def test_the_last_step_must_answer_and_a_failed_review_ends_not_enough():
    model = FakeModel(
        [read(CH1), read(CH2), read(LENA)],
        sufficiency=[Sufficiency(False, ["문을 연 사람"])] * 3,
    )
    agent, current, _ = make(model, max_steps=3)
    outcome = run(agent, current)

    assert model.choices == ["auto", "auto", "none", "none"]
    assert outcome.level == "NOT_ENOUGH"
    assert outcome.reviews[-1].sufficiency.missing == ["문을 연 사람"]


def test_two_steps_that_read_nothing_force_an_answer():
    model = FakeModel([read(CH3), read(CH3), read(CH3), answer()])
    agent, current, _ = make(model, max_steps=5)
    run(agent, current)

    assert model.choices[:4] == ["auto", "auto", "auto", "none"]


def test_too_little_time_skips_straight_to_the_last_answer():
    model = FakeModel()
    agent, current, content = make(model, answer_reserve_seconds=75)
    outcome = run(agent, current, seconds=60)

    assert outcome.level == "NOT_ENOUGH"
    assert model.choices == ["none"]
    assert content.reads == []
    assert model.inputs[0][-1]["content"][0]["text"].startswith("<final>")


def test_a_sufficient_answer_that_keeps_failing_review_is_rewritten_not_disclaimed():
    model = FakeModel(
        [read(CH3), answer("첫 답"), answer("둘째 답"), answer("고친 답")],
        checks=[AnswerCheck(False, ["장소가 틀렸다"])] * 3,
    )
    agent, current, _ = make(model, max_steps=2)
    outcome = run(agent, current)

    final = model.inputs[-1][-1]["content"][0]["text"]
    assert outcome.level == "SUPPORTED"
    assert "Fix every problem below" in final and "장소가 틀렸다" in final
    assert "does not settle" not in final
