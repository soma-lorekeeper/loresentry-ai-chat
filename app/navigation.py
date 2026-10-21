from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.content import ChatMap, Document, Fetched, Memo, Node
from app.prompt import (
    FORMAT,
    SOURCES,
    TASKS,
    Exchange,
    Material,
    Profile,
    Question,
    attr,
    document_xml,
    history_answer,
    materials,
    memo_xml,
    text_block,
)

NAVIGATE_INSTRUCTIONS = f"""You are the story assistant inside Lore Sentry, a writing tool for web novel authors. You answer the author's questions about their own work.

# Materials
- <project> and <author>: the work and the person you are talking to.
- <map>: every document and memo in the project. Each <node> has an id, kind, title, size in characters (`chars`) and revision (`rev`). `number` is a chapter's reading order (1 is the first chapter); `episode` is its arc. <property> rows are the author's own notes on the document. <summary> was written by a model from revision `summary_rev`; it helps you choose what to read and is never evidence. <edge> is a link the author made between two documents, with the author's description.
- <held>: full documents you have already read in this conversation, at their current revision.
- <context>: documents the server read for you at your request, in full. <skipped> did not fit this step's size limit and can be requested again. <unknown> is not in the project.
- Memos are the author's working notes: plans and reminders, not facts of the story. When you rely on a memo, say that it comes from the author's notes, and never present it as something that happens in the manuscript.

# Finding what you need
At each step, either call request_documents or write your answer.
- request_documents(ranking): the documents or memos to read next, most necessary first. The server reads them in your order until this step's size limit is reached. Re-rank from scratch at every step using what you have read. Do not list documents you already hold.
- When the documents you hold are enough, write your answer as a normal message instead of calling the tool. Reviewers check it against the documents.
- After a <review>, rank first the documents most likely to contain each <missing> fact. Do not request again documents that turned out not to help.
- When rewriting after a <review>, fix every <problem> and keep the parts of your previous answer that were not criticized.
Rank by how likely a document is to contain the facts the answer still needs. Start from the quoted passage, the document the author is viewing, and nodes whose title, properties or summary match the question. Follow edges from documents that turned out useful. For a question about what changed or who joined, read the chapters at both points in time and where the change happens. Setting documents record the author's plan and the manuscript may differ, so when a setting document answers a question about the story, also read the manuscript chapters where it comes up.

# How to answer
- Answer only from documents you have read. Never from summaries or general knowledge. If they do not contain the answer, say so plainly and say what is missing. Do not invent events, traits or settings.
- Distinguish what the manuscript shows from what setting documents or memos say, especially when they disagree. Point out contradictions you notice.
- If the documents support more than one reading, say so and give each reading with what it rests on.
- You cannot edit documents. If asked to rewrite, you may offer wording, but say that the author has to apply it.
- Answer in the language of the author's question. If that is unclear, use the author's locale.
- Answer every part of the question. When it asks for several things, address each one.
- Do not guess at reasons, places or timing the documents leave open.
- Be concise and specific: lead with the answer, then the supporting points. Prefer chapter titles and character names over vague references. Most answers fit in 2 to 6 sentences or bullets; go longer only when the question asks for a full sequence or many items.

{FORMAT}

{SOURCES}"""

