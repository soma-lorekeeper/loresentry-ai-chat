from dataclasses import dataclass

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logs import AppLogger, LogField

NO_STORE = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class Contract:
    status: int
    message: str
    next_action: str = "NONE"


CONTRACTS: dict[str, Contract] = {
    "USER_CONTEXT_REQUIRED": Contract(401, "User context is required", "RELOGIN"),
    "INVALID_REQUEST": Contract(400, "The request is invalid"),
    "CHAT_SESSION_NOT_FOUND": Contract(404, "Chat session not found"),
    "PROJECT_NOT_FOUND": Contract(404, "Project not found"),
    "CHAT_TITLE_REQUIRED": Contract(400, "A chat title is required"),
    "CHAT_QUESTION_REQUIRED": Contract(400, "A question or a quote is required"),
    "CHAT_NOT_LAST_QUESTION": Contract(409, "Only the last question can be replaced"),
    "CHAT_ANSWER_IN_PROGRESS": Contract(409, "An answer is already being written"),
    "CHAT_ANOTHER_ANSWER_IN_PROGRESS": Contract(
        409, "An answer is being written in another conversation"
    ),
    "CHAT_CONTEXT_TOO_LARGE": Contract(422, "The project is too large to read at once"),
    "CHAT_DAILY_LIMIT_REACHED": Contract(429, "Today's chat usage is used up"),
    "SETTING_CHECK_NOT_FOUND": Contract(404, "Setting check not found"),
    "SETTING_CHECK_FINDING_NOT_FOUND": Contract(404, "Setting check finding not found"),
    "SETTING_CHECK_DOCUMENT_NOT_FOUND": Contract(
        404, "The chapter to check was not found"
    ),
    "SETTING_CHECK_IN_PROGRESS": Contract(
        409, "A setting check is already running in this project"
    ),
    "CHAT_MODEL_UNAVAILABLE": Contract(
        503, "The model is temporarily unavailable", "RETRY_LATER"
    ),
    "CONTENT_UNAVAILABLE": Contract(
        503, "Project content is temporarily unavailable", "RETRY_LATER"
    ),
    "INTERNAL_ERROR": Contract(500, "Unexpected server error", "RETRY_LATER"),
}


class ChatFailure(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail

    @property
    def contract(self) -> Contract:
        return CONTRACTS[self.code]

    def body(self) -> dict[str, str]:
        contract = self.contract
        return {
            "code": self.code,
            "message": contract.message,
            "next_action": contract.next_action,
        }


def failure_response(failure: ChatFailure) -> JSONResponse:
    # 문의 받은 trace_id로 Kibana에서 이 요청의 줄을 바로 찾는다
    return JSONResponse(
        status_code=failure.contract.status,
        content={**failure.body(), "trace_id": AppLogger.current().trace_id},
        headers=NO_STORE,
    )


# 예외 처리기는 실패를 따로 찍지 않는다. 이유를 이 요청의 결과 줄(request completed)에 맡겨, 실패 요청 하나의 결과가 한 줄이다


async def handle_failure(_: Request, failure: ChatFailure) -> JSONResponse:
    AppLogger.current().add_to_outcome(LogField.error_type(failure.code))
    return failure_response(failure)


async def handle_http(request: Request, error: HTTPException) -> Response:
    # 맞는 route가 없는 404, 메서드가 다른 405 같은 프레임워크 응답. 본문은 FastAPI 기본 그대로 둔다
    AppLogger.current().add_to_outcome(LogField.error_type(type(error).__name__))
    return await http_exception_handler(request, error)


async def handle_validation(_: Request, __: RequestValidationError) -> JSONResponse:
    AppLogger.current().add_to_outcome(LogField.error_type("INVALID_REQUEST"))
    return failure_response(ChatFailure("INVALID_REQUEST"))


async def handle_unexpected(_: Request, error: Exception) -> JSONResponse:
    # 예외 메시지에는 SQL·요청 값이 섞일 수 있어 클래스 이름과 메시지 없는 스택만 맡긴다
    AppLogger.current().add_to_outcome(
        LogField.error_type(type(error).__name__), LogField.safe_stack_trace(error)
    )
    return failure_response(ChatFailure("INTERNAL_ERROR"))


class UnexpectedErrorMiddleware:
    """예상하지 못한 예외를 INTERNAL_ERROR 응답으로 바꾸는 순수 ASGI middleware. 요청 로그 middleware 안쪽에 둔다.

    FastAPI는 Exception 처리기를 맨 바깥 ServerErrorMiddleware에서 부른다. 그 자리는 요청 로그 middleware 바깥이라 결과 줄에
    이유를 맡길 수 없고, 처리기가 응답한 뒤에도 예외를 다시 던져 uvicorn이 traceback 줄을 한 번 더 찍는다. 그래서 같은 일을
    요청 로그 middleware 안쪽에서 하고 예외를 삼킨다. 응답을 시작한 뒤의 예외는 바꿀 수 없어 그대로 던진다(요청 로그
    middleware가 request failed with an unhandled exception을 남긴다).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def sending(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, sending)
        except Exception as error:
            if started:
                raise
            response = await handle_unexpected(Request(scope), error)
            await response(scope, receive, send)
