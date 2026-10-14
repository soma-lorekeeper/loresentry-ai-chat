from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import httpx

from app.errors import ChatFailure

DOC_TYPES = {
    "MANUSCRIPT": "manuscript",
    "CHARACTER": "character",
    "LOCATION": "place",
    "ORGANIZATION": "organization",
    "ITEM": "item",
    "EVENT": "event",
    "WORLDVIEW": "worldview",
}


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

    @property
    def doc_type(self) -> str:
        return DOC_TYPES.get(self.folder_code, self.folder_code.lower())

    @property
    def is_manuscript(self) -> bool:
        return self.folder_code == "MANUSCRIPT"


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


@dataclass(frozen=True)
class Snapshot:
    project_id: str
    project_name: str
    project_description: str | None
    manuscripts: tuple[Document, ...]
    settings: tuple[Document, ...]
    relations: tuple[Relation, ...]
    memos: tuple[Memo, ...]
    size: int
    documents: dict[str, Document] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "documents",
            {doc.id: doc for doc in (*self.manuscripts, *self.settings)},
        )


def parse_snapshot(payload: dict[str, Any]) -> Snapshot:
    project = payload["project"]
    return Snapshot(
        project_id=project["id"],
        project_name=project["name"],
        project_description=project.get("description"),
        manuscripts=tuple(
            Document(
                id=item["id"],
                title=item["title"],
                folder_code="MANUSCRIPT",
                body_text=item["body_text"],
                revision_no=item["revision_no"],
                ordinal=item["ordinal"],
                episode_name=item.get("episode_name"),
                properties=tuple(
                    Property(prop["key"], prop["value"])
                    for prop in item.get("properties", ())
                ),
            )
            for item in payload["manuscripts"]
        ),
        settings=tuple(
            Document(
                id=item["id"],
                title=item["title"],
                folder_code=item["folder_code"],
                body_text=item["body_text"],
                revision_no=item["revision_no"],
                properties=tuple(
                    Property(prop["key"], prop["value"]) for prop in item["properties"]
                ),
            )
            for item in payload["settings"]
        ),
        relations=tuple(
            Relation(
                item["low_document_id"], item["high_document_id"], item["description"]
            )
            for item in payload["relations"]
        ),
        memos=tuple(
            Memo(
                id=item["id"],
                scope=item["scope"],
                document_id=item.get("document_id"),
                title=item.get("title"),
                body=item["body"],
            )
            for item in payload["memos"]
        ),
        size=payload["size"],
    )


class ContentSource(Protocol):
    async def size(self, user_id: UUID, project_id: UUID) -> int: ...

    async def snapshot(self, user_id: UUID, project_id: UUID) -> Snapshot: ...


class ContentClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get(self, user_id: UUID, path: str) -> dict[str, Any]:
        try:
            response = await self.client.get(path, headers={"X-User-Id": str(user_id)})
        except httpx.HTTPError as error:
            raise ChatFailure("CONTENT_UNAVAILABLE", type(error).__name__) from None
        if response.status_code == 404:
            raise ChatFailure("PROJECT_NOT_FOUND")
        if response.status_code != 200:
            raise ChatFailure("CONTENT_UNAVAILABLE", f"status {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ChatFailure("CONTENT_UNAVAILABLE", "invalid json") from None

    async def size(self, user_id: UUID, project_id: UUID) -> int:
        payload = await self._get(user_id, f"/internal/projects/{project_id}/chat-size")
        return int(payload["size"])

    async def snapshot(self, user_id: UUID, project_id: UUID) -> Snapshot:
        payload = await self._get(
            user_id, f"/internal/projects/{project_id}/chat-snapshot"
        )
        try:
            return parse_snapshot(payload)
        except (KeyError, TypeError) as error:
            raise ChatFailure("CONTENT_UNAVAILABLE", type(error).__name__) from None
