import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

from app.main import create_app
from app.settings import Settings
from tests.support import CH3, FakeContent, FakeModel, read_events

MIGRATIONS = sorted(
    (Path(__file__).parent.parent / "db" / "migration").glob("V*__*.sql"),
    key=lambda path: int(path.name[1:].split("__")[0]),
)


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("postgres:18", driver=None) as postgres:
        info = {
            "host": postgres.get_container_host_ip(),
            "port": postgres.get_exposed_port(5432),
            "dbname": postgres.dbname,
            "user": postgres.username,
            "password": postgres.password,
        }
        with psycopg.connect(**info, autocommit=True) as conn:
            for migration in MIGRATIONS:
                conn.execute(migration.read_text())
        yield info


@dataclass
class Env:
    client: TestClient
    user: UUID
    project: UUID
    content: FakeContent
    model: FakeModel
    db: dict

    @property
    def headers(self) -> dict[str, str]:
        return {"X-User-Id": str(self.user)}

    def query(self, sql: str, *params):
        with psycopg.connect(**self.db, autocommit=True) as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    def create(self, **headers) -> dict:
        response = self.client.post(
            f"/projects/{self.project}/chat/sessions",
            headers={**self.headers, **headers},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def send(self, session_id: str, content: str = "북쪽 문은 누가 열었어?", **body):
        payload = {
            "content": content,
            "intent": "ask",
            "quote": None,
            "focus_file_id": None,
            "replaces": None,
            "client_message_id": str(uuid4()),
            **body,
        }
        with self.client.stream(
            "POST",
            f"/chat/sessions/{session_id}/messages",
            json=payload,
            headers=self.headers,
        ) as response:
            if response.status_code != 200:
                response.read()
                return response.status_code, response.json()
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store"
            return 200, read_events(response)

    def messages(self, session_id: str) -> dict:
        response = self.client.get(
            f"/chat/sessions/{session_id}/messages", headers=self.headers
        )
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def env(database):
    with psycopg.connect(**database, autocommit=True) as conn:
        conn.execute("truncate chat_sessions cascade")
    user, project = uuid4(), uuid4()
    content = FakeContent(user, project)
    model = FakeModel()
    settings = Settings(
        db_host=database["host"],
        db_port=str(database["port"]),
        db_name=database["dbname"],
        db_username=database["user"],
        db_password=database["password"],
        heartbeat_seconds=0.2,
        cancel_poll_seconds=0.05,
    )
    with TestClient(create_app(settings, content, model)) as client:
        yield Env(client, user, project, content, model, database)


def names(events) -> list[str]:
    return [name for name, _ in events if name != "ping"]


def payload(events, name: str) -> dict:
    return next(data for event, data in events if event == name)


def test_requests_need_a_user(env):
    response = env.client.get(f"/projects/{env.project}/chat/sessions")

    assert response.status_code == 401
    assert response.json() == {
        "code": "USER_CONTEXT_REQUIRED",
        "message": "User context is required",
        "next_action": "RELOGIN",
    }


def test_availability_reports_size_and_limit(env):
    response = env.client.get(
        f"/projects/{env.project}/chat/availability", headers=env.headers
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "available",
        "size": env.content.data.size,
        "limit": 1_400_000,
    }


def test_availability_flags_a_story_over_the_limit(env):
    env.content.data = replace(env.content.data, size=1_400_001)

    response = env.client.get(
        f"/projects/{env.project}/chat/availability", headers=env.headers
    )

    assert response.json()["status"] == "too-large"


def test_other_projects_are_not_found(env):
    response = env.client.post(
        f"/projects/{uuid4()}/chat/sessions", headers=env.headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "PROJECT_NOT_FOUND"


def test_session_lifecycle(env):
    created = env.create(**{"X-User-Locale": "en"})

    assert created["title"] == "New conversation"
    assert created["preview"] == ""
    assert created["project_id"] == str(env.project)

    renamed = env.client.patch(
        f"/chat/sessions/{created['id']}",
        json={"title": "  북쪽 문  "},
        headers=env.headers,
    )
    assert renamed.json()["title"] == "북쪽 문"

    empty = env.client.patch(
        f"/chat/sessions/{created['id']}", json={"title": " "}, headers=env.headers
    )
    assert empty.status_code == 400
    assert empty.json()["code"] == "CHAT_TITLE_REQUIRED"

    listed = env.client.get(
        f"/projects/{env.project}/chat/sessions", headers=env.headers
    ).json()["sessions"]
    assert [s["id"] for s in listed] == [created["id"]]

    stranger = env.client.delete(
        f"/chat/sessions/{created['id']}", headers={"X-User-Id": str(uuid4())}
    )
    assert stranger.status_code == 404

    deleted = env.client.delete(f"/chat/sessions/{created['id']}", headers=env.headers)
    assert deleted.status_code == 204
    gone = env.client.get(
        f"/chat/sessions/{created['id']}/messages", headers=env.headers
    )
    assert gone.status_code == 404
    assert gone.json()["code"] == "CHAT_SESSION_NOT_FOUND"


def test_a_turn_streams_phases_sources_and_text_then_saves(env):
    session = env.create()
    status, events = env.send(
        session["id"], "북쪽 문은 누가 열었어? 그리고 그 전에 누가 서 있었지?"
    )

    assert status == 200
    assert names(events)[:3] == ["phase", "phase", "phase"]
    assert [data["phase"] for name, data in events if name == "phase"] == [
        "gathering",
        "reading",
        "writing",
    ]
    citations = [data for name, data in events if name == "citation"]
    assert [c["index"] for c in citations] == [1, 2]
    assert citations[0]["citation"] == {
        "kind": "document",
        "file_id": CH3,
        "title": "3화 · 균열의 밤",
        "doc_type": "manuscript",
        "quote": "서윤은 등불을 내려놓고",
    }
    streamed = "".join(data["text"] for name, data in events if name == "delta")
    assert (
        streamed
        == "북쪽 문을 연 사람은 서윤이에요. [[1]]\n- 레나는 그 앞에 먼저 섰어요.[[2]]"
    )

    done = payload(events, "done")
    assert (
        done["question"]["content"]
        == "북쪽 문은 누가 열었어? 그리고 그 전에 누가 서 있었지?"
    )
    assert done["answer"]["content"] == streamed
    assert done["answer"]["role"] == "assistant"
    assert len(done["answer"]["citations"]) == 2

    saved = env.messages(session["id"])
    assert saved["generating"] is False
    assert [m["id"] for m in saved["messages"]] == [
        done["question"]["id"],
        done["answer"]["id"],
    ]

    listed = env.client.get(
        f"/projects/{env.project}/chat/sessions", headers=env.headers
    ).json()["sessions"][0]
    assert listed["title"] == "북쪽 문은 누가 열었어? 그리고 그 전에 …"
    assert (
        listed["preview"]
        == "북쪽 문을 연 사람은 서윤이에요. 레나는 그 앞에 먼저 섰어요."
    )

    turn = env.query(
        "select status, model, input_tokens, cached_tokens, output_tokens, "
        "reasoning_tokens, context_chars, first_token_ms is not null from chat_turns"
    )
    assert turn == [
        ("COMPLETE", "fake-model", 1200, 800, 90, 30, env.content.data.size, True)
    ]


def test_profile_and_question_reach_the_model(env):
    session = env.create()
    env.client.headers.update(
        {"X-User-Display-Name": quote("서윤주"), "X-User-Locale": "ko"}
    )
    try:
        env.send(
            session["id"],
            "",
            intent="explain",
            quote={
                "file_id": CH3,
                "title": "3화 · 균열의 밤",
                "doc_type": "manuscript",
                "text": "문을 밀었다",
            },
            focus_file_id=CH3,
        )
    finally:
        env.client.headers.pop("X-User-Display-Name")
        env.client.headers.pop("X-User-Locale")

    prompt = env.model.prompts[-1]
    assert (
        '<author name="서윤주" locale="ko"/>' in prompt.input[0]["content"][0]["text"]
    )
    final = prompt.input[-1]["content"][0]["text"]
    assert '<viewing id="D3"' in final and "<task>Explain" in final
    listed = env.client.get(
        f"/projects/{env.project}/chat/sessions", headers=env.headers
    ).json()["sessions"][0]
    assert listed["title"] == "문을 밀었다"


def test_a_question_needs_text_or_a_quote(env):
    session = env.create()
    status, body = env.send(session["id"], "   ")

    assert status == 400
    assert body["code"] == "CHAT_QUESTION_REQUIRED"


def test_a_story_over_the_limit_is_refused_before_streaming(env):
    session = env.create()
    env.content.data = replace(env.content.data, size=1_400_001)
    status, body = env.send(session["id"])

    assert status == 422
    assert body["code"] == "CHAT_CONTEXT_TOO_LARGE"
    assert env.query("select count(*) from chat_messages") == [(0,)]


def test_the_next_turn_carries_the_conversation(env):
    session = env.create()
    env.send(session["id"], "첫 질문")
    env.send(session["id"], "두 번째 질문")

    prompt = env.model.prompts[-1]
    assert prompt.history_turns == 1
    assert prompt.input[2]["role"] == "assistant"
    assert '<cite id="D3"/>' in prompt.input[2]["content"]


def test_resending_the_same_request_replays_the_saved_answer(env):
    session = env.create()
    client_id = str(uuid4())
    _, first = env.send(session["id"], client_message_id=client_id)
    _, again = env.send(session["id"], client_message_id=client_id)

    assert names(again) == ["done"]
    assert payload(again, "done") == payload(first, "done")
    assert len(env.model.prompts) == 1


def test_a_failed_turn_keeps_the_question_and_can_be_retried(env):
    session = env.create()
    client_id = str(uuid4())
    env.model.fail_with = "MODEL_UNAVAILABLE"
    _, failed = env.send(session["id"], client_message_id=client_id)

    assert payload(failed, "error") == {
        "code": "CHAT_MODEL_UNAVAILABLE",
        "message": "The model is temporarily unavailable",
    }
    assert [m["role"] for m in env.messages(session["id"])["messages"]] == ["user"]
    assert env.query("select status, failure_code from chat_turns") == [
        ("FAILED", "MODEL_UNAVAILABLE")
    ]

    _, retried = env.send(session["id"], client_message_id=client_id)
    assert "done" in names(retried)
    assert [m["role"] for m in env.messages(session["id"])["messages"]] == [
        "user",
        "assistant",
    ]


def test_editing_the_last_question_replaces_it_and_its_answer(env):
    session = env.create()
    _, first = env.send(session["id"], "처음 질문")
    question_id = payload(first, "done")["question"]["id"]

    _, edited = env.send(session["id"], "고친 질문", replaces=question_id)

    messages = env.messages(session["id"])["messages"]
    assert [m["content"] for m in messages][0] == "고친 질문"
    assert len(messages) == 2
    assert payload(edited, "done")["question"]["id"] != question_id


def test_only_the_last_question_can_be_replaced(env):
    session = env.create()
    _, first = env.send(session["id"], "하나")
    env.send(session["id"], "둘")

    status, body = env.send(
        session["id"], "고침", replaces=payload(first, "done")["question"]["id"]
    )

    assert status == 409
    assert body["code"] == "CHAT_NOT_LAST_QUESTION"


def test_a_renamed_title_survives_the_first_question(env):
    session = env.create()
    env.client.patch(
        f"/chat/sessions/{session['id']}",
        json={"title": "내 제목"},
        headers=env.headers,
    )
    env.send(session["id"], "질문")

    listed = env.client.get(
        f"/projects/{env.project}/chat/sessions", headers=env.headers
    ).json()["sessions"][0]
    assert listed["title"] == "내 제목"


def start_in_background(env, session_id: str, **body):
    result: dict = {}

    def run():
        result["value"] = env.send(session_id, **body)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


def test_one_answer_at_a_time_per_session(env):
    session = env.create()
    env.model.gate = threading.Event()
    thread, result = start_in_background(env, session["id"])
    wait_for(lambda: env.messages(session["id"])["generating"])

    status, body = env.send(session["id"], "또 질문")
    env.model.gate.set()
    thread.join(5)

    assert status == 409
    assert body["code"] == "CHAT_ANSWER_IN_PROGRESS"
    assert names(result["value"][1])[-1] == "done"


def test_different_sessions_answer_at_the_same_time(env):
    first, second = env.create(), env.create()
    env.model.gate = threading.Event()
    one, first_result = start_in_background(env, first["id"])
    two, second_result = start_in_background(env, second["id"])
    wait_for(
        lambda: (
            env.messages(first["id"])["generating"]
            and env.messages(second["id"])["generating"]
        )
    )
    env.model.gate.set()
    one.join(5)
    two.join(5)

    assert names(first_result["value"][1])[-1] == "done"
    assert names(second_result["value"][1])[-1] == "done"


def test_stop_during_reading_saves_nothing(env):
    session = env.create()
    env.model.gate = threading.Event()
    thread, result = start_in_background(env, session["id"])
    wait_for(lambda: env.messages(session["id"])["generating"])

    assert (
        env.client.post(
            f"/chat/sessions/{session['id']}/cancel", headers=env.headers
        ).status_code
        == 204
    )
    thread.join(5)

    status, events = result["value"]
    assert payload(events, "error")["code"] == "CHAT_ANSWER_CANCELLED"
    assert [m["role"] for m in env.messages(session["id"])["messages"]] == ["user"]
    assert env.query("select status from chat_turns") == [("CANCELLED",)]
    env.model.gate.set()


def test_pings_keep_a_quiet_stream_alive(env):
    session = env.create()
    env.model.gate = threading.Event()
    timer = threading.Timer(0.7, env.model.gate.set)
    timer.start()
    _, events = env.send(session["id"])
    timer.join()

    assert ("ping", None) in events
    assert names(events)[-1] == "done"


def test_deleting_a_session_while_answering_stops_the_answer(env):
    session = env.create()
    env.model.gate = threading.Event()
    thread, result = start_in_background(env, session["id"])
    wait_for(lambda: env.messages(session["id"])["generating"])

    env.client.delete(f"/chat/sessions/{session['id']}", headers=env.headers)
    thread.join(5)

    assert env.query("select count(*) from chat_messages") == [(0,)]
    env.model.gate.set()


def test_a_stale_generating_turn_does_not_block_the_session(env):
    session = env.create()
    env.query(
        "insert into chat_turns (session_id, status, created_at) "
        "values (%s, 'GENERATING', now() - interval '10 minutes') returning id",
        session["id"],
    )

    status, events = env.send(session["id"])

    assert status == 200 and "done" in names(events)
    assert sorted(row[0] for row in env.query("select status from chat_turns")) == [
        "COMPLETE",
        "FAILED",
    ]


def test_search_finds_titles_first_then_message_text_with_a_snippet(env):
    by_title = env.create()
    env.client.patch(
        f"/chat/sessions/{by_title['id']}",
        json={"title": "레나 정리"},
        headers=env.headers,
    )
    by_text = env.create()
    env.client.patch(
        f"/chat/sessions/{by_text['id']}", json={"title": "각성"}, headers=env.headers
    )
    env.send(by_text["id"], "레나는 어디서 처음 각성했어?")
    env.create()

    hits = env.client.get(
        f"/projects/{env.project}/chat/search",
        params={"q": " 레나 "},
        headers=env.headers,
    ).json()["hits"]

    assert [hit["session"]["id"] for hit in hits] == [by_title["id"], by_text["id"]]
    assert hits[0]["snippet"] is None
    assert hits[1]["snippet"] == {
        "before": "",
        "match": "레나",
        "after": "는 어디서 처음 각성했어?",
    }


def test_search_treats_wildcards_literally_and_checks_length(env):
    session = env.create()
    env.send(session["id"], "100% 확실해?")
    env.send(env.create()["id"], "아무 질문")

    hits = env.client.get(
        f"/projects/{env.project}/chat/search", params={"q": "%"}, headers=env.headers
    ).json()["hits"]
    assert [hit["session"]["id"] for hit in hits] == [session["id"]]

    too_long = env.client.get(
        f"/projects/{env.project}/chat/search",
        params={"q": "가" * 101},
        headers=env.headers,
    )
    assert too_long.status_code == 400
    blank = env.client.get(
        f"/projects/{env.project}/chat/search", params={"q": "  "}, headers=env.headers
    )
    assert blank.json() == {"hits": []}


def test_search_matches_source_titles(env):
    session = env.create()
    env.send(session["id"], "질문")

    hits = env.client.get(
        f"/projects/{env.project}/chat/search",
        params={"q": "균열의 밤"},
        headers=env.headers,
    ).json()["hits"]

    assert hits[0]["session"]["id"] == session["id"]
    assert "균열의 밤" in hits[0]["snippet"]["match"]


def test_deleting_user_and_project_data(env):
    mine = env.create()
    env.send(mine["id"])
    other_project = uuid4()
    env.query(
        "insert into chat_sessions (project_id, user_id, title) values (%s, %s, 't')",
        other_project,
        env.user,
    )
    stranger = uuid4()
    env.query(
        "insert into chat_sessions (project_id, user_id, title) values (%s, %s, 't')",
        env.project,
        stranger,
    )

    assert (
        env.client.delete(
            f"/projects/{env.project}/chat/data", headers=env.headers
        ).status_code
        == 204
    )
    assert env.query(
        "select user_id, project_id from chat_sessions order by user_id"
    ) == sorted([(env.user, other_project), (stranger, env.project)])

    assert env.client.delete("/users/me/data", headers=env.headers).status_code == 204
    assert env.query("select user_id from chat_sessions") == [(stranger,)]


def test_malformed_requests_are_rejected(env):
    session = env.create()

    assert (
        env.client.get("/chat/sessions/nope/messages", headers=env.headers).json()[
            "code"
        ]
        == "CHAT_SESSION_NOT_FOUND"
    )
    status, body = env.send(session["id"], intent="rewrite")
    assert (status, body["code"]) == (400, "INVALID_REQUEST")
    status, body = env.send(session["id"], "가" * 4001)
    assert (status, body["code"]) == (400, "INVALID_REQUEST")
