from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import httpx

from app.errors import ChatFailure
from app.logs import AppLogger, LogField, LogFlow, skip_entry_log

DOC_TYPES = {
    "MANUSCRIPT": "manuscript",
    "CHARACTER": "character",
    "LOCATION": "place",
    "ORGANIZATION": "organization",
    "ITEM": "item",
    "EVENT": "event",
    "WORLDVIEW": "worldview",
}


@skip_entry_log("Only maps a folder code")
def doc_type(folder_code: str) -> str:
    return DOC_TYPES.get(folder_code, folder_code.lower())


@dataclass(frozen=True)
class Property:
    key: str
    value: str


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    folder_code: str
    body_text: str
    revision_no: int
    ordinal: int | None = None
    episode_name: str | None = None
    properties: tuple[Property, ...] = ()
    core_facts: tuple[str, ...] = ()

    @property
    def doc_type(self) -> str:
        return doc_type(self.folder_code)

    @property
    def is_manuscript(self) -> bool:
        return self.folder_code == "MANUSCRIPT"

    @property
    def chars(self) -> int:
        return (
            len(self.title)
            + len(self.body_text)
            + sum(len(prop.value) for prop in self.properties)
        )


@dataclass(frozen=True)
class Relation:
    low_document_id: str
    high_document_id: str
    description: str


@dataclass(frozen=True)
class Memo:
    id: str
    scope: str
    document_id: str | None
    title: str | None
    body: str

    @property
    def chars(self) -> int:
        return len(self.title or "") + len(self.body)


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    folder_code: str
    title: str
    chars: int
    revision_no: int | None = None
    ordinal: int | None = None
    episode_name: str | None = None
    properties: tuple[Property, ...] = ()
    document_id: str | None = None
    updated_at: str | None = None

    @property
    def is_memo(self) -> bool:
        return self.kind == "MEMO"

    @property
    def doc_type(self) -> str:
        return "memo" if self.is_memo else doc_type(self.folder_code)


@dataclass(frozen=True)
class ChatMap:
    project_id: str
    project_name: str
    project_description: str | None
    manuscripts: tuple[Node, ...]
    settings: tuple[Node, ...]
    memos: tuple[Node, ...]
    relations: tuple[Relation, ...]
    nodes: dict[str, Node] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nodes",
            {
                node.id: node
                for node in (*self.manuscripts, *self.settings, *self.memos)
            },
        )

    @property
    def ordered(self) -> tuple[Node, ...]:
        return (*self.manuscripts, *self.settings, *self.memos)


@dataclass(frozen=True)
class Fetched:
    documents: tuple[Document, ...]
    memos: tuple[Memo, ...]
    missing: tuple[str, ...]


@skip_entry_log("Only converts the content response")
def properties_of(item: dict[str, Any]) -> tuple[Property, ...]:
    return tuple(Property(prop["key"], prop["value"]) for prop in item["properties"])


@skip_entry_log("Only converts the content response")
def core_facts_of(item: dict[str, Any]) -> tuple[str, ...]:
    facts = item.get("core_facts")
    if not isinstance(facts, list):
        return ()
    return tuple(fact for fact in facts if isinstance(fact, str) and fact.strip())


@skip_entry_log("Only converts the content response")
def parse_map(payload: dict[str, Any]) -> ChatMap:
    project = payload["project"]
    return ChatMap(
        project_id=project["id"],
        project_name=project["name"],
        project_description=project.get("description"),
        manuscripts=tuple(
            Node(
                id=item["id"],
                kind="DOCUMENT",
                folder_code="MANUSCRIPT",
                title=item["title"],
                chars=item["chars"],
                revision_no=item["revision_no"],
                ordinal=item["ordinal"],
                episode_name=item.get("episode_name"),
                properties=properties_of(item),
                updated_at=item.get("updated_at"),
            )
            for item in payload["manuscripts"]
        ),
        settings=tuple(
            Node(
                id=item["id"],
                kind="DOCUMENT",
                folder_code=item["folder_code"],
                title=item["title"],
                chars=item["chars"],
                revision_no=item["revision_no"],
                properties=properties_of(item),
                updated_at=item.get("updated_at"),
            )
            for item in payload["settings"]
        ),
        memos=tuple(
            Node(
                id=item["id"],
                kind="MEMO",
                folder_code="MEMO",
                title=item.get("title") or "",
                chars=item["chars"],
                document_id=item.get("document_id"),
                updated_at=item.get("updated_at"),
            )
            for item in payload["memos"]
        ),
        relations=tuple(
            Relation(
                item["low_document_id"], item["high_document_id"], item["description"]
            )
            for item in payload["relations"]
        ),
    )


