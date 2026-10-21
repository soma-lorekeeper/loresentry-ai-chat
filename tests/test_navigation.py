from uuid import uuid4

from app.navigation import (
    AnswerCheck,
    Review,
    Sufficiency,
    Summary,
    Turn,
    final_xml,
    map_xml,
    review_xml,
    short_ids,
)
from app.prompt import Exchange, Profile, Question, select_history
from tests.support import CH1, CH2, CH3, LENA, NOTE, FakeContent, short

ASK = Question("레나는 언제 처음 나와?", "ask", None, None)


def turn(**overrides) -> Turn:
    content = FakeContent(uuid4(), uuid4())
    args = dict(
        user_id=content.owner,
        project_id=content.project_id,
        chat_map=content.chat_map(),
        profile=Profile("서윤주", "ko"),
        question=ASK,
        history=[],
        summaries={},
        held_documents=[],
        held_memos=[],
    )
    args.update(overrides)
    return Turn(**args)


def test_short_ids_use_the_random_tail_and_grow_on_collision():
    first = "0192ab3f-0000-7000-8000-00000000abcd"
    second = "0192ab3f-0000-7000-8001-00000000abcd"
    third = "0192ab3f-0000-7000-8000-0000000000ef"

    assert short_ids([first, third]) == {first: "0000abcd", third: "000000ef"}
    assert short_ids([first, second]) == {
        first: "800000000000abcd",
        second: "800100000000abcd",
    }


def test_the_map_lists_every_node_without_bodies():
    current = turn(summaries={CH3: Summary(4, "서윤이 문을 민다.")})
    text = current.base_input()[0]["content"][0]["text"]

    assert '<author name="서윤주" locale="ko"/>' in text
    assert f'<node id="{short(CH1)}" kind="manuscript" number="1"' in text
    assert f'<node id="{short(CH3)}" kind="manuscript" number="3"' in text
    assert 'rev="5" summary_rev="4">' in text
    assert "<summary>서윤이 문을 민다.</summary>" in text
    assert '<property key="description">은빛 항해단의 항해사</property>' in text
    assert f'<node id="{short(NOTE)}" kind="memo" title="균열의 방향"' in text
    assert ">처음 각성한 곳</edge>" in text
    assert "서윤은 등불을 내려놓고" not in text


def test_the_same_material_gives_the_same_bytes():
    content = FakeContent(uuid4(), uuid4())
    chat_map = content.chat_map()
    ids = short_ids([node.id for node in chat_map.ordered])

    assert map_xml(chat_map, ids, {}) == map_xml(content.chat_map(), ids, {})


def test_context_blocks_carry_cache_breakpoints():
    blocks = turn().base_input()[0]["content"]

    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"} for block in blocks
    )
    assert blocks[1]["text"] == "<held/>"


def test_held_documents_come_after_the_map_and_history_after_them():
    content = FakeContent(uuid4(), uuid4())
    citation = {
        "kind": "document",
        "file_id": LENA,
        "title": "레나 아르벨",
        "doc_type": "character",
        "quote": None,
    }
    current = turn(
        held_documents=[content.docs[CH2]],
        history=[Exchange(ASK, "2화에서 처음 나와요. [[1]]", [citation])],
    )
    items = current.base_input()

    assert items[0]["content"][1]["text"].startswith(
        f'<held>\n<document id="{short(CH2)}" kind="manuscript" number="2"'
    )
    assert items[2] == {
        "role": "assistant",
        "content": f'2화에서 처음 나와요. <cite id="{short(LENA)}"/>',
    }
    assert items[-1]["content"][0]["text"].startswith("<turn>")


def test_the_question_carries_focus_quote_and_task():
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
    text = turn(question=question).base_input()[-1]["content"][0]["text"]

    assert f'<viewing id="{short(CH2)}" title="2화 · 북쪽 문"/>' in text
    assert (
        f'<quote id="{short(CH3)}" title="3화 · 균열의 밤">\n문을 밀었다\n</quote>'
        in text
    )
    assert "<task>Explain the quoted passage" in text


def test_the_sufficiency_input_holds_no_text_the_model_wrote():
    content = FakeContent(uuid4(), uuid4())
    current = turn(
        held_documents=[content.docs[CH3]],
        history=[
            Exchange(Question("앞 질문", "ask", None, None), "모델이 쓴 앞 답", [])
        ],
    )

    sufficiency = current.sufficiency_input()
    check = current.answer_input("이번 답")

    assert "앞 질문" in sufficiency and "모델이 쓴 앞 답" not in sufficiency
    assert "이번 답" not in sufficiency
    assert "서윤은 등불을 내려놓고" in sufficiency
    assert "모델이 쓴 앞 답" in check and "<answer>\n이번 답\n</answer>" in check


def test_reviews_list_missing_facts_and_problems():
    review = Review(
        2, Sufficiency(False, ["첫 각성의 근거"]), AnswerCheck(False, ["인용이 틀렸다"])
    )

    assert review_xml(review) == (
        '<review n="2" sufficient="false" answer_ok="false">\n'
        "<missing>첫 각성의 근거</missing>\n"
        "<problem>인용이 틀렸다</problem>\n"
        "</review>"
    )
    assert "Still missing: 첫 각성의 근거." in final_xml([review])
    assert "Still missing: not checked." in final_xml([])


def test_history_keeps_the_newest_turns_within_budget():
    turns = [
        Exchange(Question("q" * 10, "ask", None, None), "a" * 10, []) for _ in range(5)
    ]

    assert select_history(turns, 45) == turns[-2:]
