from uuid import uuid4

from app.citations import CitationStream, Cited, Delta
from app.prompt import materials
from tests.support import CH1, CH2, CH3, GREENHOUSE, LENA, NOTE, FakeContent

ALIASES = {CH1: "D1", CH2: "D2", CH3: "D3", LENA: "D4", GREENHOUSE: "D5", NOTE: "M1"}


def stream() -> CitationStream:
    content = FakeContent(uuid4(), uuid4())
    return CitationStream(
        materials(
            list(content.docs.values()),
            list(content.memos.values()),
            content.relations,
            ALIASES,
        )
    )


def run(parser: CitationStream, chunks: list[str]) -> list[Delta | Cited]:
    out = []
    for chunk in chunks:
        out += parser.feed(chunk)
    return out + parser.finish()


def text_of(events) -> str:
    return "".join(event.text for event in events if isinstance(event, Delta))


def test_tag_split_across_chunks_becomes_a_marker_after_its_citation():
    parser = stream()
    events = run(
        parser, ["문을 밀었어요. <ci", 'te id="D3" quo', 'te="등불을 내려놓고"/> 끝']
    )

    cited = [event for event in events if isinstance(event, Cited)]
    assert text_of(events) == "문을 밀었어요. [[1]] 끝"
    assert cited[0].citation == {
        "kind": "document",
        "file_id": CH3,
        "title": "3화 · 균열의 밤",
        "doc_type": "manuscript",
        "quote": "등불을 내려놓고",
    }
    assert events.index(cited[0]) < next(
        i for i, e in enumerate(events) if isinstance(e, Delta) and e.text == "[[1]]"
    )


def test_quote_not_in_the_source_is_dropped_but_the_source_stays():
    parser = stream()
    run(parser, ['레나예요.<cite id="D4" quote="지어낸 문장"/>'])

    assert parser.citations[0]["file_id"] == LENA
    assert parser.citations[0]["quote"] is None


def test_quote_is_checked_against_properties_and_relations_with_spacing_folded():
    parser = stream()
    run(
        parser,
        [
            '<cite id="D4" quote="은빛  항해단의 항해사"/>',
            '<cite id="D4" quote="처음 각성한 곳"/>',
        ],
    )

    assert [c["quote"] for c in parser.citations] == [
        "은빛 항해단의 항해사",
        "처음 각성한 곳",
    ]


def test_unknown_ids_leave_no_marker():
    parser = stream()
    events = run(parser, ['없는 문서.<cite id="D99"/> 다음'])

    assert text_of(events) == "없는 문서. 다음"
    assert parser.citations == []


def test_repeated_sources_share_one_index():
    parser = stream()
    events = run(parser, ['가<cite id="D3"/> 나<cite id="D3"/>'])

    assert text_of(events) == "가[[1]] 나[[1]]"
    assert len(parser.citations) == 1


def test_memo_sources_are_marked_as_memos():
    parser = stream()
    run(parser, ['메모에 적어 두셨어요.<cite id="M1" quote="기억의 방향이다"/>'])

    assert parser.citations == [
        {
            "kind": "memo",
            "memo_id": NOTE,
            "title": "균열의 방향",
            "quote": "기억의 방향이다",
        }
    ]


def test_angle_bracket_inside_a_quote_does_not_end_the_tag():
    parser = stream()
    events = run(parser, ['a<cite id="D3" quote="x > y"/>b'])

    assert text_of(events) == "a[[1]]b"


def test_plain_angle_brackets_and_unfinished_tags():
    parser = stream()
    events = run(parser, ["1 < 2 이고 <", "끝", ' <cite id="D3"'])

    assert text_of(events) == "1 < 2 이고 <끝 "
    assert parser.content == "1 < 2 이고 <끝"


def test_wrapper_tags_around_the_answer_are_removed():
    parser = stream()
    events = run(parser, ["<fin", "al>답이에요.", '<cite id="D3"/></fi', "nal>"])

    assert text_of(events) == "답이에요.[[1]]"


def test_text_that_only_looks_like_a_tag_is_kept():
    parser = stream()
    events = run(parser, ["<finals> 결승 <c", "at> 고양이 <fin"])

    assert text_of(events) == "<finals> 결승 <cat> 고양이 <fin"


def test_an_unclosed_wrapper_tag_at_the_very_end_is_dropped():
    parser = stream()
    events = run(parser, ['답이에요. <cite id="D3"/></fin', "al"])

    assert text_of(events) == "답이에요. [[1]]"
    assert parser.content == "답이에요. [[1]]"