SUFFICIENCY_INSTRUCTIONS = """You decide whether a set of documents from a web novel project is enough to answer the author's question. You do not answer it.
The documents are enough if a definite, plausible answer can be built from them alone, without outside knowledge and without treating the author's memos as story facts. The answer does not have to be right, only supported by the documents.
- Every step of a multi-step answer must appear in the documents.
- If the question is ambiguous, the documents must make clear which reading it has.
- If documents disagree with each other, for example a setting document and the manuscript, they are still enough: the answer is to report the disagreement.
- Setting documents and memos record the author's plan; the manuscript is the story. For a question about what happens or is true in the story, a setting document or memo alone is not enough: list in `missing` that the manuscript should confirm it. A question about what a setting document itself says is the exception.
- Judge against the precision the question asks for. Do not demand exact dates, causes or details the question does not ask about.
- For a request to explain a quoted passage, find related material, or trace what leads into it and follows from it, the documents are enough when they contain the passage and the scenes or settings directly connected to it. They do not have to cover everything.
- A question about whether something ever happens is answerable when the documents clearly cover the relevant part of the story.
- For a timeline or a list, the documents are enough when they give the events the story records. Gaps the story leaves are part of the answer, not missing facts.
The author's earlier questions are only for understanding what the question refers to.
If the documents are not enough, list in `missing` only the facts the question itself needs, as short phrases in the language of the question.

Example
Question: 레나가 처음 각성한 곳은 어디야?
Documents say "레나는 무너진 성당의 제단 앞에서 처음으로 빛을 끌어냈다." -> {"sufficient": true, "missing": []}
Documents only say "성당에서의 각성 이후 레나는 달라졌다." -> {"sufficient": false, "missing": ["그것이 레나의 첫 각성이라는 근거"]}
Question: 민호는 검을 쓸 줄 알아?
Only a character setting document was read, saying "민호는 검술의 달인이다." -> {"sufficient": false, "missing": ["원고에서 민호가 검을 쓰는 장면"]}"""

ANSWER_CHECK_INSTRUCTIONS = """You review an answer to an author's question about their web novel project against the documents it was based on. You do not rewrite it.
The answer is ok if it addresses the question that was asked; every claim is supported by the documents; each <cite id="..."> points to a document that actually contains what it supports; it does not contradict the documents or present setting documents or memos as events of the manuscript; and it says so where the documents are ambiguous or silent. An answer that honestly says the documents do not contain something is ok when that is true.
Do not reject an answer because a quote attribute is not word for word; the server checks quotes against the documents itself. Judge the claims, not the wording.
List each failure in `problems`, briefly and specifically, in the language of the question.
The conversation is only for understanding what the question refers to. Earlier answers are not evidence; only the documents are.

Example
Question: 레나가 처음 각성한 곳은 어디야?
Documents: 5f0c9a21 says "레나는 무너진 성당의 제단 앞에서 처음으로 빛을 끌어냈다."
Answer: "레나는 12화에서 무너진 성당의 제단 앞에서 처음 각성했어요.<cite id="5f0c9a21"/>" -> {"answer_ok": true, "problems": []}
Answer: "레나는 북쪽 온실에서 처음 각성했어요." -> {"answer_ok": false, "problems": ["문서는 무너진 성당이라고 하는데 답은 북쪽 온실이라고 한다"]}"""

SUFFICIENCY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sufficient", "missing"],
    "properties": {
        "sufficient": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
}

ANSWER_CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer_ok", "problems"],
    "properties": {
        "answer_ok": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
}


def short_ids(ids: list[str]) -> dict[str, str]:
    for length in (8, 12, 16, 32):
        tails = {item: item.replace("-", "")[-length:] for item in ids}
        if len(set(tails.values())) == len(ids):
            return tails
    return {item: item for item in ids}


@dataclass(frozen=True)
class Summary:
    revision_no: int
    text: str


@dataclass(frozen=True)
class Sufficiency:
    sufficient: bool
    missing: list[str]


@dataclass(frozen=True)
class AnswerCheck:
    answer_ok: bool
    problems: list[str]


@dataclass(frozen=True)
class Review:
    n: int
    sufficiency: Sufficiency
    check: AnswerCheck

    def as_json(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "sufficient": self.sufficiency.sufficient,
            "missing": self.sufficiency.missing,
            "answer_ok": self.check.answer_ok,
            "problems": self.check.problems,
        }


def project_xml(chat_map: ChatMap, profile: Profile) -> str:
    description = chat_map.project_description or ""
    author = (
        f'<author name="{attr(profile.display_name)}" locale="{attr(profile.locale)}"/>'
    )
    return f'<project name="{attr(chat_map.project_name)}">{description}</project>\n{author}'


