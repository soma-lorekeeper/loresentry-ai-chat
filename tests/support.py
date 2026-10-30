import asyncio
import json
import threading
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from app.content import ChatMap, Document, Fetched, Memo, Node, Property, Relation
from app.errors import ChatFailure
from app.model import ModelFailure, StepResult, ToolCall, Usage
from app.navigation import AnswerCheck, Sufficiency

CH1 = str(uuid4())
CH2 = str(uuid4())
CH3 = str(uuid4())
LENA = str(uuid4())
GREENHOUSE = str(uuid4())
NOTE = str(uuid4())


def short(target: str) -> str:
    return target.replace("-", "")[-8:]


def documents() -> dict[str, Document]:
    items = [
        Document(
            CH1,
            "1화 · 유리의 계절",
            "MANUSCRIPT",
            "서윤은 정원을 처음 보았다.",
            3,
            1,
            "Episode 1",
        ),
        Document(
            CH2,
            "2화 · 북쪽 문",
            "MANUSCRIPT",
            "레나는 북쪽 문 앞에 섰다.",
            2,
            2,
            "Episode 1",
        ),
        Document(
            CH3,
            "3화 · 균열의 밤",
            "MANUSCRIPT",
            "서윤은 등불을 내려놓고 문을 밀었다.",
            5,
            3,
            None,
        ),
        Document(
            LENA,
            "레나 아르벨",
            "CHARACTER",
            "기억을 읽는 일은 언제나 대가를 요구한다.",
            7,
            properties=(Property("description", "은빛 항해단의 항해사"),),
        ),
        Document(GREENHOUSE, "북쪽 온실", "LOCATION", "유리 지붕이 금 간 온실.", 1),
    ]
    return {doc.id: doc for doc in items}


class FakeContent:
    def __init__(self, owner: UUID, project_id: UUID):
        self.owner = owner
        self.project_id = project_id
        self.docs = documents()
        self.memos = {
            NOTE: Memo(
                NOTE,
                "PROJECT",
                None,
                "균열의 방향",
                "균열은 문이 아니라 기억의 방향이다.",
            )
        }
        self.relations = (Relation(*sorted((LENA, GREENHOUSE)), "처음 각성한 곳"),)
        self.reads: list[tuple[list[str], list[str]]] = []
        self.maps = 0

    def check(self, user_id: UUID, project_id: UUID) -> None:
        if user_id != self.owner or project_id != self.project_id:
            raise ChatFailure("PROJECT_NOT_FOUND")

    @property
    def total(self) -> int:
        return (
            sum(doc.chars for doc in self.docs.values())
            + sum(memo.chars for memo in self.memos.values())
            + sum(len(r.description) for r in self.relations)
        )

    def edit(self, target: str, body: str) -> None:
        doc = self.docs[target]
        self.docs[target] = replace(
            doc, body_text=body, revision_no=doc.revision_no + 1
        )

    def chat_map(self) -> ChatMap:
        def node(doc: Document) -> Node:
            return Node(
                id=doc.id,
                kind="DOCUMENT",
                folder_code=doc.folder_code,
                title=doc.title,
                chars=doc.chars,
                revision_no=doc.revision_no,
                ordinal=doc.ordinal,
                episode_name=doc.episode_name,
                properties=doc.properties,
            )

        docs = list(self.docs.values())
        return ChatMap(
            project_id=str(self.project_id),
            project_name="유리 정원의 기록",
            project_description="유리 정원을 지키는 이야기",
            manuscripts=tuple(node(doc) for doc in docs if doc.is_manuscript),
            settings=tuple(node(doc) for doc in docs if not doc.is_manuscript),
            memos=tuple(
                Node(
                    id=memo.id,
                    kind="MEMO",
                    folder_code="MEMO",
                    title=memo.title or "",
                    chars=memo.chars,
                    document_id=memo.document_id,
                )
                for memo in self.memos.values()
            ),
            relations=self.relations,
        )

    async def size(self, user_id: UUID, project_id: UUID) -> int:
        self.check(user_id, project_id)
        return self.total

    async def map(self, user_id: UUID, project_id: UUID) -> ChatMap:
        self.check(user_id, project_id)
        self.maps += 1
        return self.chat_map()

    async def documents(
        self,
        user_id: UUID,
        project_id: UUID,
        document_ids: list[str],
        memo_ids: list[str],
    ) -> Fetched:
        self.check(user_id, project_id)
        if document_ids or memo_ids:
            self.reads.append((list(document_ids), list(memo_ids)))
        found = [self.docs[i] for i in document_ids if i in self.docs]
        memos = [self.memos[i] for i in memo_ids if i in self.memos]
        missing = [i for i in (*document_ids, *memo_ids) if i not in self.docs]
        missing = [i for i in missing if i not in self.memos]
        return Fetched(tuple(found), tuple(memos), tuple(missing))


