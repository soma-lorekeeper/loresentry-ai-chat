from dataclasses import dataclass
from typing import Any

from app.content import Document
from app.prompt import attr, text_block
from app.setting_check.findings import Candidate, Reading

MAX_CANDIDATES = 20

LANGUAGE = """# Language
Write summary and direction in the language of the chapter; if that is unclear, use the author's locale. In Korean, use the polite 해요 style, as in these examples:
summary: 북쪽 문은 낡은 열쇠로만 열리는데, 서윤이 열쇠 없이 문을 밀어요
direction: 12화까지 서윤이 낡은 열쇠를 손에 넣는 장면이 없어요."""

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
- changes the story itself accounts for: time passing, an event in between, a character growing or deciding differently, a lie, a mistake, a rumour, a belief, a dream or a flashback.
Names, nicknames and titles given in document titles and properties refer to the same person, place or thing.
When you are unsure whether a place is a contradiction, report it; a reviewer checks every candidate.

# Each candidate
- kind: SETTING or MANUSCRIPT.
- source_id: the id of the setting document or earlier chapter it contradicts. Use only ids that appear in the materials.
- source_quote: the passage in that document that establishes the fact, copied character for character from its <body>, a <core> line or a <property> value. One sentence or clause, at most 200 characters.
{CANDIDATE_FIELDS}
- core: true when source_quote comes from a <core> line.
Report each contradiction once, in the order it appears in the chapter, at most {MAX_CANDIDATES}. If there is none, return an empty list.

{LANGUAGE}"""

CORE_INSTRUCTIONS = f"""You check one chapter of a web novel against the core settings its author marked: facts the story must keep. You only report; you never rewrite the story.

# Materials
- <facts>: each <fact> is one core setting, with the id (`source`), kind and title of the setting document it belongs to.
- <chapter>: the chapter being checked.
- <author>: the author's locale.

# What counts
Go through the facts one by one. Report a fact when something the chapter states or shows cannot be true together with it. Do not report facts the chapter does not touch, differences in wording or naming style, or changes the chapter itself accounts for: a lie, a mistake, a belief, a dream, a flashback, or an event that changes the fact. When you are unsure, report it; a reviewer checks every candidate.

# Each candidate
- source_id: the `source` id of the fact.
- source_quote: the part of the fact that the chapter contradicts, copied character for character; the whole fact if it is short. At most 200 characters.
{CANDIDATE_FIELDS}
Report each contradiction once, in the order it appears in the chapter, at most {MAX_CANDIDATES}. If there is none, return an empty list.

{LANGUAGE}"""

JUDGE_INSTRUCTIONS = f"""You review one candidate contradiction in a chapter of a web novel. Another reviewer found it after reading the setting documents and every earlier chapter; you decide whether it holds. You never rewrite the story.

# Materials
- <chapter>: the chapter being checked, in full.
- <source>: the setting document or earlier chapter that the candidate says the chapter contradicts. Setting documents are the ground truth. <core> lines are facts the author marked as must-keep.
- <verdicts>: what the author said about earlier candidates that used this same source: INTENDED (the author meant it that way) or NOT_CONFLICT (the author says it is not a contradiction), with the author's reason when given. When the author has already explained the same point, follow the explanation.
- <candidate>: the sentence from the chapter (<quote>), the passage from the source (<source_quote>), and the first reviewer's summary and direction.
- <author>: the author's locale.

# Verdict
- CONFLICT: the two cannot both be true at that point in the story, and nothing in the materials accounts for it.
- CHANGED_OVER_TIME: the story changes it on purpose: time passes, something happens in between, a character grows or decides differently.
- CHARACTER_PERSPECTIVE: it is a character's lie, mistake, belief, rumour, dream or point of view, not a fact of the story.
- INSUFFICIENT: the materials do not show a disagreement: the quotes do not say what the candidate claims, or the claim needs facts that are not there.
confidence: HIGH when both sides are stated plainly; LOW when the contradiction rests on an interpretation.
summary: one sentence that names both sides, true to the texts.
direction: one short sentence saying what the story is missing or which passage is involved. Never write a corrected sentence for the author.
For any verdict other than CONFLICT, summary and direction may be short.

{LANGUAGE}"""

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
    }
)

CORE_SCHEMA = candidates_schema(FIELDS)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "confidence", "summary", "direction"],
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
        "summary": {"type": "string"},
        "direction": {"type": "string"},
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


def core_input(reading: Reading, locale: str) -> list[dict[str, Any]]:
    facts = [
        f'<fact source="{reading.ids[doc.id]}" kind="{doc.doc_type}"'
        f' title="{attr(doc.title)}">{fact}</fact>'
        for doc in reading.settings
        for fact in doc.core_facts
    ]
    facts_xml = "<facts>\n" + "\n".join(facts) + "\n</facts>"
    return user(
        [text_block(f"{facts_xml}\n{author_xml(locale)}\n{chapter_xml(reading)}")]
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
    return user(
        [
            text_block(chapter_xml(reading), breakpoint=True),
            text_block(
                f"<source>\n{document_xml(source, reading.ids[source.id])}\n</source>\n"
                f"{verdicts_xml(notes)}\n{author_xml(locale)}"
            ),
            text_block(candidate_xml(candidate)),
        ]
    )
