from dataclasses import dataclass, field
from html import escape
from typing import Any

from app.content import Document, Snapshot
from app.text import MARKER

INSTRUCTIONS = """You are the story assistant inside Lore Sentry, a writing tool for web novel authors. You answer the author's questions about their own work.

# Materials
The first user message holds the whole project:
- <project> and <author>: the work and the person you are talking to.
- <document kind="manuscript">: chapters of the story. `number` is the reading order (1 is the first chapter). `episode` is the arc the chapter belongs to.
- <document> with another kind (character, place, organization, item, event, worldview): setting documents the author wrote, with <property> rows and a <body>.
- <relation>: a link the author made between two documents, with the author's description of the link.
- <memo>: the author's working notes. Notes are plans and reminders, not facts of the story. When you rely on a note, say that it comes from the author's notes, and never present a note as something that happens in the manuscript.
Recent chapters are placed after the setting documents and notes; together with the earlier chapters they form the complete manuscript.

# How to answer
- Answer only from these materials. If they do not contain the answer, say so plainly and say what is missing. Do not invent events, traits or settings.
- Distinguish what the manuscript shows from what setting documents or notes say, especially when they disagree. Point out contradictions you notice.
- You cannot edit documents. If asked to rewrite, you may offer wording in your answer, but say that the author has to apply it.
- Answer in the language of the author's question. If that is unclear, use the author's locale.
- Be concise and specific: lead with the answer, then the supporting points. Prefer chapter titles and character names over vague references.

# Format
- Plain text. One line is one paragraph. A line starting with "- " is a bullet item.
- The only emphasis allowed is **bold**. No headings, tables, links, numbered lists or code blocks.
- Write the answer directly. Do not wrap it in tags such as <final> or <answer>.

# Sources
After each sentence or bullet that relies on the materials, add a source tag:
<cite id="D12" quote="exact words"/>
- `id` is the id of the document (D…) or memo (M…) you used. Use only ids that appear in the materials.
- `quote` is a short excerpt, at most 200 characters, copied character for character from that document's body or properties, from a relation description involving it, or from the memo. It must not contain double quotes; pick another excerpt or leave the attribute out.
- Several tags may follow one sentence. Never place a tag inside bold text."""

TASKS = {
    "ask": None,
    "explain": "Explain the quoted passage: what happens in it, who is involved, what they do, and why it matters in the story so far.",
    "related": "List the setting documents connected to the quoted passage, explicit relations first, and where the same people, places or objects appear elsewhere in the manuscript.",
    "flow": "Describe how the quoted passage connects to what comes before and after it: the chapters and scenes that lead into it and the ones that follow from it.",
}


@dataclass(frozen=True)
class Profile:
    display_name: str | None = None
    locale: str | None = None


@dataclass(frozen=True)
class Question:
    content: str
    intent: str
    quote: dict[str, Any] | None
    focus_file_id: str | None


@dataclass(frozen=True)
class Exchange:
    question: Question
    answer: str
    citations: list[dict[str, Any]]


@dataclass(frozen=True)
class Material:
    alias: str
    kind: str
    target_id: str
    title: str
    doc_type: str | None
    texts: tuple[str, ...]

    def citation(self, quote: str | None) -> dict[str, Any]:
        if self.kind == "memo":
            return {
                "kind": "memo",
                "memo_id": self.target_id,
                "title": self.title,
                "quote": quote,
            }
        return {
            "kind": "document",
            "file_id": self.target_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "quote": quote,
        }


@dataclass
class Prompt:
    instructions: str
    input: list[dict[str, Any]]
    materials: dict[str, Material]
    history_turns: int = 0
    context_chars: int = 0
    by_target: dict[str, str] = field(default_factory=dict)