DEFAULT_ANSWER = (
    "북쪽 문을 연 사람은 서윤이에요. "
    f'<cite id="{short(CH3)}" quote="서윤은 등불을 내려놓고"/>'
    "\n- 레나는 그 앞에 먼저 섰어요."
    f'<cite id="{short(CH2)}"/>'
)


def read(*targets: str) -> tuple[str, list[str]]:
    return ("read", [short(target) for target in targets])


def answer(text: str = DEFAULT_ANSWER) -> tuple[str, str]:
    return ("answer", text)


class FakeModel:
    name = "fake-model"

    def __init__(
        self,
        steps: list[tuple[str, Any]] | None = None,
        sufficiency: list[Sufficiency] | None = None,
        checks: list[AnswerCheck] | None = None,
    ):
        self.steps = list(steps) if steps is not None else [read(CH3, CH2), answer()]
        self.sufficiency_script = list(sufficiency or [])
        self.check_script = list(checks or [])
        self.inputs: list[list[dict[str, Any]]] = []
        self.choices: list[str] = []
        self.sufficiency_inputs: list[str] = []
        self.check_inputs: list[str] = []
        self.gate: threading.Event | None = None
        self.fail_with: str | None = None
        self.usage = Usage(1200, 800, 0, 90, 30)
        self.calls = 0

    async def step(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str,
        cache_key: str,
    ) -> StepResult:
        self.inputs.append(json.loads(json.dumps(items)))
        self.choices.append(tool_choice)
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        if self.fail_with:
            code, self.fail_with = self.fail_with, None
            raise ModelFailure(code)
        self.calls += 1
        action = self.steps.pop(0) if self.steps else answer()
        if action[0] == "read" and tool_choice == "none":
            action = answer()
        if action[0] == "read":
            call = ToolCall(f"call_{self.calls}", "request_documents", action[1])
            item = {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": json.dumps({"ranking": action[1]}),
            }
            return StepResult(call, "", [item], self.usage)
        message = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": action[1]}],
        }
        return StepResult(None, action[1], [message], self.usage)

    async def sufficiency(
        self, instructions: str, text: str
    ) -> tuple[Sufficiency, Usage | None]:
        self.sufficiency_inputs.append(text)
        verdict = (
            self.sufficiency_script.pop(0)
            if self.sufficiency_script
            else Sufficiency(True, [])
        )
        return verdict, Usage(100, 0, 0, 10, 5)

    async def check_answer(
        self, instructions: str, text: str
    ) -> tuple[AnswerCheck, Usage | None]:
        self.check_inputs.append(text)
        verdict = (
            self.check_script.pop(0) if self.check_script else AnswerCheck(True, [])
        )
        return verdict, Usage(100, 0, 0, 10, 5)


def read_events(response) -> list[tuple[str, dict | None]]:
    events: list[tuple[str, dict | None]] = []
    name = None
    for line in response.iter_lines():
        if line.startswith(": "):
            events.append(("ping", None))
        elif line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            events.append((name or "message", json.loads(line[len("data: ") :])))
            name = None
    return events


def candidate(
    source: str,
    source_quote: str,
    quote: str,
    kind: str = "SETTING",
    summary: str = "요약이에요",
    direction: str = "방향이에요",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_id": short(source),
        "source_quote": source_quote,
        "quote": quote,
        "summary": summary,
        "direction": direction,
        "core": False,
    }


class FakeChecker:
    name = "fake-checker"

    def __init__(
        self,
        whole: list[dict[str, Any]] | None = None,
        by_setting: list[dict[str, Any]] | None = None,
        verdicts: dict[str, dict[str, str]] | None = None,
    ):
        self.whole = list(whole or [])
        self.by_setting = list(by_setting or [])
        self.verdicts = dict(verdicts or {})
        self.finds: dict[str, list[dict[str, Any]]] = {}
        self.judged: list[str] = []
        self.gate: threading.Event | None = None
        self.fail_with: str | None = None
        self.usage = Usage(1000, 400, 0, 50, 20)

    async def wait(self) -> None:
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)

    async def find(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        name: str,
        schema: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], Usage | None]:
        self.finds[name] = json.loads(json.dumps(items))
        await self.wait()
        if self.fail_with:
            raise ModelFailure(self.fail_with)
        settings = name == "setting_check_setting_candidates"
        return {"candidates": self.by_setting if settings else self.whole}, self.usage

    async def judge(
        self,
        instructions: str,
        items: list[dict[str, Any]],
        name: str,
        schema: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], Usage | None]:
        blocks = [block["text"] for block in items[0]["content"]]
        self.judged.append("\n".join(blocks))
        verdict = {
            "verdict": "CONFLICT",
            "confidence": "HIGH",
            "explanation": "",
            "quote_focus": "",
            "source_focus": "",
        }
        for quote, scripted in self.verdicts.items():
            if f"<quote>{quote}</quote>" in blocks[-1]:
                verdict.update(scripted)
        return verdict, self.usage
