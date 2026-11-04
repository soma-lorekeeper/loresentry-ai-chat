"""모든 로그 줄(우리 AppLogger, uvicorn·httpx·psycopg 같은 프레임워크)을 ECS JSON 한 줄로 만든다(LOG_SCHEMA.md 5절 "형식").

``configure_logging``이 root logger에 이 formatter를 단 stdout handler 하나를 둔다. 프레임워크 logger는 따로 handler를
두지 않고 root로 올려 보내 같은 formatter를 거친다.

필드
- 늘: ``@timestamp``(UTC, 마이크로초, Z), ``log.level``(INFO·WARN·ERROR, DEBUG), ``log.logger``, ``message``,
  ``service.name``, ``service.version``(환경 변수 APP_VERSION, 이미지 태그), ``process.thread.name``, ``ecs.version``
- trace·사용자·문맥: 우리 줄은 그 줄을 찍은 객체의 값, 프레임워크 줄은 같은 문맥에 바인딩된 객체의 값(Java MDC 문맥 필드와
  같다). 바인딩이 없으면 지금 열려 있는 span의 trace.id·span.id
- 우리 줄: ``log.origin.function``, ``log.layer``, ``log.package``, 그 줄의 필드, 객체의 문맥 필드(줄의 값이 이긴다), ``tags``
- 프레임워크 줄의 예외: ``error.type``(클래스 이름)과 메시지 없는 ``error.stack_trace``. 예외 메시지는 남기지 않는다

점 이름은 중첩 객체로 펼치고 먼저 들어간 값이 이긴다(Java EcsLogFormatter.put과 같다). JSON을 만들다 실패해도 줄을 버리지 않고
최소 필드만 담은 줄을 쓴다.
"""

import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from app.logs._fields import LogField
from app.logs._logger import LINE_ATTR, Line, bound_logger
from app.logs._trace import current_ids

# Logstash는 "ecs":{...}가 든 줄만 필드로 펼친다
ECS_VERSION = "8.11"
# 표준 logging 레벨 이름 → log.level. LOG_SCHEMA.md 3절의 Python 표현이다
LEVELS = {"WARNING": "WARN", "CRITICAL": "ERROR"}
# 요청 URL 전체를 INFO로 찍는 라이브러리(OpenAI SDK와 TestClient는 httpx2). 쿼리에 검색어가 들어갈 수 있고 호출 결과 줄은 LogFlow.call이 남기므로 WARNING부터만 둔다
QUIET = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "openai",
    "botocore",
    "boto3",
    "urllib3",
)
# configure_logging이 단 handler 표시. 두 번 불러도 한 번만 단다
HANDLER_NAME = "loresentry-ecs"
# Kafka client(librdkafka)가 자기 줄을 넘기는 logger 이름. graph-rag도 같은 이름을 쓴다
KAFKA_CLIENT_LOGGER = "librdkafka"


class EcsFormatter(logging.Formatter):
    """ECS JSON 한 줄을 만드는 formatter."""

    def __init__(self, service_name: str, service_version: str | None) -> None:
        super().__init__()
        self.service_name = service_name
        self.service_version = service_version

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(
                self.document(record), ensure_ascii=False, separators=(",", ":")
            )
        except Exception as failure:
            return self.fallback(record, failure)

    def document(self, record: logging.LogRecord) -> dict[str, Any]:
        root: dict[str, Any] = {"@timestamp": timestamp(record.created)}
        put(root, "log.level", LEVELS.get(record.levelname, record.levelname))
        put(root, "log.logger", record.name)
        put(root, "process.thread.name", record.threadName)
        put(root, "service.name", self.service_name)
        put(root, "service.version", self.service_version)
        put(root, "message", record.getMessage())
        line: Line | None = getattr(record, LINE_ATTR, None)
        if line is not None:
            self.ours(root, line)
        else:
            self.framework(root, record)
        put(root, "ecs.version", ECS_VERSION)
        return root

    def ours(self, root: dict[str, Any], line: Line) -> None:
        """AppLogger로 찍은 줄: trace·사용자, 위치, 그 줄의 필드, 문맥 필드, tags 순서. 같은 이름은 먼저 넣은 값이 이긴다."""
        put(root, "trace.id", line.trace_id)
        put(root, "span.id", line.span_id)
        put(root, "user.id", line.user_id)
        put(root, "log.origin.function", line.origin.function)
        put(root, "log.layer", line.origin.layer)
        put(root, "log.package", line.origin.package)
        for field in (*line.fields, *line.context):
            put_field(root, field)
        if line.tags:
            put(root, "tags", list(line.tags))

    def framework(self, root: dict[str, Any], record: logging.LogRecord) -> None:
        """프레임워크 줄: 예외는 클래스 이름과 메시지 없는 스택만, trace·사용자·문맥은 같은 문맥의 바인딩에서 얻는다."""
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            put(root, "error.type", type(error).__name__)
            put_field(root, LogField.safe_stack_trace(error))
        logger = bound_logger()
        if logger is not None:
            for name, value in logger.framework_fields():
                put(root, name, value)
            return
        trace_id, span_id = current_ids()
        put(root, "trace.id", trace_id)
        put(root, "span.id", span_id)

    def fallback(self, record: logging.LogRecord, failure: Exception) -> str:
        """정상 경로가 실패했을 때 쓰는 최소 줄. 값은 문자열 연결이 아니라 json으로 이스케이프한다."""
        root: dict[str, Any] = {"@timestamp": timestamp(record.created)}
        put(root, "log.level", LEVELS.get(record.levelname, record.levelname))
        put(root, "log.logger", record.name)
        put(root, "service.name", self.service_name)
        put(root, "message", f"log formatting failed: {type(failure).__name__}")
        put(root, "ecs.version", ECS_VERSION)
        return json.dumps(root, ensure_ascii=False, separators=(",", ":"))