def attr(value: str | int | None) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def text_block(text: str, breakpoint: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "input_text", "text": text}
    if breakpoint:
        block["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return block


class Aliases:
    def __init__(self, snapshot: Snapshot):
        self.of: dict[str, str] = {}
        for index, doc in enumerate((*snapshot.manuscripts, *snapshot.settings), 1):
            self.of[doc.id] = f"D{index}"
        for index, memo in enumerate(snapshot.memos, 1):
            self.of[memo.id] = f"M{index}"


def materials(snapshot: Snapshot, aliases: Aliases) -> dict[str, Material]:
    linked: dict[str, list[str]] = {}
    for relation in snapshot.relations:
        if relation.description:
            for doc_id in (relation.low_document_id, relation.high_document_id):
                linked.setdefault(doc_id, []).append(relation.description)
    found: dict[str, Material] = {}
    for doc in (*snapshot.manuscripts, *snapshot.settings):
        alias = aliases.of[doc.id]
        found[alias] = Material(
            alias=alias,
            kind="document",
            target_id=doc.id,
            title=doc.title,
            doc_type=doc.doc_type,
            texts=(
                doc.title,
                doc.body_text,
                *(prop.value for prop in doc.properties),
                *linked.get(doc.id, ()),
            ),
        )
    for memo in snapshot.memos:
        alias = aliases.of[memo.id]
        found[alias] = Material(
            alias=alias,
            kind="memo",
            target_id=memo.id,
            title=memo.title or "",
            doc_type=None,
            texts=(memo.title or "", memo.body),
        )
    return found


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
    return f"{head}{props}\n<body>\n{doc.body_text}\n</body>\n</document>"


def project_xml(snapshot: Snapshot, profile: Profile) -> str:
    description = snapshot.project_description or ""
    author = (
        f'<author name="{attr(profile.display_name)}" locale="{attr(profile.locale)}"/>'
    )
    return f'<project name="{attr(snapshot.project_name)}">{description}</project>\n{author}'


def relations_xml(snapshot: Snapshot, aliases: Aliases) -> str:
    if not snapshot.relations:
        return "<relations/>"
    rows = "".join(
        f'\n<relation a="{aliases.of[r.low_document_id]}" b="{aliases.of[r.high_document_id]}">'
        f"{r.description}</relation>"
        for r in snapshot.relations
        if r.low_document_id in aliases.of and r.high_document_id in aliases.of
    )
    return f"<relations>{rows}\n</relations>"


def memo_xml(snapshot: Snapshot, aliases: Aliases) -> str:
    parts = []
    for memo in snapshot.memos:
        about = (
            f' about="{aliases.of[memo.document_id]}"'
            if memo.document_id in aliases.of
            else ""
        )
        title = f' title="{attr(memo.title)}"' if memo.title else ""
        parts.append(
            f'<memo id="{aliases.of[memo.id]}"{about}{title}>\n{memo.body}\n</memo>'
        )
    return "\n".join(parts)


def history_answer(exchange: Exchange, by_target: dict[str, str]) -> str:
    def replace(match) -> str:
        index = int(match.group(1)) - 1
        if not 0 <= index < len(exchange.citations):
            return ""
        citation = exchange.citations[index]
        target = citation.get("file_id") or citation.get("memo_id")
        alias = by_target.get(target or "")
        return f'<cite id="{alias}"/>' if alias else ""

    return MARKER.sub(replace, exchange.answer)


def question_xml(
    question: Question, by_target: dict[str, str], snapshot: Snapshot
) -> str:
    parts = []
    focus = snapshot.documents.get(question.focus_file_id or "")
    if focus:
        parts.append(
            f'<viewing id="{by_target[focus.id]}" title="{attr(focus.title)}"/>'
        )
    if question.quote:
        quote = question.quote
        alias = by_target.get(quote["file_id"])
        source = f' id="{alias}"' if alias else ""
        parts.append(
            f'<quote{source} title="{attr(quote["title"])}">\n{quote["text"]}\n</quote>'
        )
    task = TASKS.get(question.intent)
    if task:
        parts.append(f"<task>{task}</task>")
    parts.append(f"<question>\n{question.content}\n</question>")
    return "\n".join(parts)


def select_history(exchanges: list[Exchange], budget: int) -> list[Exchange]:
    chosen: list[Exchange] = []
    used = 0
    for exchange in reversed(exchanges):
        cost = len(exchange.question.content) + len(exchange.answer)
        if exchange.question.quote:
            cost += len(exchange.question.quote["text"])
        if used + cost > budget:
            break
        chosen.append(exchange)
        used += cost
    chosen.reverse()
    return chosen


def build_prompt(
    snapshot: Snapshot,
    profile: Profile,
    history: list[Exchange],
    question: Question,
    recent_count: int,
    history_budget: int,
) -> Prompt:
    aliases = Aliases(snapshot)
    found = materials(snapshot, aliases)
    by_target = {material.target_id: alias for alias, material in found.items()}

    split = max(0, len(snapshot.manuscripts) - recent_count)
    earlier, recent = snapshot.manuscripts[:split], snapshot.manuscripts[split:]

    stable = [project_xml(snapshot, profile)]
    stable += [document_xml(doc, aliases.of[doc.id]) for doc in earlier]
    stable += [document_xml(doc, aliases.of[doc.id]) for doc in snapshot.settings]
    stable.append(relations_xml(snapshot, aliases))
    notes = memo_xml(snapshot, aliases)
    if notes:
        stable.append(notes)

    context = [text_block("\n\n".join(stable), breakpoint=True)]
    if recent:
        context.append(
            text_block(
                "\n\n".join(document_xml(doc, aliases.of[doc.id]) for doc in recent),
                breakpoint=True,
            )
        )
    items: list[dict[str, Any]] = [{"role": "user", "content": context}]

    kept = select_history(history, history_budget)
    for position, exchange in enumerate(kept):
        last = position == len(kept) - 1
        items.append(
            {
                "role": "user",
                "content": [
                    text_block(
                        question_xml(exchange.question, by_target, snapshot),
                        breakpoint=last,
                    )
                ],
            }
        )
        items.append(
            {"role": "assistant", "content": history_answer(exchange, by_target)}
        )

    items.append(
        {
            "role": "user",
            "content": [text_block(question_xml(question, by_target, snapshot))],
        }
    )
    return Prompt(
        instructions=INSTRUCTIONS,
        input=items,
        materials=found,
        history_turns=len(kept),
        context_chars=snapshot.size,
        by_target=by_target,
    )