@skip_entry_log("Only converts the content response")
def parse_documents(payload: dict[str, Any]) -> Fetched:
    documents = [
        Document(
            id=item["id"],
            title=item["title"],
            folder_code="MANUSCRIPT",
            body_text=item["body_text"],
            revision_no=item["revision_no"],
            ordinal=item["ordinal"],
            episode_name=item.get("episode_name"),
            properties=properties_of(item),
        )
        for item in payload["manuscripts"]
    ]
    documents += [
        Document(
            id=item["id"],
            title=item["title"],
            folder_code=item["folder_code"],
            body_text=item["body_text"],
            revision_no=item["revision_no"],
            properties=properties_of(item),
            core_facts=core_facts_of(item),
        )
        for item in payload["settings"]
    ]
    memos = tuple(
        Memo(
            id=item["id"],
            scope=item["scope"],
            document_id=item.get("document_id"),
            title=item.get("title"),
            body=item["body"],
        )
        for item in payload["memos"]
    )
    return Fetched(tuple(documents), memos, tuple(payload["missing"]))


class ContentSource(Protocol):
    async def size(self, user_id: UUID, project_id: UUID) -> int: ...

    async def map(self, user_id: UUID, project_id: UUID) -> ChatMap: ...

    async def documents(
        self,
        user_id: UUID,
        project_id: UUID,
        document_ids: list[str],
        memo_ids: list[str],
    ) -> Fetched: ...


class ContentClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _call(
        self, user_id: UUID, method: str, path: str, body: Any = None
    ) -> dict[str, Any]:
        request = self.client.build_request(
            method, path, json=body, headers={"X-User-Id": str(user_id)}
        )
        # 결과 줄(content call completed·failed)에 대상 호스트·주소(쿼리 제외), 응답 상태, 걸린 시간이 남는다
        with LogFlow.call("content", str(request.url)) as call:
            try:
                response = await self.client.send(request)
            except httpx.HTTPError as error:
                call.fail(type(error).__name__)
                raise ChatFailure("CONTENT_UNAVAILABLE", type(error).__name__) from None
            call.status(response.status_code)
        if response.status_code == 404:
            raise ChatFailure("PROJECT_NOT_FOUND")
        if response.status_code != 200:
            raise ChatFailure("CONTENT_UNAVAILABLE", f"status {response.status_code}")
        try:
            return response.json()
        except ValueError as error:
            unreadable(error)
            raise ChatFailure("CONTENT_UNAVAILABLE", "invalid json") from None

    async def size(self, user_id: UUID, project_id: UUID) -> int:
        AppLogger.current().info(
            "content chat size get call", LogField.project_id(project_id)
        )
        payload = await self._call(
            user_id, "GET", f"/internal/projects/{project_id}/chat-size"
        )
        return int(payload["size"])

    async def map(self, user_id: UUID, project_id: UUID) -> ChatMap:
        AppLogger.current().info(
            "content chat map get call", LogField.project_id(project_id)
        )
        payload = await self._call(
            user_id, "GET", f"/internal/projects/{project_id}/chat-map"
        )
        try:
            return parse_map(payload)
        except (KeyError, TypeError) as error:
            unreadable(error)
            raise ChatFailure("CONTENT_UNAVAILABLE", type(error).__name__) from None

    async def documents(
        self,
        user_id: UUID,
        project_id: UUID,
        document_ids: list[str],
        memo_ids: list[str],
    ) -> Fetched:
        AppLogger.current().info(
            "content chat documents get call",
            LogField.project_id(project_id),
            LogField.param("document_ids", document_ids),
            LogField.param("memo_ids", memo_ids),
        )
        if not document_ids and not memo_ids:
            return Fetched((), (), ())
        payload = await self._call(
            user_id,
            "POST",
            f"/internal/projects/{project_id}/chat-documents",
            {"document_ids": document_ids, "memo_ids": memo_ids},
        )
        try:
            return parse_documents(payload)
        except (KeyError, TypeError) as error:
            unreadable(error)
            raise ChatFailure("CONTENT_UNAVAILABLE", type(error).__name__) from None


def unreadable(error: Exception) -> None:
    """content가 200으로 답했지만 본문을 읽지 못했다. 실패 원인 줄이고, 요청 결과 줄에는 CONTENT_UNAVAILABLE이 남는다."""
    AppLogger.current().warn(
        "content response unreadable", LogField.error_type(type(error).__name__)
    )
