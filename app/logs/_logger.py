"""서비스 코드가 로그를 남기는 유일한 통로 ``AppLogger``(LOG_SCHEMA.md 5절 "로그 클래스").

서비스 코드가 쓰는 것은 ``AppLogger.current()``로 꺼낸 객체의 ``info``·``warn``·``error``, 흐름 중간에 알게 된 사용자를 알리는
``identify``, 결과 줄에 이유를 맡기는 ``add_to_outcome``, 에러 응답 본문에 싣는 ``trace_id``뿐이다. 객체를 만들고 바인딩하는 일
(``new_logger``, ``with_fields``, ``with_tag``, ``with_user``, ``bind``)은 로깅 팩토리(RequestLogMiddleware, LogFlow, LogScope)만 한다.
이 모듈(``app.logs._logger``)은 ruff로 서비스 코드에서 가져오지 못하게 막는다.

흐름
1. 시작점(HTTP 요청, Kafka 레코드, 스케줄 회차, 저장해 둔 작업)이 그때 아는 값(trace.id, span.id, user.id)으로 객체를 만든다.
2. ``bind``로 요청 범위 저장소(contextvars)에 넣는다. 같은 흐름의 어느 계층이든 ``AppLogger.current()``로 꺼낸다.
   asyncio task와 ``asyncio.to_thread``는 만들 때의 contextvars를 복사하므로 객체가 함께 옮겨 간다.
3. 줄마다 그 줄을 찍은 위치(모듈·함수·레이어·패키지)를 호출 프레임에서 읽어, 표준 ``logging`` 레코드에 실어 넘긴다.
   root logger의 ECS formatter(``_format``)가 JSON 한 줄로 만든다.

객체는 바꿀 수 없다. 나중에 아는 값은 ``with_fields`` 등이 만든 새 객체를 다시 ``bind``한다. 예외는 흐름 공유 칸(``_Shared``)
하나다. 흐름 하나(요청, Kafka 레코드, 스케줄 회차)의 객체들이 같은 칸을 함께 써서, 실패 이유(``add_to_outcome``)는 결과 줄에
붙고, 흐름 중간에 알게 된 사용자(``identify``)는 그 뒤의 모든 줄과 결과 줄에 붙는다.
"""

import logging
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar, Token
from types import CodeType, FrameType
from typing import Any, NamedTuple, TypeVar
from uuid import UUID

from app.logs._fields import LogField
from app.logs._trace import current_ids

# 표준 logging 레코드에 우리 줄의 내용을 싣는 속성 이름. formatter가 이 속성으로 우리 줄과 프레임워크 줄을 가른다
LINE_ATTR = "loresentry_line"
# 이 패키지 이름. 줄 위치를 찾을 때 이 패키지의 프레임은 건너뛴다
PACKAGE = "app.logs"
# 문맥 필드 중 같은 문맥에서 프레임워크(uvicorn, httpx, psycopg 등)가 찍은 줄에도 붙이는 이름(Java MDC_CONTEXT_KEYS와 같다).
# 흐름 하나 동안 바뀌지 않는 값이다. 목록 밖의 문맥 필드는 우리 줄에만 붙는다
FRAMEWORK_CONTEXT_KEYS = frozenset(
    {
        "http.request.method",
        "url.path",
        "http.route",
        "loresentry.schedule.name",
        "loresentry.kafka.topic",
        "loresentry.kafka.partition",
        "loresentry.kafka.offset",
        "loresentry.cloudevent.id",
        "loresentry.cloudevent.type",
        "loresentry.cloudevent.subject",
    }
)
# 모듈 이름 끝 → log.layer(LOG_SCHEMA.md 5절 "레이어와 위치"의 Python 판). 맞지 않는 모듈은 모듈에 LOG_LAYER 상수를 둔다
LAYERS = {
    "routes": "controller",
    "service": "service",
    "services": "service",
    "store": "repository",
    "repository": "repository",
    "client": "client",
    "content": "client",
    "model": "client",
    "llm": "client",
    "consumer": "consumer",
    "handlers": "consumer",
    "publisher": "publisher",
    "worker": "scheduler",
    "relay": "scheduler",
    "outbox": "scheduler",
}

F = TypeVar("F", bound=Callable[..., Any])


