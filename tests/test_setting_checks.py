import hashlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

from app.content import Document, parse_documents
from app.errors import ChatFailure
from app.main import create_app
from app.setting_check.findings import (
    Candidate,
    Reading,
    dedupe,
    fingerprint,
    judged,
    parse_candidates,
)
from app.setting_check.prompts import JUDGE_SCHEMA, judge_input
from app.settings import Settings
from tests.support import (
    CH1,
    CH2,
    CH3,
    GREENHOUSE,
    LENA,
    FakeChecker,
    FakeContent,
    FakeModel,
    candidate,
    short,
)

MIGRATIONS = sorted(
    (Path(__file__).parent.parent / "db" / "migration").glob("V*__*.sql"),
    key=lambda path: int(path.name[1:].split("__")[0]),
)

CHAPTER = (
    "레나는 은빛 눈으로 정원을 보았다. 서윤은 이 정원에 처음 왔다.\n"
    "서윤은 등불을 내려놓고 문을 밀었다. 하린은 북쪽 문이 늘 열려 있다고 말했다."
)
FACT = "북쪽 문은 낡은 열쇠 없이는 열리지 않는다."
REASON = "12화에서 열쇠를 미리 줬어요"
LENA_EXPLANATION = "‘레나 아르벨’ 설정은 레나의 눈을 검은색으로 정해 두었어요. 그런데 3화에서 레나는 은빛 눈으로 정원을 봐요. 눈 색이 바뀌는 장면이 앞 회차에 없어서 두 내용이 함께 성립하지 않아요."

