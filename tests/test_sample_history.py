from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

from app import sample_history
from app.main import create_app
from app.setting_check.findings import fingerprint
from app.settings import Settings
from tests.support import CH1, CH3, LENA, FakeChecker, FakeContent, FakeModel

MIGRATIONS = sorted(
    (Path(__file__).parent.parent / "db" / "migration").glob("V*__*.sql"),
    key=lambda path: int(path.name[1:].split("__")[0]),
)

HISTORY = {
    "sessions": [
        {
            "starts_before_seconds": 900,
            "messages": [
                {
                    "role": "user",
                    "offset_seconds": 0,
                    "content": "처음인데 어떻게 시작하면 돼?",
                    "intent": "ask",
                },
                {
                    "role": "assistant",
                    "offset_seconds": 12,
                    "content": "안내를 따라가세요.\n[[action:tour]]",
                    "citations": [],
                },
            ],
        },
        {
            "starts_before_seconds": 600,
            "messages": [
                {
                    "role": "user",
                    "offset_seconds": 0,
                    "content": "서윤이 처음 본 곳은?",
                    "intent": "ask",
                },
                {
                    "role": "assistant",
                    "offset_seconds": 40,
                    "content": "정원이에요. [[1]]",
                    "citations": [
                        {
                            "kind": "document",
                            "title": "1화 · 유리의 계절",
                            "doc_type": "manuscript",
                            "quote": "서윤은 정원을 처음 보았다.",
                        }
                    ],
                },
                {
                    "role": "user",
                    "offset_seconds": 60,
                    "content": "레나는 누구야?",
                    "intent": "ask",
                },
                {
                    "role": "assistant",
                    "offset_seconds": 75,
                    "content": "항해사예요. [[1]]",
                    "citations": [
                        {
                            "kind": "document",
                            "title": "레나 아르벨",
                            "doc_type": "character",
                            "quote": None,
                        }
                    ],
                },
            ],
        },
    ],
    "setting_check": {
        "document": "3화 · 균열의 밤",
        "starts_before_seconds": 240,
        "duration_seconds": 42,
        "read": {
            "manuscripts": 2,
            "from_ordinal": 1,
            "to_ordinal": 2,
            "settings": 2,
            "core_facts": 0,
        },
        "findings": [
            {
                "kind": "SETTING",
                "core": False,
                "confidence": "HIGH",
                "explanation": "눈 색이 달라요.",
                "quote": "서윤은 등불을 내려놓고 문을 밀었다.",
                "quote_focus": "문을 밀었다",
                "source": {
                    "title": "레나 아르벨",
                    "doc_type": "character",
                    "ordinal": None,
                    "quote": "기억을 읽는 일은 언제나 대가를 요구한다.",
                    "quote_focus": None,
                },
            }
        ],
    },
}


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


@contextmanager
def make_client(database, history=HISTORY):
    with psycopg.connect(**database, autocommit=True) as conn:
        conn.execute("truncate setting_check_runs, chat_sessions cascade")
    user, project = uuid4(), uuid4()
    content = FakeContent(user, project)
    settings = Settings(
        db_host=database["host"],
        db_port=str(database["port"]),
        db_name=database["dbname"],
        db_username=database["user"],
        db_password=database["password"],
    )
    with (
        patch("app.routes.SAMPLE_HISTORY", history),
        TestClient(create_app(settings, content, FakeModel(), FakeChecker())) as client,
    ):
        yield client, user, project


