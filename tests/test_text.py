from app.text import chat_preview, chat_title, default_title, search_text, snippet

LENA = {
    "kind": "document",
    "file_id": "f",
    "title": "레나 아르벨",
    "doc_type": "character",
    "quote": None,
}


def test_title_keeps_short_questions():
    assert chat_title("  북쪽 문은\n누가 열었지?  ") == "북쪽 문은 누가 열었지?"


def test_title_truncates_long_questions_to_24_characters():
    title = chat_title("가" * 30)

    assert title == "가" * 23 + "…"
    assert len(title) == 24


def test_default_title_follows_locale():
    assert default_title("en") == "New conversation"
    assert default_title(None) == "새 대화"


def test_preview_names_an_inline_source_and_drops_trailing_ones():
    content = (
        "주요 인물 가운데 [[1]]에 이어 둔 문서가 가장 많아요. [[1]]\n- **조직** 항해단"
    )

    assert chat_preview(content, [LENA]) == (
        "주요 인물 가운데 레나 아르벨에 이어 둔 문서가 가장 많아요. 조직 항해단"
    )


def test_preview_is_cut_at_80_characters():
    preview = chat_preview("나" * 100, [])

    assert preview == "나" * 80 + "…"


def test_search_text_includes_quote_and_source_titles():
    text = search_text("근거는 [[1]] 문서예요.", {"text": "북쪽 문"}, [LENA])

    assert text == "북쪽 문 근거는 레나 아르벨 문서예요."


def test_snippet_marks_the_first_match_case_insensitively():
    found = snippet("The Glass Garden keeps every memory of the north door", "garden")

    assert found == {
        "before": "The Glass ",
        "match": "Garden",
        "after": " keeps every memory of the north door",
    }


def test_snippet_returns_none_without_a_match():
    assert snippet("abc", "z") is None