BY_SETTING = [
    candidate(
        GREENHOUSE,
        FACT,
        "서윤은 등불을 내려놓고 문을 밀었다.",
        summary="북쪽 문은 낡은 열쇠로만 열리는데, 서윤이 열쇠 없이 문을 밀어요",
        direction="3화까지 서윤이 낡은 열쇠를 손에 넣는 장면이 없어요.",
    )
]
WHOLE = [
    candidate(LENA, "레나의 눈은 검은색이다.", "“레나는 은빛 눈으로 정원을 보았다.”"),
    candidate(GREENHOUSE, "북쪽 문은 낡은 열쇠 없이는", "문을 밀었다"),
    candidate(
        CH1,
        "서윤은 정원을 처음 보았다.",
        "서윤은 이 정원에 처음 왔다.",
        kind="MANUSCRIPT",
        summary="1화에서 서윤이 정원을 이미 봤는데, 처음 왔다고 해요",
        direction="1화의 첫 방문 장면이 관련돼요.",
    ),
    candidate(
        CH2,
        "레나는 북쪽 문 앞에 섰다.",
        "하린은 북쪽 문이 늘 열려 있다고 말했다.",
        kind="MANUSCRIPT",
    ),
    candidate(
        LENA,
        "기억을 읽는 일은 언제나 대가를 요구한다.",
        "서윤은 정원을 이미 열 번 보았다.",
    ),
    candidate(
        GREENHOUSE, "온실의 유리는 붉다.", "하린은 북쪽 문이 늘 열려 있다고 말했다."
    ),
    {**candidate(LENA, "레나의 눈은 검은색이다.", "하린은"), "source_id": "ffff0000"},
]
VERDICTS = {
    "레나는 은빛 눈으로 정원을 보았다.": {
        "confidence": "LOW",
        "explanation": "  ‘레나 아르벨’ 설정은 레나의 눈을 검은색으로 정해 두었어요. 그런데 3화에서 레나는 은빛 눈으로 정원을 봐요. 눈 색이 바뀌는 장면이 앞 회차에 없어서 두 내용이 함께 성립하지 않아요.\n",
        "quote_focus": "은빛 눈",
        "source_focus": " “검은색” ",
    },
    "서윤은 이 정원에 처음 왔다.": {
        "quote_focus": "처음 왔다",
        "source_focus": "정원을 두 번 보았다",
    },
    "하린은 북쪽 문이 늘 열려 있다고 말했다.": {"verdict": "CHARACTER_PERSPECTIVE"},
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


@dataclass
class Env:
    client: TestClient
    user: UUID
    project: UUID
    content: FakeContent
    checker: FakeChecker
    settings: Settings
    db: dict

    @property
    def headers(self) -> dict[str, str]:
        return {"X-User-Id": str(self.user)}

    @property
    def runner(self):
        return self.client.app.state.services.checks

    def query(self, sql: str, *params):
        with psycopg.connect(**self.db, autocommit=True) as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    def start(self, document_id: str = CH3, **headers):
        return self.client.post(
            f"/projects/{self.project}/setting-checks",
            json={"document_id": document_id},
            headers={**self.headers, **headers},
        )

    def get(self, path: str, **params):
        response = self.client.get(path, params=params, headers=self.headers)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        return response.json()

    def finish(self, run_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = self.get(f"/setting-checks/{run_id}")
            if run["status"] != "RUNNING" and not self.runner.tasks:
                return run
            time.sleep(0.02)
        raise AssertionError("run did not finish")

    def check(self, document_id: str = CH3) -> dict:
        response = self.start(document_id)
        assert response.status_code == 202, response.text
        return self.finish(response.json()["id"])

    def verdict(self, finding_id: str, verdict: str, reason=None, user=None):
        return self.client.put(
            f"/setting-check-findings/{finding_id}/verdict",
            json={"verdict": verdict, "reason": reason},
            headers={"X-User-Id": str(user or self.user)},
        )


def story(content: FakeContent) -> None:
    docs = content.docs
    docs[CH3] = replace(docs[CH3], body_text=CHAPTER)
    docs[LENA] = replace(
        docs[LENA],
        body_text="기억을 읽는 일은 언제나 대가를 요구한다. 레나의 눈은 검은색이다.",
    )
    docs[GREENHOUSE] = replace(
        docs[GREENHOUSE],
        body_text=f"유리 지붕이 금 간 온실.\n{FACT}",
        core_facts=(FACT,),
    )


@contextmanager
def make_env(database, checker: FakeChecker | None = None, **overrides):
    with psycopg.connect(**database, autocommit=True) as conn:
        conn.execute(
            "truncate setting_check_runs, setting_check_verdicts, chat_sessions cascade"
        )
    user, project = uuid4(), uuid4()
    content = FakeContent(user, project)
    story(content)
    checker = checker or FakeChecker(WHOLE, BY_SETTING, VERDICTS)
    settings = Settings(
        db_host=database["host"],
        db_port=str(database["port"]),
        db_name=database["dbname"],
        db_username=database["user"],
        db_password=database["password"],
        **overrides,
    )
    with TestClient(create_app(settings, content, FakeModel(), checker)) as client:
        yield Env(client, user, project, content, checker, settings, database)


@pytest.fixture
def env(database):
    with make_env(database) as made:
        yield made


REQUIRED = {
    "id",
    "project_id",
    "document_id",
    "document_title",
    "revision_no",
    "status",
    "stage",
    "created_at",
    "finished_at",
    "read",
    "counts",
    "failure_code",
}


def assert_run_shape(run: dict, findings: bool) -> None:
    assert set(run) == REQUIRED | ({"findings"} if findings else set())
    for key in REQUIRED - {"stage", "finished_at", "failure_code"}:
        assert run[key] is not None, key
    for key in ("manuscripts", "settings", "core_facts"):
        assert isinstance(run["read"][key], int), key
    assert set(run["read"]) == {
        "manuscripts",
        "from_ordinal",
        "to_ordinal",
        "settings",
        "core_facts",
    }
    assert set(run["counts"]) == {"open", "intended", "not_conflict"}
    assert all(isinstance(value, int) for value in run["counts"].values())
    if findings:
        assert isinstance(run["findings"], list)


def test_norm_ignores_spacing_quote_marks_and_end_punctuation():
    a = fingerprint("SETTING", GREENHOUSE, f"“{FACT}”", "서윤은  등불을\n내려놓고.")
    b = fingerprint("SETTING", GREENHOUSE, FACT, "서윤은 등불을 내려놓고")
    raw = f"SETTING|{GREENHOUSE}|북쪽 문은 낡은 열쇠 없이는 열리지 않는다|서윤은 등불을 내려놓고"

    assert a == b == hashlib.sha256(raw.encode()).hexdigest()
    assert fingerprint("MANUSCRIPT", GREENHOUSE, FACT, "서윤은 등불을 내려놓고") != a


def test_quotes_must_occur_and_kind_and_core_come_from_the_source():
    content = FakeContent(uuid4(), uuid4())
    story(content)
    docs = content.docs
    reading = Reading(docs[CH3], (docs[CH1], docs[CH2]), (docs[LENA], docs[GREENHOUSE]))

    def make(source, source_quote, quote, kind="MANUSCRIPT"):
        return Candidate(kind, source, source_quote, quote, "s", "d")

    found = reading.verify(make(GREENHOUSE, f"「{FACT}」", "문을  밀었다."))
    assert found == Candidate(
        "SETTING", GREENHOUSE, FACT, "문을 밀었다.", "s", "d", core=True
    )
    assert (
        reading.verify(make(GREENHOUSE, "유리 지붕이 금 간", "문을 밀었다")).core
        is False
    )
    assert reading.verify(make(LENA, "은빛 항해단의 항해사", "문을 밀었다")) is not None
    assert reading.verify(make(CH1, "서윤은 정원을", "정원을 이미 열 번")) is None
    assert reading.verify(make(CH1, "레나는 정원을", "문을 밀었다")) is None
    assert reading.verify(make(CH3, "문을 밀었다", "문을 밀었다")) is None
    assert reading.verify(make(CH1, "서윤은", "레나")) is None


def test_the_judge_reads_the_earlier_chapters_a_candidate_relies_on():
    content = FakeContent(uuid4(), uuid4())
    story(content)
    docs = content.docs
    reading = Reading(docs[CH3], (docs[CH1], docs[CH2]), (docs[LENA], docs[GREENHOUSE]))
    item = {
        **candidate(
            LENA, "레나의 눈은 검은색이다.", "레나는 은빛 눈으로 정원을 보았다."
        ),
        "related_ids": [short(CH1), short(LENA), "nope", short(CH1), short(CH2)],
    }
    (found,) = parse_candidates({"candidates": [item]}, reading.by_short, False)

    assert found.related_ids == (CH1, LENA, CH2)

    def blocks(item):
        return [
            block["text"]
            for block in judge_input(reading, item, [], "ko")[0]["content"]
        ]

    first, rest, _ = blocks(found)
    assert first.startswith("<settings>") and CHAPTER in first
    assert docs[CH1].body_text in first and docs[CH2].body_text in first
    assert "<related>" not in rest
    assert rest.count("레나의 눈은 검은색이다.") == 1

    with patch(
        "app.setting_check.prompts.JUDGE_EARLIER_CHARS", len(docs[CH2].body_text)
    ):
        first, rest, _ = blocks(found)
    assert docs[CH2].body_text in first and docs[CH1].body_text not in first
    assert "<related>" in rest and docs[CH1].body_text in rest

    (alone,) = parse_candidates(
        {"candidates": [candidate(LENA, "a", "b")]}, reading.by_short, False
    )
    with patch("app.setting_check.prompts.JUDGE_EARLIER_CHARS", 0):
        first, rest, _ = blocks(alone)
    assert alone.related_ids == ()
    assert "<earlier/>" in first and "<related>" not in rest


def test_duplicates_are_one_chapter_place_against_one_source():
    first = Candidate("SETTING", GREENHOUSE, FACT, "서윤은 문을 밀었다.", "", "")
    same_place = replace(first, quote="문을 밀었다", source_quote="유리 지붕")
    other_place = replace(first, quote="등불을 내려놓고")
    elsewhere = replace(first, source_id=LENA)

    assert dedupe([first, same_place, other_place, elsewhere]) == [
        first,
        other_place,
        elsewhere,
    ]
    with_chapters = replace(same_place, related_ids=(CH1, CH2))
    assert dedupe([first, with_chapters, replace(first, related_ids=(CH2,))]) == [
        replace(first, related_ids=(CH1, CH2))
    ]


def test_a_focus_is_kept_only_when_it_occurs_in_its_quote():
    found = Candidate(
        "MANUSCRIPT",
        CH1,
        "다섯 번째 순찰을 마친 새벽이었다.",
        "서윤은 세 번째 순찰에서야 알아차렸다.",
        "s",
        "d",
    )

    def judge(candidate=found, **focus):
        value = {"verdict": "CONFLICT", "confidence": "HIGH", "explanation": ""}
        kept = judged(candidate, {**value, **focus})
        return kept.quote_focus, kept.source_focus

    assert judge(quote_focus="세 번째 순찰", source_focus="“다섯 번째\n순찰”") == (
        "세 번째 순찰",
        "다섯 번째 순찰",
    )
    assert judge(quote_focus=" 알아차렸다. ", source_focus=found.source_quote) == (
        "알아차렸다.",
        found.source_quote,
    )
    assert judge(quote_focus="네 번째 순찰", source_focus="") == (None, None)
    assert judge(quote_focus=3, source_focus="세 번째 순찰") == (None, None)
    assert judge() == (None, None)
    long = replace(found, quote=f"{'가' * 130} 순찰.")
    assert judge(long, quote_focus="가" * 120)[0] == "가" * 120
    assert judge(long, quote_focus="가" * 121)[0] is None
    assert JUDGE_SCHEMA["required"] == list(JUDGE_SCHEMA["properties"])
    assert {"explanation", "quote_focus", "source_focus"} <= set(
        JUDGE_SCHEMA["required"]
    )


def test_the_explanation_falls_back_to_the_first_reviewer():
    found = Candidate(
        "SETTING", LENA, "가나다라", "마바사아", "검은 눈인데 은빛이에요", ""
    )

    def explain(value):
        return judged(found, {"verdict": "CONFLICT", **value}).explanation

    assert explain({"explanation": " 첫째예요.\n 둘째예요. "}) == "첫째예요. 둘째예요."
    assert explain({"explanation": ""}) == "검은 눈인데 은빛이에요."
    assert explain({}) == "검은 눈인데 은빛이에요."
    both = replace(found, direction="장면이 없어요.")
    assert judged(both, {"verdict": "CONFLICT"}).explanation == (
        "검은 눈인데 은빛이에요. 장면이 없어요."
    )


def test_core_facts_are_read_and_default_to_none():
    def setting(**extra):
        return {
            "id": str(uuid4()),
            "title": "북쪽 온실",
            "folder_code": "LOCATION",
            "body_text": "본문",
            "revision_no": 1,
            "properties": [],
            **extra,
        }

    fetched = parse_documents(
        {
            "manuscripts": [],
            "settings": [setting(), setting(core_facts=["가", " ", 3, "나"])],
            "memos": [],
            "missing": [],
        }
    )

    assert [doc.core_facts for doc in fetched.documents] == [(), ("가", "나")]


def test_a_check_runs_in_the_background_and_keeps_only_judged_conflicts(env):
    response = env.start(**{"X-User-Locale": "en"})

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    started = response.json()
    assert_run_shape(started, findings=False)
    assert started["status"] == "RUNNING"
    assert started["stage"] == "READING"
    assert started["document_title"] == "3화 · 균열의 밤"
    assert started["read"] == {
        "manuscripts": 0,
        "from_ordinal": None,
        "to_ordinal": None,
        "settings": 0,
        "core_facts": 0,
    }
    assert started["counts"] == {"open": 0, "intended": 0, "not_conflict": 0}

    run = env.finish(started["id"])

    assert_run_shape(run, findings=True)
    assert run["status"] == "DONE"
    assert run["stage"] is None and run["finished_at"] is not None
    assert run["failure_code"] is None
    assert run["revision_no"] == env.content.docs[CH3].revision_no
    assert run["read"] == {
        "manuscripts": 2,
        "from_ordinal": 1,
        "to_ordinal": 2,
        "settings": 2,
        "core_facts": 1,
    }
    assert run["counts"] == {"open": 3, "intended": 0, "not_conflict": 0}
    findings = run["findings"]
    for finding in findings:
        assert UUID(finding["id"])
        del finding["id"]
    assert findings == [
        {
            "kind": "SETTING",
            "core": False,
            "confidence": "LOW",
            "explanation": LENA_EXPLANATION,
            "quote": "레나는 은빛 눈으로 정원을 보았다.",
            "quote_focus": "은빛 눈",
            "source": {
                "document_id": LENA,
                "title": "레나 아르벨",
                "doc_type": "character",
                "ordinal": None,
                "quote": "레나의 눈은 검은색이다.",
                "quote_focus": "검은색",
            },
            "verdict": None,
            "reason": None,
        },
        {
            "kind": "MANUSCRIPT",
            "core": False,
            "confidence": "HIGH",
            "explanation": "1화에서 서윤이 정원을 이미 봤는데, 처음 왔다고 해요. "
            "1화의 첫 방문 장면이 관련돼요.",
            "quote": "서윤은 이 정원에 처음 왔다.",
            "quote_focus": "처음 왔다",
            "source": {
                "document_id": CH1,
                "title": "1화 · 유리의 계절",
                "doc_type": "manuscript",
                "ordinal": 1,
                "quote": "서윤은 정원을 처음 보았다.",
                "quote_focus": None,
            },
            "verdict": None,
            "reason": None,
        },
        {
            "kind": "SETTING",
            "core": True,
            "confidence": "HIGH",
            "explanation": "북쪽 문은 낡은 열쇠로만 열리는데, 서윤이 열쇠 없이 문을 밀어요. "
            "3화까지 서윤이 낡은 열쇠를 손에 넣는 장면이 없어요.",
            "quote": "서윤은 등불을 내려놓고 문을 밀었다.",
            "quote_focus": None,
            "source": {
                "document_id": GREENHOUSE,
                "title": "북쪽 온실",
                "doc_type": "place",
                "ordinal": None,
                "quote": FACT,
                "quote_focus": None,
            },
            "verdict": None,
            "reason": None,
        },
    ]

    judged = env.checker.judged
    assert len(judged) == 4
    assert all(
        "<settings>" in text and "레나의 눈은 검은색이다." in text for text in judged
    )
    assert not any("이미 열 번" in text for text in judged)
    assert not any("온실의 유리는 붉다" in text for text in judged)
    whole = env.checker.finds["setting_check_candidates"][0]["content"]
    assert f"<core>{FACT}</core>" in whole[0]["text"]
    assert (
        f'<document id="{short(CH1)}" kind="manuscript" number="1"' in whole[1]["text"]
    )
    assert '<author locale="en"/>' in whole[2]["text"]
    assert CHAPTER in whole[2]["text"]
    by_setting = env.checker.finds["setting_check_setting_candidates"][0]["content"]
    assert f'<document id="{short(GREENHOUSE)}" kind="place"' in by_setting[0]["text"]
    assert f"<core>{FACT}</core>" in by_setting[0]["text"]
    assert CHAPTER in by_setting[1]["text"]
    assert env.content.docs[CH1].body_text not in by_setting[1]["text"]

    assert env.query(
        "select status, model, input_tokens, cached_tokens, output_tokens "
        "from setting_check_runs"
    ) == [("DONE", "fake-checker", 6000, 2400, 300)]
    assert env.query("select count(*) from chat_usage") == [(0,)]
    stored = env.query(
        "select quote_focus, source_focus, fingerprint from setting_check_findings "
        "order by position"
    )
    assert [row[:2] for row in stored] == [
        ("은빛 눈", "검은색"),
        ("처음 왔다", None),
        (None, None),
    ]
    assert stored[0][2] == fingerprint(
        "SETTING", LENA, "레나의 눈은 검은색이다.", "레나는 은빛 눈으로 정원을 보았다."
    )


def test_summary_and_history_list_runs_without_findings(env):
    first = env.check()
    second = env.check()
    lena = env.check(CH2)

    summary = env.get(f"/projects/{env.project}/setting-checks/summary")
    assert summary["running"] is None
    assert [c["document_id"] for c in summary["chapters"]] == [CH2, CH3]
    for chapter in summary["chapters"]:
        assert_run_shape(chapter["latest"], findings=False)
    assert summary["chapters"][1]["latest"]["id"] == second["id"]
    assert summary["chapters"][0]["latest"]["id"] == lena["id"]

    runs = env.get(f"/projects/{env.project}/setting-checks", document_id=CH3)["runs"]
    assert [run["id"] for run in runs] == [second["id"], first["id"]]
    for run in runs:
        assert_run_shape(run, findings=False)
    assert runs[0]["counts"] == {"open": 3, "intended": 0, "not_conflict": 0}

    everything = env.get(f"/projects/{env.project}/setting-checks")["runs"]
    assert len(everything) == 3
    assert env.get(f"/projects/{uuid4()}/setting-checks/summary") == {
        "running": None,
        "chapters": [],
    }


def test_verdicts_follow_the_fingerprint_into_later_runs(env):
    run = env.check()
    lena, manuscript, door = run["findings"]

    response = env.verdict(door["id"], "INTENDED", f"  {REASON}  ")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {**door, "verdict": "INTENDED", "reason": REASON}
    assert env.verdict(lena["id"], "NOT_CONFLICT").json()["reason"] is None
    assert env.get(f"/setting-checks/{run['id']}")["counts"] == {
        "open": 1,
        "intended": 1,
        "not_conflict": 1,
    }

    again = env.check()

    assert again["counts"] == {"open": 1, "intended": 1, "not_conflict": 1}
    assert [(f["verdict"], f["reason"]) for f in again["findings"]] == [
        ("NOT_CONFLICT", None),
        (None, None),
        ("INTENDED", REASON),
    ]
    door_judgement = next(text for text in env.checker.judged[4:] if FACT in text)
    assert f"<reason>{REASON}</reason>" in door_judgement
    assert '<verdict value="NOT_CONFLICT">' not in door_judgement
    manuscript_judgement = next(
        text
        for text in env.checker.judged[4:]
        if "처음 왔다" in text.split("<candidate")[-1]
    )
    assert "<verdicts/>" in manuscript_judgement

    cleared = env.client.delete(
        f"/setting-check-findings/{again['findings'][2]['id']}/verdict",
        headers=env.headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["verdict"] is None and cleared.json()["reason"] is None
    assert env.get(f"/setting-checks/{run['id']}")["counts"] == {
        "open": 2,
        "intended": 0,
        "not_conflict": 1,
    }
    assert env.query("select count(*) from setting_check_verdicts") == [(1,)]


def test_the_verdict_ledger_lists_each_verdict_with_its_latest_finding(env):
    lena, _, door = env.check()["findings"]
    env.verdict(door["id"], "INTENDED", REASON)
    env.verdict(lena["id"], "NOT_CONFLICT")
    again = env.check()["findings"]
    path = f"/projects/{env.project}/setting-checks/verdicts"

    ledger = env.get(path)["verdicts"]

    assert [entry["finding"] for entry in ledger] == [again[0], again[2]]
    for entry in ledger:
        assert set(entry) == {"document_id", "document_title", "decided_at", "finding"}
        assert entry["document_id"] == CH3
        assert entry["document_title"] == "3화 · 균열의 밤"
    assert ledger[1]["finding"]["verdict"] == "INTENDED"
    assert ledger[1]["finding"]["reason"] == REASON
    assert ledger[0]["decided_at"] > ledger[1]["decided_at"]

    env.client.delete(
        f"/setting-check-findings/{lena['id']}/verdict", headers=env.headers
    )
    assert [entry["finding"]["id"] for entry in env.get(path)["verdicts"]] == [
        again[2]["id"]
    ]
    stranger = env.client.get(path, headers={"X-User-Id": str(uuid4())})
    assert (stranger.status_code, stranger.json()) == (200, {"verdicts": []})
    assert env.get(f"/projects/{uuid4()}/setting-checks/verdicts") == {"verdicts": []}


def test_verdict_requests_are_validated_and_private(env):
    finding = env.check()["findings"][0]["id"]

    for body in (
        {"verdict": "INTENDED", "reason": "가" * 501},
        {"verdict": "MAYBE", "reason": None},
        {"verdict": "INTENDED", "reason": None, "extra": 1},
    ):
        response = env.client.put(
            f"/setting-check-findings/{finding}/verdict", json=body, headers=env.headers
        )
        assert (response.status_code, response.json()["code"]) == (
            400,
            "INVALID_REQUEST",
        )
    assert env.verdict(finding, "INTENDED", "가" * 500).status_code == 200
    assert env.verdict(finding, "INTENDED", "   ").json()["reason"] is None

    for target, user in ((finding, uuid4()), (str(uuid4()), None), ("nope", None)):
        response = env.verdict(target, "INTENDED", user=user)
        assert (response.status_code, response.json()["code"]) == (
            404,
            "SETTING_CHECK_FINDING_NOT_FOUND",
        )
    stranger = env.client.delete(
        f"/setting-check-findings/{finding}/verdict",
        headers={"X-User-Id": str(uuid4())},
    )
    assert stranger.status_code == 404


def test_one_running_check_per_project_and_cancel_saves_nothing(env):
    env.checker.gate = threading.Event()
    started = env.start().json()
    deadline = time.monotonic() + 5
    while env.get(f"/setting-checks/{started['id']}")["stage"] != "FINDING":
        assert time.monotonic() < deadline
        time.sleep(0.02)

    busy = env.start(CH2)
    assert (busy.status_code, busy.json()["code"]) == (409, "SETTING_CHECK_IN_PROGRESS")
    summary = env.get(f"/projects/{env.project}/setting-checks/summary")
    assert summary["running"]["id"] == started["id"]
    assert_run_shape(summary["running"], findings=False)
    assert summary["running"]["read"]["manuscripts"] == 2

    stranger = env.client.post(
        f"/setting-checks/{started['id']}/cancel", headers={"X-User-Id": str(uuid4())}
    )
    assert (stranger.status_code, stranger.json()["code"]) == (
        404,
        "SETTING_CHECK_NOT_FOUND",
    )
    cancel = env.client.post(
        f"/setting-checks/{started['id']}/cancel", headers=env.headers
    )
    assert cancel.status_code == 204
    assert cancel.headers["cache-control"] == "no-store"
    env.checker.gate.set()
    run = env.finish(started["id"])

    assert run["status"] == "CANCELLED"
    assert run["stage"] is None and run["finished_at"] is not None
    assert run["findings"] == []
    assert env.query("select count(*) from setting_check_findings") == [(0,)]
    again = env.client.post(
        f"/setting-checks/{started['id']}/cancel", headers=env.headers
    )
    assert again.status_code == 204
    assert env.start(CH2).status_code == 202


def test_start_errors(env):
    for document, code, status in (
        (GREENHOUSE, "SETTING_CHECK_DOCUMENT_NOT_FOUND", 404),
        (str(uuid4()), "SETTING_CHECK_DOCUMENT_NOT_FOUND", 404),
    ):
        response = env.start(document)
        assert (response.status_code, response.json()["code"]) == (status, code)

    other = env.client.post(
        f"/projects/{uuid4()}/setting-checks",
        json={"document_id": CH3},
        headers=env.headers,
    )
    assert (other.status_code, other.json()["code"]) == (404, "PROJECT_NOT_FOUND")
    for body in ({}, {"document_id": "nope"}, {"document_id": CH3, "x": 1}):
        response = env.client.post(
            f"/projects/{env.project}/setting-checks", json=body, headers=env.headers
        )
        assert (response.status_code, response.json()["code"]) == (
            400,
            "INVALID_REQUEST",
        )
    anonymous = env.client.post(
        f"/projects/{env.project}/setting-checks", json={"document_id": CH3}
    )
    assert (anonymous.status_code, anonymous.json()["code"]) == (
        401,
        "USER_CONTEXT_REQUIRED",
    )
    assert env.query("select count(*) from setting_check_runs") == [(0,)]

    run = env.check()
    for path in (f"/setting-checks/{uuid4()}", "/setting-checks/nope"):
        response = env.client.get(path, headers=env.headers)
        assert (response.status_code, response.json()["code"]) == (
            404,
            "SETTING_CHECK_NOT_FOUND",
        )
    hidden = env.client.get(
        f"/setting-checks/{run['id']}", headers={"X-User-Id": str(uuid4())}
    )
    assert hidden.status_code == 404


def test_failures_are_recorded_on_the_run(env):
    env.checker.fail_with = "MODEL_UNAVAILABLE"
    failed = env.check()

    assert (failed["status"], failed["failure_code"]) == (
        "FAILED",
        "CHAT_MODEL_UNAVAILABLE",
    )
    assert failed["findings"] == [] and failed["stage"] is None

    env.checker.fail_with = None

    async def unavailable(*_):
        raise ChatFailure("CONTENT_UNAVAILABLE")

    env.content.documents = unavailable
    assert env.check()["failure_code"] == "CONTENT_UNAVAILABLE"


def test_without_core_facts_both_passes_run_and_nothing_is_core(env):
    env.content.docs[GREENHOUSE] = replace(env.content.docs[GREENHOUSE], core_facts=())
    run = env.check()

    assert sorted(env.checker.finds) == [
        "setting_check_candidates",
        "setting_check_setting_candidates",
    ]
    assert run["read"]["core_facts"] == 0
    assert all(not finding["core"] for finding in run["findings"])


def test_oldest_chapters_are_dropped_past_the_size_limit(database):
    docs = FakeContent(uuid4(), uuid4())
    story(docs)
    limit = sum(docs.docs[i].chars for i in (CH3, CH2, LENA, GREENHOUSE))
    with make_env(database, setting_check_max_chars=limit) as env:
        run = env.check()

    assert run["read"]["manuscripts"] == 1
    assert (run["read"]["from_ordinal"], run["read"]["to_ordinal"]) == (2, 2)
    earlier = env.checker.finds["setting_check_candidates"][0]["content"][1]["text"]
    assert env.content.docs[CH1].body_text not in earlier
    assert env.content.docs[CH2].body_text in earlier
    assert [finding["source"]["document_id"] for finding in run["findings"]] == [
        LENA,
        GREENHOUSE,
    ]


def test_too_large_settings_fail_the_run(database):
    with make_env(database, setting_check_max_chars=10) as env:
        run = env.check()

    assert (run["status"], run["failure_code"]) == ("FAILED", "CHAT_CONTEXT_TOO_LARGE")


def test_documents_are_fetched_in_batches_of_100(env):
    for n in range(150):
        doc = Document(str(uuid4()), f"설정 {n}", "ITEM", "본문", 1)
        env.content.docs[doc.id] = doc
    run = env.check()

    assert [len(ids) for ids, _ in env.content.reads] == [100, 55]
    assert env.content.reads[0][0][:3] == [CH3, CH1, CH2]
    assert run["read"]["settings"] == 152


def test_stale_running_checks_are_interrupted(env):
    stale = env.query(
        "insert into setting_check_runs (project_id, user_id, document_id, "
        "document_title, revision_no, status, stage, created_at) values "
        "(%s, %s, %s, 't', 1, 'RUNNING', 'FINDING', now() - interval '1 hour') "
        "returning id",
        uuid4(),
        env.user,
        CH3,
    )[0][0]
    with TestClient(create_app(env.settings, env.content, FakeModel(), env.checker)):
        pass
    assert env.query(
        "select status, stage, failure_code from setting_check_runs where id = %s",
        stale,
    ) == [("FAILED", None, "INTERRUPTED")]

    env.query(
        "insert into setting_check_runs (project_id, user_id, document_id, "
        "document_title, revision_no, status, created_at) values "
        "(%s, %s, %s, 't', 1, 'RUNNING', now() - interval '1 hour')",
        env.project,
        env.user,
        CH3,
    )
    response = env.start()
    assert response.status_code == 202
    assert env.finish(response.json()["id"])["status"] == "DONE"


def test_a_fresh_running_check_blocks_the_project(env):
    env.query(
        "insert into setting_check_runs (project_id, user_id, document_id, "
        "document_title, revision_no, status) values (%s, %s, %s, 't', 1, 'RUNNING')",
        env.project,
        env.user,
        CH2,
    )

    response = env.start()

    assert (response.status_code, response.json()["code"]) == (
        409,
        "SETTING_CHECK_IN_PROGRESS",
    )


def test_deleting_data_removes_checks_and_stops_running_ones(env):
    run = env.check()
    env.verdict(run["findings"][0]["id"], "INTENDED", REASON)
    other_project, stranger = uuid4(), uuid4()
    for project, user in ((other_project, env.user), (env.project, stranger)):
        env.query(
            "insert into setting_check_runs (project_id, user_id, document_id, "
            "document_title, revision_no, status) values (%s, %s, %s, 't', 1, 'DONE')",
            project,
            user,
            CH3,
        )
        env.query(
            "insert into setting_check_verdicts (project_id, user_id, fingerprint, "
            "verdict, source_document_id, source_quote) "
            "values (%s, %s, 'f', 'INTENDED', %s, 'q')",
            project,
            user,
            CH3,
        )
    env.checker.gate = threading.Event()
    running = env.start().json()
    deadline = time.monotonic() + 5
    while (
        not env.runner.tasks
        or env.get(f"/setting-checks/{running['id']}")["stage"] != "FINDING"
    ):
        assert time.monotonic() < deadline
        time.sleep(0.02)

    deleted = env.client.delete(
        f"/projects/{env.project}/chat/data", headers=env.headers
    )
    assert deleted.status_code == 204
    deadline = time.monotonic() + 5
    while env.runner.tasks:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    env.checker.gate.set()

    rows = sorted([(other_project, env.user), (env.project, stranger)])
    assert (
        sorted(env.query("select project_id, user_id from setting_check_runs")) == rows
    )
    assert (
        sorted(env.query("select project_id, user_id from setting_check_verdicts"))
        == rows
    )
    assert env.query("select count(*) from setting_check_findings") == [(0,)]

    assert env.client.delete("/users/me/data", headers=env.headers).status_code == 204
    assert env.query("select user_id from setting_check_runs") == [(stranger,)]
    assert env.query("select user_id from setting_check_verdicts") == [(stranger,)]
