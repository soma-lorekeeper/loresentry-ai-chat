from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db

app = FastAPI(title="ai-chat-api")

SERVICE = "ai-chat-api"


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
def health_db() -> JSONResponse:
    try:
        result = db.check()
    except Exception as exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "service": SERVICE, "error": str(exception)},
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": SERVICE, **result},
    )
