from app.guide import (
    guide_locale,
    guide_xml,
    load,
    parse_actions,
    strip_markers,
    valid_action,
    with_markers,
)


def test_both_guides_load_with_the_same_topics():
    ko, en = load("ko"), load("en")

    assert [t.id for t in ko.topics] == [t.id for t in en.topics]
    assert ko.topic("setting-check") is not None
    assert ko.has_section("writing", "import")
    assert load("fr").locale == "ko"


def test_guide_xml_carries_overview_topics_sections_and_actions():
    xml = guide_xml(load("ko"))

    assert xml.startswith('<guide locale="ko">')
    assert "<overview>" in xml and "Lore Sentry" in xml
    assert '<topic id="refresh"' in xml
    assert '<section id="run"' in xml
    assert '<action id="view:setting-check">' in xml
    assert "<actions>" in xml


def test_locale_follows_hangul_then_profile():
    assert guide_locale("가져오기는 어떻게 해?", "en") == "ko"
    assert guide_locale("How do I import?", "en") == "en"
    assert guide_locale("How do I import?", None) == "ko"


def test_actions_are_validated_against_the_guide():
    guide = load("ko")

    assert valid_action("tour", guide)
    assert valid_action("import", guide)
    assert valid_action("view:graph", guide)
    assert valid_action("guide:refresh", guide)
    assert valid_action("guide:refresh#run", guide)
    assert not valid_action("view:bogus", guide)
    assert not valid_action("guide:refresh#nope", guide)
    assert not valid_action("guide:nothing", guide)
    assert not valid_action("press:import", guide)


def test_parse_actions_strips_tags_and_keeps_valid_ids_in_order():
    text = (
        '가져오기를 누르세요. <action id="import"/>\n'
        "- txt와 md만 받아요.\n\n\n"
        '<action id="guide:writing#import"/> <action id="import"/>'
        '<action id="view:nope"/><action id="tour"></action><action id="view:help"/>'
    )

    clean, actions = parse_actions(text, load("ko"))

    assert clean == "가져오기를 누르세요.\n- txt와 md만 받아요."
    assert actions == ["import", "guide:writing#import", "tour"]
    assert with_markers(clean, actions) == (
        "가져오기를 누르세요.\n- txt와 md만 받아요.\n"
        "[[action:import]][[action:guide:writing#import]][[action:tour]]"
    )
    assert strip_markers(with_markers(clean, actions)) == clean
    assert with_markers("답", []) == "답"