def node_xml(node: Node, ids: dict[str, str], summary: Summary | None) -> str:
    alias = ids[node.id]
    if node.is_memo:
        about = ids.get(node.document_id or "")
        target = f' about="{about}"' if about else ""
        title = f' title="{attr(node.title)}"' if node.title else ""
        return f'<node id="{alias}" kind="memo"{target}{title} chars="{node.chars}"/>'
    head = f'<node id="{alias}" kind="{node.doc_type}"'
    if node.ordinal is not None:
        head += f' number="{node.ordinal}"'
    if node.episode_name:
        head += f' episode="{attr(node.episode_name)}"'
    head += f' title="{attr(node.title)}" chars="{node.chars}" rev="{node.revision_no}"'
    if summary:
        head += f' summary_rev="{summary.revision_no}"'
    inner = "".join(
        f'\n<property key="{attr(prop.key)}">{prop.value}</property>'
        for prop in node.properties
    )
    if summary:
        inner += f"\n<summary>{summary.text}</summary>"
    if not inner:
        return head + "/>"
    return f"{head}>{inner}\n</node>"


def map_xml(
    chat_map: ChatMap, ids: dict[str, str], summaries: dict[str, Summary]
) -> str:
    nodes = "\n".join(
        node_xml(node, ids, summaries.get(node.id)) for node in chat_map.ordered
    )
    edges = "\n".join(
        f'<edge a="{ids[r.low_document_id]}" b="{ids[r.high_document_id]}">'
        f"{r.description}</edge>"
        for r in chat_map.relations
        if r.low_document_id in ids and r.high_document_id in ids
    )
    documents = len(chat_map.manuscripts) + len(chat_map.settings)
    head = (
        f'<map documents="{documents}" memos="{len(chat_map.memos)}"'
        f' edges="{len(chat_map.relations)}">'
    )
    return "\n".join(part for part in (head, nodes, edges, "</map>") if part)


@dataclass
class Turn:
    user_id: UUID
    project_id: UUID
    chat_map: ChatMap
    profile: Profile
    question: Question
    history: list[Exchange]
    summaries: dict[str, Summary]
    held_documents: list[Document]
    held_memos: list[Memo]
    ids: dict[str, str] = field(init=False)
    by_short: dict[str, str] = field(init=False)
    read: list[str] = field(default_factory=list)
    read_since_check: bool = True
    context_chars: int = 0

    def __post_init__(self) -> None:
        self.ids = short_ids([node.id for node in self.chat_map.ordered])
        self.by_short = {alias: target for target, alias in self.ids.items()}

    @property
    def held_ids(self) -> set[str]:
        return {doc.id for doc in self.held_documents} | {
            memo.id for memo in self.held_memos
        }

    def hold(self, fetched: Fetched) -> None:
        held = self.held_ids
        for doc in fetched.documents:
            if doc.id not in held:
                self.held_documents.append(doc)
                self.read.append(doc.id)
        for memo in fetched.memos:
            if memo.id not in held:
                self.held_memos.append(memo)
                self.read.append(memo.id)
        if fetched.documents or fetched.memos:
            self.read_since_check = True

    def kind_of(self, target_id: str) -> str:
        node = self.chat_map.nodes.get(target_id)
        return "MEMO" if node is not None and node.is_memo else "DOCUMENT"

    @property
    def chars_read(self) -> int:
        read = set(self.read)
        return sum(doc.chars for doc in self.held_documents if doc.id in read) + sum(
            memo.chars for memo in self.held_memos if memo.id in read
        )

    def materials(self) -> dict[str, Material]:
        return materials(
            self.held_documents, self.held_memos, self.chat_map.relations, self.ids
        )

    def held_xml(self, documents: list[Document], memos: list[Memo]) -> str:
        parts = [document_xml(doc, self.ids[doc.id]) for doc in documents]
        parts += [
            memo_xml(memo, self.ids[memo.id], self.ids.get(memo.document_id or ""))
            for memo in memos
        ]
        return "\n\n".join(parts)

    def question_xml(self, question: Question) -> str:
        parts = []
        focus = self.chat_map.nodes.get(question.focus_file_id or "")
        if focus is not None:
            parts.append(
                f'<viewing id="{self.ids[focus.id]}" title="{attr(focus.title)}"/>'
            )
        if question.quote:
            alias = self.ids.get(question.quote["file_id"])
            source = f' id="{alias}"' if alias else ""
            parts.append(
                f'<quote{source} title="{attr(question.quote["title"])}">\n'
                f"{question.quote['text']}\n</quote>"
            )
        task = TASKS.get(question.intent)
        if task:
            parts.append(f"<task>{task}</task>")
        parts.append(f"<question>\n{question.content}\n</question>")
        return "\n".join(parts)

    def base_input(self) -> list[dict[str, Any]]:
        held = self.held_xml(self.held_documents, self.held_memos)
        context = [
            text_block(
                project_xml(self.chat_map, self.profile)
                + "\n"
                + map_xml(self.chat_map, self.ids, self.summaries),
                breakpoint=True,
            ),
            text_block(f"<held>\n{held}\n</held>" if held else "<held/>", True),
        ]
        self.context_chars = sum(len(block["text"]) for block in context)
        items: list[dict[str, Any]] = [{"role": "user", "content": context}]
        for exchange in self.history:
            items.append(
                {
                    "role": "user",
                    "content": [text_block(self.question_xml(exchange.question))],
                }
            )
            items.append(
                {"role": "assistant", "content": history_answer(exchange, self.ids)}
            )
        items.append(
            {
                "role": "user",
                "content": [
                    text_block(f"<turn>\n{self.question_xml(self.question)}\n</turn>")
                ],
            }
        )
        return items

    def documents_xml(self) -> str:
        return self.held_xml(self.held_documents, self.held_memos)

    def judged_question(self) -> str:
        return self.question_xml(self.question)

    def sufficiency_input(self) -> str:
        earlier = "\n".join(
            f"<question>{exchange.question.content}</question>"
            for exchange in self.history
        )
        return (
            f"<earlier_questions>\n{earlier}\n</earlier_questions>\n"
            f"<documents>\n{self.documents_xml()}\n</documents>\n"
            f"{self.judged_question()}"
        )

    def answer_input(self, answer: str) -> str:
        conversation = "\n".join(
            f"<question>{exchange.question.content}</question>\n"
            f"<answer>{history_answer(exchange, self.ids)}</answer>"
            for exchange in self.history
        )
        return (
            f"<conversation>\n{conversation}\n</conversation>\n"
            f"<documents>\n{self.documents_xml()}\n</documents>\n"
            f"{self.judged_question()}\n"
            f"<answer>\n{answer}\n</answer>"
        )