class StdoutHandler(logging.StreamHandler):
    """줄마다 그때의 sys.stdout에 쓰는 handler. 테스트(capsys)가 바꾼 stdout에도 그대로 쓴다."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, value: Any) -> None:
        # StreamHandler.__init__이 넣으려는 값은 쓰지 않는다. 늘 그때의 sys.stdout이다
        pass


def configure_logging(service_name: str) -> None:
    """root logger에 ECS JSON handler를 단다. 여러 번 불러도 한 번만 한다.

    - root는 INFO. uvicorn·uvicorn.error는 자기 handler 없이 root로 올려 보내고, uvicorn 접근 로그는 끈다
      (요청 줄은 RequestLogMiddleware가 남긴다. Dockerfile도 --no-access-log로 띄운다)
    - URL 전체를 INFO로 찍는 라이브러리(httpx 등)는 WARNING부터만 둔다
    - 처리되지 않은 예외(sys.excepthook, threading.excepthook)도 logging으로 보내 평문 traceback이 stderr에 남지 않게 한다

    API는 모듈을 import할 때(uvicorn이 Started server process를 찍기 전), 다른 프로세스는 진입점의 맨 처음에 부른다.
    """
    root = logging.getLogger()
    if any(handler.get_name() == HANDLER_NAME for handler in root.handlers):
        return
    handler = StdoutHandler()
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(EcsFormatter(service_name, os.getenv("APP_VERSION")))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.disabled = True
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    sys.excepthook = _uncaught
    threading.excepthook = _uncaught_in_thread


def kafka_client_logger() -> logging.Logger:
    """librdkafka 자체 줄(브로커 연결, 리밸런스 등)을 받을 logger. Kafka client 설정의 "logger"에 넘긴다.

    넘기지 않으면 librdkafka가 평문으로 stderr에 쓴다(ES에서 log.level이 unknown이 된다). 넘기면 confluent-kafka가
    poll()·consume()을 부를 때 이 logger로 넘기고, root의 ECS JSON handler를 거친다. Kafka client 스레드의 줄이라 흐름
    밖이고 문맥 필드가 붙지 않는다(log.logger: librdkafka로 찾는다).
    """
    return logging.getLogger(KAFKA_CLIENT_LOGGER)


def timestamp(created: float) -> str:
    """레코드 시각을 UTC ISO-8601(마이크로초, Z)로."""
    return (
        datetime.fromtimestamp(created, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def put(root: dict[str, Any], name: str, value: Any) -> None:
    """점 이름("log.origin.function")을 중첩 객체로 넣는다. 값이 None이면 넣지 않는다.

    먼저 들어간 값이 이긴다: 같은 이름이 이미 있거나 중간 이름에 객체가 아닌 값이 있으면(이름 충돌) 이 값은 버린다.
    """
    if value is None:
        return
    parts = name.split(".")
    node = root
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        elif not isinstance(child, dict):
            return
        node = child
    node.setdefault(parts[-1], value)


def put_field(root: dict[str, Any], field: LogField | None) -> None:
    if field is not None:
        for name, value in field.entries:
            put(root, name, value)


def _uncaught(
    kind: type[BaseException],
    error: BaseException,
    traceback: TracebackType | None,
) -> None:
    """처리되지 않은 예외. formatter가 클래스 이름과 메시지 없는 스택으로 남긴다."""
    logging.getLogger("app").error(
        "uncaught exception", exc_info=(kind, error, traceback)
    )


def _uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    """스레드에서 처리되지 않은 예외. 기본 동작과 같이 SystemExit은 남기지 않는다."""
    if args.exc_value is None or args.exc_type is SystemExit:
        return
    logging.getLogger("app").error(
        "uncaught exception in thread",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