def seed(client, user, project):
    response = client.post(
        f"/projects/{project}/chat/sample-history", headers={"X-User-Id": str(user)}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["seeded"]


def get(client, user, path):
    response = client.get(path, headers={"X-User-Id": str(user)})
    assert response.status_code == 200, response.text
    return response.json()


def query(database, sql, *params):
    with psycopg.connect(**database, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def test_seeds_the_recorded_sessions_with_this_projects_documents(database):
    with make_client(database) as (client, user, project):
        assert seed(client, user, project) is True

        sessions = get(client, user, f"/projects/{project}/chat/sessions")["sessions"]
        assert [s["title"] for s in sessions] == [
            "서윤이 처음 본 곳은?",
            "처음인데 어떻게 시작하면 돼?",
        ]
        assert sessions[0]["preview"].startswith("항해사예요.")

        messages = get(client, user, f"/chat/sessions/{sessions[0]['id']}/messages")[
            "messages"
        ]
        assert [(m["role"], m["content"]) for m in messages] == [
            ("user", "서윤이 처음 본 곳은?"),
            ("assistant", "정원이에요. [[1]]"),
            ("user", "레나는 누구야?"),
            ("assistant", "항해사예요. [[1]]"),
        ]
        assert messages[0]["intent"] == "ask"
        assert messages[1]["citations"] == [
            {
                "kind": "document",
                "file_id": CH1,
                "title": "1화 · 유리의 계절",
                "doc_type": "manuscript",
                "quote": "서윤은 정원을 처음 보았다.",
            }
        ]
        assert messages[3]["citations"][0]["file_id"] == LENA

        start = get(client, user, f"/chat/sessions/{sessions[1]['id']}/messages")[
            "messages"
        ]
        assert start[1]["content"].endswith("[[action:tour]]")


def test_seeds_a_finished_check_with_fingerprints_for_this_project(database):
    with make_client(database) as (client, user, project):
        assert seed(client, user, project) is True

        history = get(
            client, user, f"/projects/{project}/setting-checks?document_id={CH3}"
        )
        assert [run["status"] for run in history["runs"]] == ["DONE"]
        rows = query(
            database,
            "select id, document_id, status, model, revision_no from setting_check_runs",
        )
        assert len(rows) == 1
        run_id, document_id, status, model, revision = rows[0]
        assert (str(document_id), status, model, revision) == (
            CH3,
            "DONE",
            sample_history.MODEL,
            5,
        )

        run = get(client, user, f"/setting-checks/{run_id}")
        assert run["document_title"] == "3화 · 균열의 밤"
        assert run["read"]["settings"] == 2
        assert run["counts"] == {"open": 1, "intended": 0, "not_conflict": 0}
        finding = run["findings"][0]
        assert finding["source"]["document_id"] == LENA
        assert finding["source"]["doc_type"] == "character"
        assert finding["quote_focus"] == "문을 밀었다"

        stored = query(database, "select fingerprint from setting_check_findings")[0][0]
        assert stored == fingerprint(
            "SETTING",
            LENA,
            "기억을 읽는 일은 언제나 대가를 요구한다.",
            "서윤은 등불을 내려놓고 문을 밀었다.",
        )


def test_seeds_only_once(database):
    with make_client(database) as (client, user, project):
        assert seed(client, user, project) is True
        assert seed(client, user, project) is False
        assert query(database, "select count(*) from chat_sessions")[0][0] == 2
        assert query(database, "select count(*) from setting_check_runs")[0][0] == 1


def test_skips_a_project_missing_any_recorded_document(database):
    history = {
        **HISTORY,
        "setting_check": {**HISTORY["setting_check"], "document": "없는 회차"},
    }
    with make_client(database, history) as (client, user, project):
        assert seed(client, user, project) is False
        assert query(database, "select count(*) from chat_sessions")[0][0] == 0
        assert query(database, "select count(*) from setting_check_runs")[0][0] == 0


def test_another_users_project_is_not_found(database):
    with make_client(database) as (client, _, project):
        response = client.post(
            f"/projects/{project}/chat/sample-history",
            headers={"X-User-Id": str(uuid4())},
        )
        assert response.status_code == 404
        assert response.json()["code"] == "PROJECT_NOT_FOUND"
        assert query(database, "select count(*) from chat_sessions")[0][0] == 0


def test_the_korean_recording_is_two_sessions_of_three_turns_and_one_check():
    data = sample_history.load()
    for session in data["sessions"]:
        roles = [message["role"] for message in session["messages"]]
        assert roles == ["user", "assistant"] * 3
        offsets = [message["offset_seconds"] for message in session["messages"]]
        assert offsets == sorted(offsets) and offsets[0] == 0
    assert (
        data["sessions"][0]["messages"][0]["content"] == "처음인데 어떻게 시작하면 돼?"
    )
    assert data["setting_check"]["document"] == "30화 · 동지 사흘 전"
    assert len(data["setting_check"]["findings"]) == 4
    assert "흑요석 성반" in sample_history.titles(data)
