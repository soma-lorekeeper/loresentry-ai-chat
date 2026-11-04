from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.content import ContentSource
from app.errors import NO_STORE, ChatFailure
from app.logs import AppLogger, LogField, skip_entry_log
from app.prompt import Profile, Question
from app.setting_check.runner import CheckRunner
from app.settings import Settings
from app.store import Replayed, Store
from app.text import default_title
from app.turns import Channel, TurnRunner

QUESTION_LIMIT = 4000
TITLE_LIMIT = 200
SEARCH_LIMIT = 100
STREAM_HEADERS = {**NO_STORE, "X-Accel-Buffering": "no"}
# 로그에 그대로 남겨도 되는 코드 값(목록 밖은 other)
LOCALES = {"ko", "en"}
INTENTS = {"ask", "explain", "related", "flow"}

router = APIRouter()


@dataclass
class Services:
    settings: Settings
    store: Store
    content: ContentSource
    runner: TurnRunner
    checks: CheckRunner


@skip_entry_log("Request dependency that only returns the app services")
def services(request: Request) -> Services:
    return request.app.state.services


@skip_entry_log("Request dependency that only reads X-User-Id")
def current_user(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> UUID:
    if not x_user_id:
        raise ChatFailure("USER_CONTEXT_REQUIRED")
    try:
        return UUID(x_user_id)
    except ValueError:
        raise ChatFailure("INVALID_REQUEST") from None


@skip_entry_log("Request dependency that only reads the profile headers")
def profile(
    x_user_locale: Annotated[str | None, Header(alias="X-User-Locale")] = None,
    x_user_display_name: Annotated[
        str | None, Header(alias="X-User-Display-Name")
    ] = None,
) -> Profile:
    locale = x_user_locale if x_user_locale in {"ko", "en"} else None
    name = unquote(x_user_display_name) if x_user_display_name else None
    return Profile(display_name=name, locale=locale)


@skip_entry_log("Only parses a path id")
def uuid_or(value: str, code: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ChatFailure(code) from None


User = Annotated[UUID, Depends(current_user)]
Deps = Annotated[Services, Depends(services)]


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: UUID
    title: str = Field(max_length=255)
    doc_type: str = Field(max_length=40)
    text: str = Field(min_length=1, max_length=2000)


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(default="", max_length=QUESTION_LIMIT)
    intent: Literal["ask", "explain", "related", "flow"] = "ask"
    quote: QuoteIn | None = None
    focus_file_id: UUID | None = None
    replaces: UUID | None = None
    client_message_id: UUID


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


@skip_entry_log("Only builds a response")
def no_store(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


@router.get("/projects/{project_id}/chat/availability")
async def availability(project_id: str, user: User, deps: Deps) -> JSONResponse:
    AppLogger.current().info(
        "chat availability get requested", LogField.project_id(project_id)
    )
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    size = await deps.content.size(user, project)
    return no_store({"status": "available", "size": size, "limit": size})


@router.get("/chat/usage")
async def usage(user: User, deps: Deps) -> JSONResponse:
    AppLogger.current().info("chat usage get requested")
    return no_store(await deps.runner.usage(user))


@router.get("/projects/{project_id}/chat/sessions")
async def list_sessions(project_id: str, user: User, deps: Deps) -> JSONResponse:
    AppLogger.current().info(
        "chat sessions list requested", LogField.project_id(project_id)
    )
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    return no_store({"sessions": await deps.store.sessions(user, project)})


@router.post("/projects/{project_id}/chat/sessions")
async def create_session(
    project_id: str,
    user: User,
    deps: Deps,
    who: Annotated[Profile, Depends(profile)],
) -> JSONResponse:
    AppLogger.current().info(
        "chat session create requested",
        LogField.project_id(project_id),
        LogField.code("locale", who.locale, LOCALES),
    )
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    await deps.content.size(user, project)
    session = await deps.store.create_session(user, project, default_title(who.locale))
    return no_store(session, 201)


@router.get("/projects/{project_id}/chat/search")
async def search(
    project_id: str,
    user: User,
    deps: Deps,
    q: Annotated[str, Query()] = "",
) -> JSONResponse:
    # 검색어는 내용 대신 길이만 남는다
    AppLogger.current().info(
        "chat search requested", LogField.project_id(project_id), LogField.param("q", q)
    )
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    term = q.strip()
    if len(term) > SEARCH_LIMIT:
        raise ChatFailure("INVALID_REQUEST")
    hits = await deps.store.search(user, project, term) if term else []
    return no_store({"hits": hits})


@router.patch("/chat/sessions/{session_id}")
async def rename_session(
    session_id: str, body: RenameIn, user: User, deps: Deps
) -> JSONResponse:
    AppLogger.current().info(
        "chat session rename requested",
        LogField.chat_session_id(session_id),
        LogField.param("title", body.title),
    )
    session = uuid_or(session_id, "CHAT_SESSION_NOT_FOUND")
    title = body.title.strip()
    if not title:
        raise ChatFailure("CHAT_TITLE_REQUIRED")
    if len(title) > TITLE_LIMIT:
        raise ChatFailure("INVALID_REQUEST")
    return no_store(await deps.store.rename(user, session, title))


@router.delete("/chat/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, user: User, deps: Deps) -> Response:
    AppLogger.current().info(
        "chat session delete requested", LogField.chat_session_id(session_id)
    )
    session = uuid_or(session_id, "CHAT_SESSION_NOT_FOUND")
    deps.runner.stop(await deps.store.delete_session(user, session))
    return Response(status_code=204, headers=NO_STORE)


@router.get("/chat/sessions/{session_id}/messages")
async def list_messages(session_id: str, user: User, deps: Deps) -> JSONResponse:
    AppLogger.current().info(
        "chat messages list requested", LogField.chat_session_id(session_id)
    )
    session = uuid_or(session_id, "CHAT_SESSION_NOT_FOUND")
    await deps.store.session(user, session)
    return no_store(
        {
            "messages": await deps.store.messages(session),
            "generating": await deps.store.generating(session),
        }
    )


@router.post("/chat/sessions/{session_id}/messages")
async def send(
    session_id: str,
    body: SendIn,
    user: User,
    deps: Deps,
    who: Annotated[Profile, Depends(profile)],
) -> Response:
    # 질문·인용은 내용 대신 길이와 있는지만 남는다
    AppLogger.current().info(
        "chat message send requested",
        LogField.chat_session_id(session_id),
        LogField.code("intent", body.intent, INTENTS),
        LogField.param("content", body.content),
        LogField.param("quoted", body.quote is not None),
        LogField.param("focus_file_id", body.focus_file_id),
        LogField.param("replaced_message_id", body.replaces),
        LogField.param("client_message_id", body.client_message_id),
    )
    session = uuid_or(session_id, "CHAT_SESSION_NOT_FOUND")
    content = body.content.strip()
    quote = body.quote.model_dump(mode="json") if body.quote else None
    if not content and quote is None:
        raise ChatFailure("CHAT_QUESTION_REQUIRED")
    row = await deps.store.session(user, session)
    await deps.content.size(user, row["project_id"])
    limit = deps.settings.daily_credit_limit
    if limit > 0 and await deps.store.used_today(user) >= limit:
        raise ChatFailure("CHAT_DAILY_LIMIT_REACHED")
    started = await deps.store.begin_turn(
        user,
        session,
        content,
        body.intent,
        quote,
        body.replaces,
        body.client_message_id,
    )
    if isinstance(started, Replayed):
        # 같은 요청을 다시 보냈다. 저장한 답을 그대로 돌려준다
        AppLogger.current().info("chat answer replayed")
        channel = Channel()
        channel.put(
            "done",
            {
                "question": started.question,
                "answer": started.answer,
                "usage": await deps.runner.usage(user),
            },
        )
    else:
        question = Question(
            content=started.question["content_md"],
            intent=started.question["intent"] or "ask",
            quote=started.question["quote"],
        )
        channel = deps.runner.start(started, user, who, question)
    return StreamingResponse(
        channel.events(deps.settings.heartbeat_seconds),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


@router.post("/chat/sessions/{session_id}/cancel", status_code=204)
async def cancel(session_id: str, user: User, deps: Deps) -> Response:
    AppLogger.current().info(
        "chat answer cancel requested", LogField.chat_session_id(session_id)
    )
    session = uuid_or(session_id, "CHAT_SESSION_NOT_FOUND")
    await deps.store.session(user, session)
    deps.runner.stop(await deps.store.cancel(user, session))
    return Response(status_code=204, headers=NO_STORE)


@router.delete("/users/me/data", status_code=204)
async def delete_user_data(user: User, deps: Deps) -> Response:
    AppLogger.current().info("user chat data delete requested")
    deps.runner.stop(await deps.store.delete_user_data(user))
    deps.checks.stop_for(user)
    return Response(status_code=204, headers=NO_STORE)


@router.delete("/projects/{project_id}/chat/data", status_code=204)
async def delete_project_data(project_id: str, user: User, deps: Deps) -> Response:
    AppLogger.current().info(
        "project chat data delete requested", LogField.project_id(project_id)
    )
    try:
        project = UUID(project_id)
    except ValueError:
        return Response(status_code=204, headers=NO_STORE)
    deps.runner.stop(await deps.store.delete_project_data(user, project))
    deps.checks.stop_for(user, project)
    return Response(status_code=204, headers=NO_STORE)