class Origin(NamedTuple):
    """줄 하나의 위치. log.logger·log.origin.function·log.layer·log.package가 된다."""

    logger: str
    function: str
    layer: str
    package: str
    path: str
    line: int


class Line(NamedTuple):
    """formatter에 넘기는 우리 줄 하나의 내용."""

    origin: Origin
    trace_id: str | None
    span_id: str | None
    user_id: str | None
    fields: tuple[LogField, ...]
    context: tuple[LogField, ...]
    tags: tuple[str, ...]


class _Shared:
    """흐름 하나의 공유 칸. 그 흐름에서 만든 객체(with_fields 등으로 만든 것 포함)가 함께 쓴다.

    채팅 답변처럼 asyncio task나 to_thread의 다른 스레드에서도 쓰므로, 시작 줄 꺼내기는 lock으로 한 번만 한다.
    """

    __slots__ = ("outcome", "user", "start", "lock")

    def __init__(self) -> None:
        # 결과 줄(request completed)에 붙일 필드(add_to_outcome)
        self.outcome: list[LogField] = []
        # 흐름 중간에 알게 된 사용자(identify). 모르면 None
        self.user: str | None = None
        # 이 흐름의 첫 줄 직전에 한 번 남길 시작 줄(LogFlow.polling의 늦은 시작 줄). 없으면 None
        self.start: Callable[[], None] | None = None
        self.lock = threading.Lock()

    def take_start(self) -> Callable[[], None] | None:
        with self.lock:
            start, self.start = self.start, None
        return start


