from fastapi import FastAPI

app = FastAPI(title="ai-chat-api")


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "ai-chat-api"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
