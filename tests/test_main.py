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
    monkeypatch.setattr(
        db,
        "check",
        lambda: {
            "database": "ai_chat",
            "username": "ai_chat_svc",
            "version": "PostgreSQL 18.6",
        },
    )

    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ai-chat-api",
        "database": "ai_chat",
        "username": "ai_chat_svc",
        "version": "PostgreSQL 18.6",
    }


def test_health_db_reports_unreachable_database(monkeypatch):
    def fail() -> dict[str, str]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(db, "check", fail)

    response = client.get("/health/db")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "service": "ai-chat-api",
        "error": "connection refused",
    }
