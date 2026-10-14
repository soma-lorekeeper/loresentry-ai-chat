from uuid import uuid4

from app.prompt import Exchange, Profile, Question, build_prompt, select_history
from tests.support import CH2, CH3, LENA, snapshot

ASK = Question("레나는 언제 처음 나와?", "ask", None, None)


def build(**overrides):
    data = overrides.pop("data", snapshot(str(uuid4())))
    args = dict(
        snapshot=data,
        profile=Profile("서윤주", "ko"),
        history=[],
        question=ASK,
        recent_count=1,
        history_budget=10_000,
    )
    args.update(overrides)
    return build_prompt(**args)


def blocks(prompt):
    return prompt.input[0]["content"]


def test_stable_context_comes_before_recent_chapters():
    prompt = build()
    stable, recent = (block["text"] for block in blocks(prompt))

    assert stable.index('<author name="서윤주" locale="ko"/>') < stable.index(
        'number="1"'
    )
    assert stable.index('number="2"') < stable.index('kind="character"')
    assert stable.index('kind="place"') < stable.index("<relations>")
    assert stable.index("<relations>") < stable.index("<memo")
    assert 'number="3"' not in stable
    assert recent.startswith('<document id="D3" kind="manuscript" number="3"')


def test_context_blocks_carry_cache_breakpoints():
    prompt = build()

    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        for block in blocks(prompt)
    )
    assert "prompt_cache_breakpoint" not in prompt.input[-1]["content"][0]


def test_same_material_gives_the_same_prompt():
    data = snapshot(str(uuid4()))

    assert build(data=data).input == build(data=data).input


def test_relations_and_memos_use_aliases():
    stable = blocks(build())[0]["text"]

    assert '<relation a="D' in stable and ">처음 각성한 곳</relation>" in stable
    assert '<memo id="M1" title="균열의 방향">' in stable
    assert '<property key="description">은빛 항해단의 항해사</property>' in stable


def test_question_carries_focus_quote_and_task():
    question = Question(
        "이 부분을 설명해 줘",
        "explain",
        {
            "file_id": CH3,
            "title": "3화 · 균열의 밤",
            "doc_type": "manuscript",
            "text": "문을 밀었다",
        },
        CH2,
    )
    text = build(question=question).input[-1]["content"][0]["text"]

    assert '<viewing id="D2" title="2화 · 북쪽 문"/>' in text
    assert '<quote id="D3" title="3화 · 균열의 밤">\n문을 밀었다\n</quote>' in text
    assert "<task>Explain the quoted passage" in text
    assert text.endswith("<question>\n이 부분을 설명해 줘\n</question>")


def test_history_answers_turn_markers_back_into_tags():
    citation = {
        "kind": "document",
        "file_id": LENA,
        "title": "레나 아르벨",
        "doc_type": "character",
        "quote": None,
    }
    history = [Exchange(ASK, "2화에서 처음 나와요. [[1]]", [citation])]
    prompt = build(history=history)

    assert prompt.input[1]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }
    assert prompt.input[2] == {
        "role": "assistant",
        "content": '2화에서 처음 나와요. <cite id="D4"/>',
    }
    assert prompt.history_turns == 1


def test_history_keeps_the_newest_turns_within_budget():
    turns = [
        Exchange(Question("q" * 10, "ask", None, None), "a" * 10, []) for _ in range(5)
    ]

    assert select_history(turns, 45) == turns[-2:]