class AppLogger:
    """로그 객체. 서비스 코드는 ``AppLogger.current()``로 꺼내 ``info``·``warn``·``error``만 부른다."""

    __slots__ = ("_trace_id", "_span_id", "_user_id", "_context", "_shared", "_tags")

    def __init__(
        self,
        trace_id: str | None,
        span_id: str | None,
        user_id: str | None,
        context: tuple[LogField, ...],
        shared: _Shared | None,
        tags: tuple[str, ...],
    ) -> None:
        # 시작점이 받은 trace(32 hex)·span(16 hex). 없으면 None이고, 그때는 줄을 찍을 때 열려 있는 span 값을 쓴다
        self._trace_id = trace_id
        self._span_id = span_id
        # 시작점이 아는 사용자(auth users.id). identify로 알게 된 사용자는 공유 칸에 있다
        self._user_id = user_id
        # 이 객체로 찍는 모든 줄에 붙는 필드(요청 하나의 url.path, Kafka 레코드 하나의 topic·offset 등)
        self._context = context
        # 흐름 공유 칸. 바인딩이 없는 기본 객체(DETACHED)는 None이라 아무것도 쌓이지 않는다
        self._shared = shared
        # 이 객체로 찍는 모든 줄의 tags(예: 헬스체크 요청의 health). 정해 둔 이름만 쓴다(LOG_SCHEMA.md 2.3절)
        self._tags = tags

    # ------------------------------------------------------------ 서비스 코드가 쓰는 것

    @classmethod
    def current(cls) -> "AppLogger":
        """지금 문맥에 바인딩된 객체. 없으면 trace 없는 기본 객체(DETACHED)라 None이 아니다."""
        return _CURRENT.get() or DETACHED

    @property
    def trace_id(self) -> str | None:
        """에러 응답 본문의 trace_id. 바인딩이 없거나 시작점에 span이 없었으면 지금 열려 있는 span의 값이다."""
        return self._trace_id or current_ids()[0]

    def info(self, message: str, *fields: LogField | None) -> None:
        """정상 동작의 진행 상황. 어느 계층에서 무엇을 처리 중인지와 도메인 ID를 남긴다."""
        self.emit(None, logging.INFO, message, fields)

    def warn(self, message: str, *fields: LogField | None) -> None:
        """처리했지만 예상 밖인 상황(재시도, 충돌, 검증 실패, 건너뛴 이벤트). error.type을 함께 넘긴다."""
        self.emit(None, logging.WARNING, message, fields)

    def error(self, message: str, *fields: LogField | None) -> None:
        """작업 하나의 실패. error.type과, 예외가 있으면 LogField.safe_stack_trace를 함께 넘긴다."""
        self.emit(None, logging.ERROR, message, fields)

    def identify(self, user_id: UUID | None) -> None:
        """흐름 중간에 알게 된 사용자를 알린다. 이 뒤의 모든 줄, 결과 줄, 같은 문맥의 프레임워크 줄에 user.id가 붙는다."""
        if self._shared is not None and user_id is not None:
            self._shared.user = str(user_id)

    def add_to_outcome(self, *fields: LogField | None) -> None:
        """이 흐름의 결과 줄(request completed)에 붙일 필드를 맡긴다.

        예외 처리기는 실패를 따로 찍지 않고 이유(우리 오류 코드)를 여기에 맡긴다. 그래서 실패 요청 하나의 결과는 한 줄이고,
        error 줄 수가 실패 요청 수와 같다. 바인딩된 흐름이 없으면 아무것도 하지 않는다.
        """
        if self._shared is not None:
            self._shared.outcome.extend(field for field in fields if field is not None)

    # ------------------------------------------------------------ 로깅 팩토리만 쓰는 것

    def with_fields(self, *fields: LogField | None) -> "AppLogger":
        """이 객체로 찍는 모든 줄에 붙일 필드를 더한 새 객체. 같은 공유 칸을 쓴다."""
        added = tuple(field for field in fields if field is not None)
        return AppLogger(
            self._trace_id,
            self._span_id,
            self._user_id,
            self._context + added,
            self._shared,
            self._tags,
        )

    def with_tag(self, tag: str) -> "AppLogger":
        """이 객체로 찍는 모든 줄의 tags에 값을 더한 새 객체."""
        if tag in self._tags:
            return self
        return AppLogger(
            self._trace_id,
            self._span_id,
            self._user_id,
            self._context,
            self._shared,
            (*self._tags, tag),
        )

    def outcome_fields(self) -> tuple[LogField, ...]:
        """맡겨진 결과 필드. 결과 줄을 쓰는 RequestLogMiddleware가 읽는다."""
        return tuple(self._shared.outcome) if self._shared is not None else ()

    def before_first_line(self, start: Callable[[], None]) -> None:
        """이 흐름의 첫 줄 직전에 한 번 부를 일을 맡긴다(LogFlow.polling의 늦은 시작 줄)."""
        if self._shared is not None:
            self._shared.start = start

    def framework_fields(self) -> list[tuple[str, Any]]:
        """같은 문맥에서 프레임워크가 찍은 줄에 붙일 값(trace, user, 정해 둔 문맥 필드). formatter가 부른다."""
        trace_id, span_id = self._ids()
        found: list[tuple[str, Any]] = [
            ("trace.id", trace_id),
            ("span.id", span_id),
            ("user.id", self._effective_user()),
        ]
        for field in self._context:
            found.extend(
                entry for entry in field.entries if entry[0] in FRAMEWORK_CONTEXT_KEYS
            )
        if self._tags:
            found.append(("tags", list(self._tags)))
        return found

    def emit(
        self,
        origin: Origin | None,
        level: int,
        message: str,
        fields: tuple[LogField | None, ...],
    ) -> None:
        """줄 하나를 표준 logging 레코드로 넘긴다.

        origin이 없으면 이 패키지 밖의 첫 호출 프레임이 위치다. 로깅 팩토리(LogFlow)는 흐름을 연 함수의 위치를 넘긴다.
        logging logger는 찍은 모듈의 것이라, 모듈별 레벨 설정이 우리 줄에도 그대로 적용된다.
        """
        if origin is None:
            origin = caller()
        logger = logging.getLogger(origin.logger)
        if not logger.isEnabledFor(level):
            return
        # 이 흐름의 첫 줄이면 맡겨 둔 시작 줄을 먼저 남긴다(폴링). 시작 줄 자신이 다시 부르지 않도록 꺼내면서 비운다
        if self._shared is not None:
            start = self._shared.take_start()
            if start is not None:
                start()
        trace_id, span_id = self._ids()
        line = Line(
            origin,
            trace_id,
            span_id,
            self._effective_user(),
            tuple(field for field in fields if field is not None),
            self._context,
            self._tags,
        )
        record = logger.makeRecord(
            logger.name,
            level,
            origin.path,
            origin.line,
            message,
            (),
            None,
            func=origin.function,
            extra={LINE_ATTR: line},
        )
        logger.handle(record)

    def _ids(self) -> tuple[str | None, str | None]:
        if self._trace_id is not None:
            return self._trace_id, self._span_id
        return current_ids()

    def _effective_user(self) -> str | None:
        """이 줄에 남길 사용자. 흐름 중간에 알린 사용자(identify)가 있으면 그 값이다."""
        if self._shared is not None and self._shared.user is not None:
            return self._shared.user
        return self._user_id


