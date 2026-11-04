"""gateway가 부른 요청 하나의 시작점 ``RequestLogMiddleware``(LOG_SCHEMA.md 5절 "접근 로그").

순수 ASGI middleware라 스트리밍(SSE) 응답을 버퍼링하지 않고 contextvars 문맥도 새로 만들지 않는다. 그래서 route 함수와 그
함수가 만든 asyncio task가 이 요청의 로그 객체를 그대로 본다.

순서: OpenTelemetry FastAPI 계측이 gateway가 보낸 traceparent를 이어 서버 span을 연 뒤, 그 안쪽(사용자 middleware 자리)에서
이 middleware가 돈다. 그래서 이 요청의 모든 줄이 gateway 줄과 같은 trace.id를 갖는다. 예외 처리기(ChatFailure, 검증 실패,
그 밖의 예외)는 이 middleware 안쪽의 ExceptionMiddleware에서 돌아 결과 줄에 이유를 맡긴다.

하는 일
1. 지금 span의 trace·span ID와 gateway가 확인해 보낸 사용자(X-User-Id, UUID일 때만)로 로그 객체를 만든다. 요청 하나의
   모든 줄에 받은 경로(url.path), 경로 템플릿(http.route), 메서드(http.request.method)를 붙인다. 경로 템플릿은 FastAPI가
   나중에 고를 route를 요청 시작 때 미리 찾는다(맞는 route가 없는 404에는 없다). 헬스체크(/health, /health/...)는 모든 줄에
   tags: health를 붙여, 정상(info) 줄은 Logstash가 버린다.
2. ``request received``와 ``request completed``(상태, 걸린 시간, 예외 처리기가 맡긴 이유)를 남긴다. 레벨은 5xx error,
   4xx warn, 그 밖 info다. 스트리밍 응답은 본문이 끝나거나(또는 끊겨) 응답 함수가 돌아올 때 결과 줄을 쓴다.
3. 예외 처리기도 잡지 못한 예외는 결과 줄 대신 ``request failed with an unhandled exception``(error, error.type, 메시지 없는
   스택, 상태 500)을 남기고 예외를 그대로 다시 던진다.

쿼리스트링은 남기지 않는다(검색어). 경로만 url.path에 넣는다.
"""

import logging
import sys
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from uuid import UUID

from fastapi.routing import iter_route_contexts
from starlette.routing import Match

from app.logs._fields import LogField, duration
from app.logs._logger import AppLogger, Origin, bind, new_logger, origin_of, reset

# 접근 로그 줄의 위치는 우리 서비스 코드가 아니라 웹 서버가 부르는 이 middleware다(LOG_SCHEMA.md 5절 "레이어와 위치")
LOG_LAYER = "filter"
# gateway가 세션을 확인한 뒤 넣는 사용자 헤더(ASGI 헤더 이름은 소문자 bytes)
USER_HEADER = b"x-user-id"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestLogMiddleware:
    """요청마다 로그 객체를 만들어 바인딩하고 접근 로그(받음·결과)를 남기는 순수 ASGI middleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = origin_of(sys._getframe(0))
        path = scope.get("path", "")
        logger = new_logger(user_of(scope)).with_fields(
            LogField.url_path(path),
            LogField.route(route_of(scope)),
            LogField.http_method(scope.get("method")),
        )
        if path == "/health" or path.startswith("/health/"):
            logger = logger.with_tag("health")
        token = bind(logger)
        started = time.perf_counter()
        # 응답 상태. 응답을 시작하기 전에 끝나면 None이다
        status: int | None = None
        # 결과 줄을 이미 남겼는가(처리되지 않은 예외 줄)
        written = False
        # 취소(클라이언트가 끊음, 서버 종료)로 끝났으면 그 예외 클래스 이름
        stopped: str | None = None

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            logger.emit(origin, logging.INFO, "request received", ())
            await self.app(scope, receive, sending)
        except Exception as failure:
            # 예외 처리기가 잡지 못한 예외(응답을 시작한 뒤의 예외 등). 예외 메시지에는 요청 값·SQL이 섞일 수 있어
            # 클래스 이름과 메시지 없는 스택만 남긴다. 같은 이름은 먼저 넣은 값이 남으므로 error.type을 맡겨진 필드보다 앞에 둔다
            written = True
            logger.emit(
                origin,
                logging.ERROR,
                "request failed with an unhandled exception",
                (
                    LogField.error_type(type(failure).__name__),
                    LogField.safe_stack_trace(failure),
                    LogField.status_code(500),
                    duration(time.perf_counter() - started),
                    *logger.outcome_fields(),
                ),
            )
            raise
        except BaseException as cancelled:
            stopped = type(cancelled).__name__
            raise
        finally:
            if not written:
                completed(logger, origin, status, started, stopped)
            reset(token)


def completed(
    logger: AppLogger,
    origin: Origin,
    status: int | None,
    started: float,
    stopped: str | None,
) -> None:
    """결과 줄. 상태로 레벨을 정하고(5xx error, 4xx warn, 그 밖 info), 예외 처리기가 맡긴 이유를 붙인다.

    응답을 시작하기 전에 취소됐으면(상태 없음) warn에 그 예외 클래스 이름을 error.type으로 남긴다.
    url.path·http.route·http.request.method는 문맥 필드로 이미 붙어 있다.
    """
    fields = (
        LogField.status_code(status),
        duration(time.perf_counter() - started),
        LogField.error_type(stopped if status is None else None),
        *logger.outcome_fields(),
    )
    if status is None:
        level = logging.WARNING
    elif status >= 500:
        level = logging.ERROR
    elif status >= 400:
        level = logging.WARNING
    else:
        level = logging.INFO
    logger.emit(origin, level, "request completed", fields)


def route_of(scope: Scope) -> str | None:
    """FastAPI가 이 요청에 고를 경로 템플릿(예: /chat/sessions/{session_id}). 맞는 route가 없으면(404·405) None.

    Starlette가 middleware 앞에서 scope["app"]에 앱을 넣는다. include_router로 더한 route는 FastAPI가 묶어 두므로
    iter_route_contexts로 펼쳐(prefix까지 붙은 전체 템플릿) 맞춰 본다. matches는 scope를 바꾸지 않는다.
    """
    router = getattr(scope.get("app"), "router", None)
    for route in iter_route_contexts(getattr(router, "routes", ())):
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route.path
    return None


def user_of(scope: Scope) -> UUID | None:
    """gateway가 보낸 사용자. 값이 하나이고 표준 표기 UUID일 때만 쓴다. 틀린 값은 route가 400으로 거절한다."""
    values = [value for name, value in scope.get("headers", ()) if name == USER_HEADER]
    if len(values) != 1 or len(values[0]) != 36:
        return None
    try:
        return UUID(values[0].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
