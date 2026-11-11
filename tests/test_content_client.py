import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.content import ContentClient
from app.errors import ChatFailure

USER = uuid4()
PROJECT = uuid4()
DOCUMENT = str(uuid4())
VERSION = str(uuid4())

MANUSCRIPT = {
    "id": DOCUMENT,
    "title": "3화 · 균열의 밤",
    "body_text": "서윤은 등불을 내려놓고 문을 밀었다.",
    "revision_no": 12,
    "ordinal": 3,
    "episode_name": None,
    "properties": [{"key": "pov", "value": "서윤"}],
}


def client(handler) -> ContentClient:
    transport = httpx.MockTransport(handler)
    return ContentClient(httpx.AsyncClient(transport=transport, base_url="http://c"))


def check_version(handler):
    return asyncio.run(client(handler).check_version(USER, PROJECT, DOCUMENT))


def test_check_version_posts_the_document_and_reads_the_snapshot():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "version_id": VERSION,
                "created_at": "2026-11-01T09:00:00Z",
                "manuscript": MANUSCRIPT,
            },
        )

    checked = check_version(handler)

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == f"/internal/projects/{PROJECT}/check-versions"
    assert request.headers["X-User-Id"] == str(USER)
    assert json.loads(request.content) == {"document_id": DOCUMENT}
    assert checked.version_id == VERSION
    assert checked.document.id == DOCUMENT
    assert checked.document.is_manuscript
    assert checked.document.revision_no == 12
    assert checked.document.body_text == MANUSCRIPT["body_text"]
    assert checked.document.ordinal == 3


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (404, {"code": "PROJECT_NOT_FOUND"}, "PROJECT_NOT_FOUND"),
        (404, {"code": "FILE_NOT_FOUND"}, "SETTING_CHECK_DOCUMENT_NOT_FOUND"),
        (404, None, "SETTING_CHECK_DOCUMENT_NOT_FOUND"),
        (500, {"code": "INTERNAL_ERROR"}, "CONTENT_UNAVAILABLE"),
        (200, {"version_id": VERSION}, "CONTENT_UNAVAILABLE"),
    ],
)
def test_check_version_failures(status, body, code):
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, text="missing")
        return httpx.Response(status, json=body)

    with pytest.raises(ChatFailure) as failure:
        check_version(handler)

    assert failure.value.code == code
