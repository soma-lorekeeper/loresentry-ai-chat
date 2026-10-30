from dataclasses import dataclass
from typing import Any

from app.content import Document
from app.prompt import attr, text_block
from app.setting_check.findings import Candidate, Reading

MAX_CANDIDATES = 20
JUDGE_EARLIER_CHARS = 30_000

LANGUAGE = """# Language
Write summary and direction in the language of the chapter; if that is unclear, use the author's locale. In Korean, use the polite 해요 style, as in these examples:
summary: 북쪽 문은 낡은 열쇠로만 열리는데, 서윤이 열쇠 없이 문을 밀어요
direction: 12화까지 서윤이 낡은 열쇠를 손에 넣는 장면이 없어요."""

JUDGE_LANGUAGE = """# Language
Write explanation in the language of the chapter; if that is unclear, use the author's locale. In Korean, use the polite 해요 style, as in this example:
‘북쪽 온실’ 설정은 북쪽 문이 낡은 열쇠 없이는 열리지 않는다고 정해 두었어요. 그런데 12화에서 서윤은 열쇠를 꺼내지 않고 손으로 문을 밀어 열어요. 11화까지 서윤이 낡은 열쇠를 받거나 줍는 장면이 없어서, 이 문이 열린 까닭을 원고 안에서 찾을 수 없어요. 열쇠를 손에 넣는 장면이나 문이 다른 힘으로 열리는 까닭이 앞에 있어야 두 내용이 함께 성립해요."""

CANDIDATE_FIELDS = """- quote: the sentence in <chapter> that contradicts it, copied character for character. One sentence or clause, at most 200 characters.
- summary: one sentence that names both sides.
- direction: one short sentence saying what the story is missing or which passage is involved. Never write a corrected sentence for the author.
The server checks both quotes against the documents and throws away a candidate whose quote is not copied exactly. Do not paraphrase, shorten with "…", or join words from separate places."""

WHOLE_INSTRUCTIONS = f"""You check one chapter of a web novel against what its author has already established, and list the places where the chapter contradicts it. You only report; you never rewrite the story.

# Materials
- <settings>: the author's setting documents, each with its kind, title, the author's <property> rows and <body>. Setting documents are the ground truth. <core> lines are passages the author marked as core settings: facts the story must keep. Check every one of them against the chapter.
- <earlier>: the chapters before the checked one, in reading order (`number` 1 is the first chapter).
- <chapter>: the chapter being checked.
- <author>: the author's locale.

# What counts
Report a place where something the chapter states or shows cannot be true together with what a setting document or an earlier chapter establishes:
- SETTING: the chapter contradicts a setting document, for example a trait, ability, rule, appearance, age, relationship, place, object or past event.
- MANUSCRIPT: the chapter contradicts what an earlier chapter showed or said.
Do not report:
- disagreements between setting documents, or between earlier chapters, that the checked chapter takes no part in;
- spelling, naming style, tone or wording;
- details the setting documents and earlier chapters never mention; silence is not a contradiction;
- changes the story itself shows or states: time passing, an event in between, a character growing or deciding differently, a lie, a mistake, a rumour, a belief, a dream or a flashback. A character who simply acts against a setting, with no reason shown, is a contradiction, not growth.
Names, nicknames and titles given in document titles and properties refer to the same person, place or thing.
When you are unsure whether a place is a contradiction, report it; a reviewer checks every candidate.

# Each candidate
- kind: SETTING or MANUSCRIPT.
- source_id: the id of the setting document or earlier chapter it contradicts. Use only ids that appear in the materials.
- source_quote: the passage in that document that establishes the fact, copied character for character from its <body>, a <core> line or a <property> value. One sentence or clause, at most 200 characters.
{CANDIDATE_FIELDS}
- core: true when source_quote comes from a <core> line.
- related_ids: the ids of up to three other earlier chapters the contradiction also depends on, for example the chapter that gives an age or a date the source does not; an empty list when the source alone shows it.
Report each contradiction once, in the order it appears in the chapter, at most {MAX_CANDIDATES}. If there is none, return an empty list.

{LANGUAGE}"""

SETTINGS_INSTRUCTIONS = f"""You check one chapter of a web novel against its author's setting documents, one document at a time. You only report; you never rewrite the story.

# Materials
- <settings>: the author's setting documents, each with its id, kind, title, the author's <property> rows, <core> lines and <body>. Setting documents are the ground truth. <core> lines are facts the author marked as must-keep.
- <chapter>: the chapter being checked.
- <author>: the author's locale.

# What counts
Go through the setting documents one by one, and within each document fact by fact: every <core> line, property and sentence of the body. Report a fact when something the chapter states or shows cannot be true together with it. Names, nicknames and titles given in document titles and properties refer to the same person, place or thing. Do not report facts the chapter does not touch, details the settings never mention, differences in wording or naming style, or changes the chapter itself shows or states: time passing, an event that changes the fact, a lie, a mistake, a rumour, a belief, a dream or a flashback. A character who simply acts against a fact, with no reason shown, contradicts it. When you are unsure, report it; a reviewer checks every candidate.

# Each candidate
- source_id: the id of the setting document.
- source_quote: the fact the chapter contradicts, copied character for character from its <body>, a <core> line or a <property> value. One sentence or clause, at most 200 characters.
{CANDIDATE_FIELDS}
Report each contradiction once, in the order it appears in the chapter, at most {MAX_CANDIDATES}. If there is none, return an empty list.

{LANGUAGE}"""

