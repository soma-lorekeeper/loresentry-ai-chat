import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

from app.content import Document, Memo, Property, Relation, Snapshot
from app.errors import ChatFailure
from app.model import Finished, ModelFailure, TextDelta, Usage
from app.prompt import Prompt

CH1 = str(uuid4())
CH2 = str(uuid4())
CH3 = str(uuid4())
LENA = str(uuid4())
GREENHOUSE = str(uuid4())
NOTE = str(uuid4())


def snapshot(project_id: str, size: int | None = None) -> Snapshot:
    manuscripts = (
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
    )
    settings = (
        Document(
            LENA,
            "레나 아르벨",
            "CHARACTER",
            "기억을 읽는 일은 언제나 대가를 요구한다.",
            7,
            properties=(Property("description", "은빛 항해단의 항해사"),),
        ),
        Document(GREENHOUSE, "북쪽 온실", "LOCATION", "유리 지붕이 금 간 온실.", 1),
    )
    relations = (Relation(*sorted((LENA, GREENHOUSE)), "처음 각성한 곳"),)
    memos = (
        Memo(
            NOTE, "PROJECT", None, "균열의 방향", "균열은 문이 아니라 기억의 방향이다."
        ),
    )
    computed = sum(
        len(doc.title) + len(doc.body_text) + sum(len(p.value) for p in doc.properties)
        for doc in (*manuscripts, *settings)
    )
    computed += sum(len(r.description) for r in relations)
    computed += sum(len(m.title or "") + len(m.body) for m in memos)
    return Snapshot(
        project_id=project_id,
        project_name="유리 정원의 기록",
        project_description="유리 정원을 지키는 이야기",
        manuscripts=manuscripts,
        settings=settings,
        relations=relations,
        memos=memos,
        size=computed if size is None else size,
    )


class FakeContent:
    def __init__(self, owner: UUID, project_id: UUID, size: int | None = None):
        self.owner = owner
        self.project_id = project_id
        self.data = snapshot(str(project_id), size)
        self.snapshots = 0

    def check(self, user_id: UUID, project_id: UUID) -> None:
        if user_id != self.owner or project_id != self.project_id:
            raise ChatFailure("PROJECT_NOT_FOUND")

    async def size(self, user_id: UUID, project_id: UUID) -> int:
        self.check(user_id, project_id)
        return self.data.size

    async def snapshot(self, user_id: UUID, project_id: UUID) -> Snapshot:
        self.check(user_id, project_id)
        self.snapshots += 1
        return self.data


Script = Callable[[Prompt], list[str]]


class FakeModel:
    name = "fake-model"

    def __init__(self, script: Script | list[str] | None = None):
        self.script = script or [
            "북쪽 문을 연 사람은 서윤이에요.",
            ' <cite id="D3" quote="서윤은 등불을 내려놓고"/>',
            "\n- 레나는 그 앞에 먼저 섰어요.",
            '<cite id="D2"/>',
        ]
        self.prompts: list[Prompt] = []
        self.gate: threading.Event | None = None
        self.fail_with: str | None = None
        self.usage = Usage(1200, 800, 0, 90, 30)

    async def stream(
        self, prompt: Prompt, cache_key: str
    ) -> AsyncIterator[TextDelta | Finished]:
        self.prompts.append(prompt)
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        if self.fail_with:
            code, self.fail_with = self.fail_with, None
            raise ModelFailure(code)
        chunks = self.script(prompt) if callable(self.script) else self.script
        for chunk in chunks:
            await asyncio.sleep(0)
            yield TextDelta(chunk)
        yield Finished(self.usage)


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
