from dataclasses import dataclass

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
    "CHAT_CONTEXT_TOO_LARGE": Contract(422, "The project is too large to read at once"),
    "CHAT_DAILY_LIMIT_REACHED": Contract(429, "Today's chat usage is used up"),
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
    return JSONResponse(
        status_code=failure.contract.status,
        content=failure.body(),
        headers=NO_STORE,
    )


async def handle_failure(_: Request, failure: ChatFailure) -> JSONResponse:
    return failure_response(failure)


async def handle_validation(_: Request, __: RequestValidationError) -> JSONResponse:
    return failure_response(ChatFailure("INVALID_REQUEST"))


async def handle_unexpected(_: Request, __: Exception) -> JSONResponse:
    return failure_response(ChatFailure("INTERNAL_ERROR"))