JUDGE_INSTRUCTIONS = f"""You review one candidate contradiction in a chapter of a web novel. Another reviewer found it after reading the setting documents and every earlier chapter; you decide whether it holds. You never rewrite the story.

# Materials
- <settings>: every setting document, with its properties (names, nicknames, roles) and <core> lines. Use them to tell who or what a sentence refers to, and for facts the candidate depends on.
- <earlier>: the most recent chapters before the checked one, as many as fit.
- <chapter>: the chapter being checked, in full.
- <source>: the setting document or earlier chapter that the candidate says the chapter contradicts. Setting documents are the ground truth. <core> lines are facts the author marked as must-keep.
- <related>: older chapters the first reviewer says the contradiction also depends on, when they are not in <earlier>.
- <verdicts>: what the author said about earlier candidates that used this same source: INTENDED (the author meant it that way) or NOT_CONFLICT (the author says it is not a contradiction), with the author's reason when given. When the author has already explained the same point, follow the author's reason.
- <candidate>: the sentence from the chapter (<quote>), the passage from the source (<source_quote>), and the first reviewer's summary and direction.
- <author>: the author's locale.

# Verdict
Judge what the texts say, not what could be true. Read the chapter as its readers would: a person, place or thing called by a title, nickname, role or description in the materials is the same one. A contradiction stands unless the materials account for it; do not dismiss it with an explanation the story never gives, such as a second key, another notebook, a ghost or an event nobody mentions. Sentences happen in the story's present unless the text marks them otherwise; a marker such as a flashback ("ten years ago"), a memory or a dream covers only the passage it introduces.
- CONFLICT: on the most natural reading, the two cannot both be true at that point in the story, and nothing in the materials accounts for it.
- CHANGED_OVER_TIME: the materials show or state the change: time passes, something happens in between, a character grows or decides differently. A character simply acting against the setting is not a change.
- CHARACTER_PERSPECTIVE: the sentence in <quote> itself is a character's words, lie, mistake, belief, rumour, dream or point of view, not a fact of the story. A rumour or belief elsewhere in the chapter does not turn narration into one.
- INSUFFICIENT: read in context, the quotes do not say what the candidate claims, or the contradiction depends on a fact that appears nowhere in the materials.
confidence: HIGH when both sides are stated plainly; LOW when the contradiction rests on an interpretation, such as which person or object a sentence means. A contradiction on a plausible reading is still CONFLICT, with LOW confidence.
explanation: three or four sentences, addressed to the author, that let them see why this is a contradiction without opening either document. Say what the source establishes and where (the document's title, or the chapter number); what the chapter states or shows at <quote>; why the two cannot both be true at that point in the story; and what the story never shows that would let both hold, such as a missing scene or reason. Use the concrete names, objects, numbers and events from the texts. Do not copy either quote in full, do not judge the writing and never write a corrected sentence.
quote_focus: the shortest phrase in <quote> that carries the contradiction, copied character for character from <quote>; an empty string when no single phrase does. For example, 세 번째 순찰 in "서윤은 세 번째 순찰에서야 알아차렸다."
source_focus: the same for <source_quote>: the shortest phrase that carries the contradiction, copied character for character from <source_quote>; an empty string when no single phrase does. For example, 다섯 번째 순찰 in "다섯 번째 순찰을 마친 새벽이었다."
For any verdict other than CONFLICT, explanation may be one short sentence.

{JUDGE_LANGUAGE}"""

FIELDS: dict[str, Any] = {
    "source_id": {"type": "string"},
    "source_quote": {"type": "string"},
    "quote": {"type": "string"},
    "summary": {"type": "string"},
    "direction": {"type": "string"},
}


def candidates_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "maxItems": MAX_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(properties),
                    "properties": properties,
                },
            }
        },
    }


WHOLE_SCHEMA = candidates_schema(
    {
        "kind": {"type": "string", "enum": ["SETTING", "MANUSCRIPT"]},
        **FIELDS,
        "core": {"type": "boolean"},
        "related_ids": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
    }
)

SETTINGS_SCHEMA = candidates_schema(FIELDS)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "verdict",
        "confidence",
        "explanation",
        "quote_focus",
        "source_focus",
    ],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [
                "CONFLICT",
                "CHANGED_OVER_TIME",
                "CHARACTER_PERSPECTIVE",
                "INSUFFICIENT",
            ],
        },
        "confidence": {"type": "string", "enum": ["HIGH", "LOW"]},
        "explanation": {"type": "string"},
        "quote_focus": {"type": "string"},
        "source_focus": {"type": "string"},
    },
}