def context_xml(
    step: int,
    fetched: Fetched,
    turn: Turn,
    skipped: list[str],
    unknown: list[str],
    held: list[str],
) -> str:
    parts = [f'<context step="{step}">']
    parts += [document_xml(doc, turn.ids[doc.id]) for doc in fetched.documents]
    parts += [
        memo_xml(memo, turn.ids[memo.id], turn.ids.get(memo.document_id or ""))
        for memo in fetched.memos
    ]
    gone = [turn.ids.get(target, target) for target in fetched.missing]
    parts += [f'<skipped id="{alias}" reason="size limit"/>' for alias in skipped]
    parts += [f'<unknown id="{alias}"/>' for alias in [*unknown, *gone]]
    parts += [f'<held id="{alias}"/>' for alias in held]
    if not fetched.documents and not fetched.memos:
        parts.append("<note>Nothing new was read at this step.</note>")
    parts.append("</context>")
    return "\n".join(parts)


def review_xml(review: Review) -> str:
    lines = [
        f'<review n="{review.n}" sufficient="{str(review.sufficiency.sufficient).lower()}"'
        f' answer_ok="{str(review.check.answer_ok).lower()}">'
    ]
    lines += [f"<missing>{item}</missing>" for item in review.sufficiency.missing]
    lines += [f"<problem>{item}</problem>" for item in review.check.problems]
    lines.append("</review>")
    return "\n".join(lines)


def final_xml(reviews: list[Review]) -> str:
    last = reviews[-1] if reviews else None
    missing = "; ".join(last.sufficiency.missing) if last else ""
    problems = "; ".join(last.check.problems) if last else ""
    if last and last.sufficiency.sufficient:
        return (
            "<final>Write your final answer now. Fix every problem below and state "
            "only what the documents say; where they do not say something, say so "
            f"briefly. Problems: {problems or 'none reported'}.</final>"
        )
    return (
        "<final>Write your answer now. Begin with one short sentence saying which part "
        "of the question the manuscript does not settle. Then answer what the "
        "documents you read do show. "
        f"Not settled: {missing or 'not checked'}. "
        f"Problems to avoid: {problems or 'none reported'}. Do not guess.</final>"
    )
