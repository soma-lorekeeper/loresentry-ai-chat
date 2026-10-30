import base64
import json
from dataclasses import dataclass

from app.manuscript import is_envelope

TYPES = {"FileChanged", "ProjectDeleted"}
STATES = {"ACTIVE", "TRASHED"}


class Malformed(ValueError):
    """A FileChanged that does not follow content's current contract."""


@dataclass(frozen=True)
class Upsert:
    document_id: str
    project_id: str
    sequence: int
    revision_no: int
    folder_code: str
    title: str
    body_enc: bytes
    wrapped_key: bytes


@dataclass(frozen=True)
class Remove:
    document_id: str
    sequence: int


@dataclass(frozen=True)
class RemoveProject:
    project_id: str


Change = Upsert | Remove | RemoveProject


def header_map(headers: list[tuple[str, bytes | None]] | None) -> dict[str, str]:
    return {
        name: value.decode("utf-8") if value is not None else ""
        for name, value in headers or ()
    }


def sequence_of(headers: dict[str, str]) -> int:
    # content's outbox seq grows in commit order for each document. revision_no
    # cannot order events: a rename or a move leaves it as it was.
    raw = headers.get("ce_sequence", "")
    if not (raw.isascii() and raw.isdigit()):
        raise Malformed("ce_sequence")
    return int(raw)


def body_of(payload: dict, document: dict) -> tuple[bytes, bytes]:
    sealed = document.get("body_text_enc")
    if not isinstance(sealed, str):
        raise Malformed("body_text_enc")
    body_enc = base64.b64decode(sealed, validate=True)
    if not is_envelope(body_enc):
        raise Malformed("body_text_enc")
    wrapped = payload.get("wrapped_key")
    if not isinstance(wrapped, str) or not wrapped:
        raise Malformed("wrapped_key")
    return body_enc, base64.b64decode(wrapped, validate=True)


def parse(headers: dict[str, str], value: bytes | None) -> Change | None:
    kind = headers.get("ce_type")
    if kind not in TYPES or value is None:
        return None
    payload = json.loads(value)
    if kind == "ProjectDeleted":
        return RemoveProject(str(payload["project_id"]))
    sequence = sequence_of(headers)
    file_id = str(payload["file_id"])
    if payload.get("change_type") == "DELETED":
        return Remove(file_id, sequence)
    document = payload.get("file")
    if (
        not isinstance(document, dict)
        or document.get("state") not in STATES
        or not isinstance(document.get("revision_no"), int)
    ):
        raise Malformed("file")
    # A trashed document keeps its whole snapshot and can still get an UPDATED,
    # so its state, not change_type, says whether it is gone.
    if document["state"] == "TRASHED":
        return Remove(file_id, sequence)
    body_enc, wrapped_key = body_of(payload, document)
    return Upsert(
        document_id=file_id,
        project_id=str(payload["project_id"]),
        sequence=sequence,
        revision_no=document["revision_no"],
        folder_code=document["folder_code"],
        title=document["title"] or "",
        body_enc=body_enc,
        wrapped_key=wrapped_key,
    )
