from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_returns_service_name():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"service": "ai-chat-api"}


def test_health_db_reports_reachable_database(monkeypatch):
    monkeypatch.setattr(db, "check", lambda: "ai_chat")

    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ai-chat-api",
        "database": "ai_chat",
    }


def test_health_db_reports_unreachable_database(monkeypatch):
    def fail() -> str:
        raise ConnectionError("connection refused for user ai_chat_svc")

    monkeypatch.setattr(db, "check", fail)

    response = client.get("/health/db")

    assert response.status_code == 503
    body = response.json()
    assert len(body.pop("trace_id")) == 32
    assert body == {
        "status": "error",
        "service": "ai-chat-api",
        "message": "The database is unreachable",
    }