@dataclass(frozen=True)
class Note:
    verdict: str
    source_quote: str
    reason: str | None


def author_xml(locale: str) -> str:
    return f'<author locale="{attr(locale)}"/>'


def document_xml(doc: Document, alias: str) -> str:
    if doc.is_manuscript:
        head = (
            f'<document id="{alias}" kind="manuscript" number="{doc.ordinal}"'
            + (f' episode="{attr(doc.episode_name)}"' if doc.episode_name else "")
            + f' title="{attr(doc.title)}">'
        )
    else:
        head = (
            f'<document id="{alias}" kind="{doc.doc_type}" title="{attr(doc.title)}">'
        )
    props = "".join(
        f'\n<property key="{attr(prop.key)}">{prop.value}</property>'
        for prop in doc.properties
    )
    core = "".join(f"\n<core>{fact}</core>" for fact in doc.core_facts)
    return f"{head}{props}{core}\n<body>\n{doc.body_text}\n</body>\n</document>"


def chapter_xml(reading: Reading) -> str:
    target = reading.target
    return (
        f'<chapter id="{reading.ids[target.id]}" number="{target.ordinal}"'
        f' title="{attr(target.title)}">\n{target.body_text}\n</chapter>'
    )


def section(name: str, parts: list[str]) -> str:
    return (
        f"<{name}>\n" + "\n\n".join(parts) + f"\n</{name}>" if parts else f"<{name}/>"
    )


def user(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": blocks}]


def whole_input(reading: Reading, locale: str) -> list[dict[str, Any]]:
    settings = [document_xml(doc, reading.ids[doc.id]) for doc in reading.settings]
    earlier = [document_xml(doc, reading.ids[doc.id]) for doc in reading.earlier]
    return user(
        [
            text_block(section("settings", settings), breakpoint=True),
            text_block(section("earlier", earlier), breakpoint=True),
            text_block(f"{author_xml(locale)}\n{chapter_xml(reading)}"),
        ]
    )


def settings_input(reading: Reading, locale: str) -> list[dict[str, Any]]:
    settings = [document_xml(doc, reading.ids[doc.id]) for doc in reading.settings]
    return user(
        [
            text_block(section("settings", settings), breakpoint=True),
            text_block(f"{author_xml(locale)}\n{chapter_xml(reading)}"),
        ]
    )


def verdicts_xml(notes: list[Note]) -> str:
    parts = [
        f'<verdict value="{note.verdict}">\n<source_quote>{note.source_quote}'
        "</source_quote>"
        + (f"\n<reason>{note.reason}</reason>" if note.reason else "")
        + "\n</verdict>"
        for note in notes
    ]
    return section("verdicts", parts)


def recent(reading: Reading) -> list[Document]:
    kept: list[Document] = []
    room = JUDGE_EARLIER_CHARS
    for doc in reversed(reading.earlier):
        if len(doc.body_text) > room:
            break
        kept.append(doc)
        room -= len(doc.body_text)
    return kept[::-1]


def related_xml(reading: Reading, candidate: Candidate) -> str:
    shown = {doc.id for doc in recent(reading)}
    related = [
        document_xml(reading.documents[doc_id], reading.ids[doc_id])
        for doc_id in candidate.related_ids
        if doc_id != candidate.source_id
        and doc_id not in shown
        and doc_id in reading.documents
        and reading.documents[doc_id].is_manuscript
    ]
    return f"{section('related', related)}\n" if related else ""


def candidate_xml(candidate: Candidate) -> str:
    return (
        f'<candidate kind="{candidate.kind}">\n'
        f"<quote>{candidate.quote}</quote>\n"
        f"<source_quote>{candidate.source_quote}</source_quote>\n"
        f"<summary>{candidate.summary}</summary>\n"
        f"<direction>{candidate.direction}</direction>\n"
        "</candidate>"
    )


def judge_input(
    reading: Reading, candidate: Candidate, notes: list[Note], locale: str
) -> list[dict[str, Any]]:
    source = reading.documents[candidate.source_id]
    settings = [document_xml(doc, reading.ids[doc.id]) for doc in reading.settings]
    earlier = [document_xml(doc, reading.ids[doc.id]) for doc in recent(reading)]
    return user(
        [
            text_block(
                f"{section('settings', settings)}\n{section('earlier', earlier)}\n"
                f"{chapter_xml(reading)}",
                breakpoint=True,
            ),
            text_block(
                f"<source>\n{document_xml(source, reading.ids[source.id])}\n</source>\n"
                f"{related_xml(reading, candidate)}{verdicts_xml(notes)}\n"
                f"{author_xml(locale)}"
            ),
            text_block(candidate_xml(candidate)),
        ]
    )