# 바인딩이 없을 때(기동, 프레임워크 콜백) 쓰는 객체. trace는 줄을 찍을 때 열려 있는 span이 있으면 그 값을 쓴다
DETACHED = AppLogger(None, None, None, (), None, ())
_CURRENT: ContextVar[AppLogger | None] = ContextVar("app_logger", default=None)
# 모듈 이름별 (log.layer, log.package). 줄마다 모듈 속성을 다시 보지 않게 한 번만 계산한다
_LOCATIONS: dict[str, tuple[str, str]] = {}


def new_logger(user_id: UUID | None = None) -> AppLogger:
    """시작점의 새 객체. 지금 span의 ID와 시작점이 아는 사용자로 만들고, 새 공유 칸을 연다."""
    trace_id, span_id = current_ids()
    return AppLogger(
        trace_id, span_id, str(user_id) if user_id else None, (), _Shared(), ()
    )


def bound_logger() -> AppLogger | None:
    """지금 문맥에 바인딩된 객체. 없으면 None(formatter가 프레임워크 줄에 붙일 값을 찾을 때 쓴다)."""
    return _CURRENT.get()


def bind(logger: AppLogger) -> Token[AppLogger | None]:
    """객체를 지금 문맥에 바인딩한다. 돌려준 token을 reset에 넘기면 이전 객체로 되돌린다."""
    return _CURRENT.set(logger)


def reset(token: Token[AppLogger | None]) -> None:
    _CURRENT.reset(token)


def skip_entry_log(reason: str) -> Callable[[F], F]:
    """레이어 함수의 진입 줄 규칙에서 빼는 표시(Java ``@SkipEntryLog``). 실행할 때는 아무것도 하지 않는다.

    진입 줄 테스트(tests/test_logs.py)가 이 decorator를 보고 그 함수를 건너뛴다. 이유는 로그 문구처럼 영어로 적는다.
    예: 한 요청에서 여러 번 불리는 getter, 짧은 주기 폴링의 실행 함수, 값을 계산하기만 하는 함수.
    """

    def mark(function: F) -> F:
        return function

    return mark


def caller() -> Origin:
    """이 패키지 밖의 첫 호출 프레임의 위치."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and _in_package(frame):
        frame = frame.f_back
    if frame is None:
        return Origin(PACKAGE, "?", "other", PACKAGE, "", 0)
    return origin_of(frame)


def origin_of(frame: FrameType) -> Origin:
    """프레임의 위치. python -m 으로 실행한 모듈(__main__)은 원래 모듈 이름을 쓴다."""
    names = frame.f_globals
    module = names.get("__name__", "?")
    spec = names.get("__spec__")
    if module == "__main__" and spec is not None:
        module = spec.name
    location = _LOCATIONS.get(module)
    if location is None:
        package = names.get("__package__") or module.rpartition(".")[0]
        location = (layer_of(module, names), package)
        _LOCATIONS[module] = location
    return Origin(
        module,
        function_name(frame.f_code),
        location[0],
        location[1],
        frame.f_code.co_filename,
        frame.f_lineno,
    )


def layer_of(module: str, names: dict[str, Any]) -> str:
    """모듈의 log.layer. 모듈 상수 LOG_LAYER가 먼저이고, 없으면 모듈 이름 끝 규칙(LAYERS), 그것도 아니면 other."""
    declared = names.get("LOG_LAYER")
    if isinstance(declared, str):
        return declared
    last = module.rsplit(".", 1)[-1]
    if last in LAYERS:
        return LAYERS[last]
    if last.endswith("_client"):
        return "client"
    return "other"


def function_name(code: CodeType) -> str:
    """log.origin.function. lambda·generator expression처럼 이름 없는 함수는 그것을 감싼 가장 가까운 이름 있는 함수다."""
    if not code.co_name.startswith("<"):
        return code.co_name
    for part in reversed(code.co_qualname.split(".")):
        if not part.startswith("<"):
            return part
    return code.co_name


def _in_package(frame: FrameType) -> bool:
    module = frame.f_globals.get("__name__", "")
    return module == PACKAGE or module.startswith(PACKAGE + ".")
