import json
from dataclasses import dataclass

TYPES = {"FileChanged", "ProjectDeleted"}
GONE = {"TRASHED", "DELETED"}


@dataclass(frozen=True)
class Upsert:
    document_id: str
    project_id: str
    revision_no: int
    folder_code: str
    title: str
    body: str


@dataclass(frozen=True)
class Remove:
    document_id: str


@dataclass(frozen=True)
class RemoveProject:
    project_id: str


Change = Upsert | Remove | RemoveProject


def header_map(headers: list[tuple[str, bytes | None]] | None) -> dict[str, str]:
    return {
        name: value.decode("utf-8") if value is not None else ""
        for name, value in headers or ()
    }


def parse(headers: dict[str, str], value: bytes | None) -> Change | None:
    kind = headers.get("ce_type")
    if kind not in TYPES or value is None:
        return None
    payload = json.loads(value)
    if kind == "ProjectDeleted":
        return RemoveProject(str(payload["project_id"]))
    file_id = str(payload["file_id"])
    document = payload.get("file")
    if payload.get("change_type") in GONE or document is None:
        return Remove(file_id)
    return Upsert(
        document_id=file_id,
        project_id=str(payload["project_id"]),
        revision_no=int(payload["revision_no"]),
        folder_code=document["folder_code"],
        title=document["title"] or "",
        body=document["body_text"] or "",
    )
