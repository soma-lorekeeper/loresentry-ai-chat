from dataclasses import dataclass
from html import escape
from typing import Any

from app.content import Document, Memo, Relation
from app.text import MARKER

FORMAT = """# Format
- Plain text. One line is one paragraph. A line starting with "- " is a bullet item.
- The only emphasis allowed is **bold**. No headings, tables, links, numbered lists or code blocks.
- Write the answer directly. Do not wrap it in tags such as <final> or <answer>."""

SOURCES = """# Sources
After each sentence or bullet that relies on the materials, add a source tag:
<cite id="5f0c9a21" quote="exact words"/>
- `id` is the id of the document or memo you used. Use only ids that appear in the materials.
- `quote` is a short excerpt, at most 200 characters, copied character for character from that document's body or properties, from a relation description involving it, or from the memo. It must not contain double quotes; pick another excerpt or leave the attribute out.
- Several tags may follow one sentence. Never place a tag inside bold text.
- Cite only documents and memos you have read. Never cite a node you only saw in the map."""

TASKS = {
    "ask": None,
    "explain": "Explain the quoted passage: what happens in it, who is involved, what they do, and why it matters in the story so far.",
    "related": "Find what the quoted passage is connected to: the setting documents about the people, places, objects or clues it mentions (explicit relations first), and the other chapters where those same things appear. Stay on what the passage is about; do not list unrelated documents.",
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


def attr(value: str | int | None) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def text_block(text: str, breakpoint: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "input_text", "text": text}
    if breakpoint:
        block["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return block


def materials(
    documents: list[Document],
    memos: list[Memo],
    relations: tuple[Relation, ...],
    aliases: dict[str, str],
) -> dict[str, Material]:
    linked: dict[str, list[str]] = {}
    for relation in relations:
        if relation.description:
            for doc_id in (relation.low_document_id, relation.high_document_id):
                linked.setdefault(doc_id, []).append(relation.description)
    found: dict[str, Material] = {}
    for doc in documents:
        alias = aliases[doc.id]
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
    for memo in memos:
        alias = aliases[memo.id]
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
            + f' title="{attr(doc.title)}" rev="{doc.revision_no}">'
        )
    else:
        head = (
            f'<document id="{alias}" kind="{doc.doc_type}" title="{attr(doc.title)}"'
            f' rev="{doc.revision_no}">'
        )
    props = "".join(
        f'\n<property key="{attr(prop.key)}">{prop.value}</property>'
        for prop in doc.properties
    )
    return f"{head}{props}\n<body>\n{doc.body_text}\n</body>\n</document>"


def memo_xml(memo: Memo, alias: str, about: str | None) -> str:
    target = f' about="{about}"' if about else ""
    title = f' title="{attr(memo.title)}"' if memo.title else ""
    return f'<memo id="{alias}"{target}{title}>\n{memo.body}\n</memo>'


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
