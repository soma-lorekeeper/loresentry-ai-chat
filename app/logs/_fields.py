"""로그 한 줄에 붙일 수 있는 필드(LOG_SCHEMA.md 5절 "필드").

이름은 ECS 필드와 ``loresentry.*`` 필드만 쓴다. Java 서비스의 ``LogField``와 같은 이름·같은 규칙이다.

서비스 코드는 ``LogField``의 정적 팩토리로만 필드를 만든다. 임의의 이름이나 값(원고, 제목, 채팅 본문, 검색어,
프롬프트, 모델 출력)을 필드로 넣을 자리가 없게 하려는 것이다. 새 필드가 필요하면 LOG_SCHEMA.md 표에 먼저 더한 뒤
여기에 팩토리를 만든다.

필드 하나가 이름·값 쌍을 여러 개 가질 수 있다. 예를 들어 문자열 입력값은 ``param.title: "[redacted]"``와
``param.title_length: 7`` 두 쌍이 된다. 값이 없으면(None) 팩토리가 None을 돌려주고, 로그 줄은 None 필드를 건너뛴다.

입력값(``param``)·결과(``result``)는 LOG_SCHEMA.md 5절 "입력값과 결과" 규칙대로 값을 바꾼다.

- 숫자·불리언·Enum·UUID: 그대로(Enum은 이름, UUID는 문자열)
- 문자열: 늘 ``[redacted]``와 ``<이름>_length``. 내용이 남지 않는다
- 허용 목록이 있는 코드 문자열(``code``·``result_code``): 목록 안이면 그대로, 밖이면 ``other``
- list·tuple·set·frozenset·dict: ``<이름>_count``
- bytes: ``<이름>_bytes``
- 그 밖의 객체(DTO 등): 받지 않는다(TypeError). 필요한 숫자·코드만 골라 넘긴다

이름은 파라미터 이름을 snake_case로 쓰고, 같은 이름은 늘 같은 타입으로 쓴다(ES는 처음 들어온 값으로 타입을 정한다).
"""

from collections.abc import Collection, Mapping
from datetime import timedelta
from enum import Enum
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

# 가린 문자열 자리에 남기는 값. Logstash mask.rb의 표시와 같다
REDACTED = "[redacted]"
# 허용 목록 밖의 코드 값을 대신하는 값
OTHER = "other"
# 메시지 없는 스택에 남기는 원인 수와 원인마다의 프레임 수 상한
MAX_CAUSES = 5
MAX_FRAMES = 20

PARAM = "loresentry.param."
RESULT = "loresentry.result."


class LogField:
    """이름·값 쌍 묶음. 생성자는 이 패키지 안에서만 쓰고, 서비스 코드는 아래 정적 팩토리만 부른다."""

    __slots__ = ("entries",)

    def __init__(self, entries: tuple[tuple[str, Any], ...]) -> None:
        # (점 이름, 값) 쌍. formatter가 점 이름을 중첩 객체로 펼친다
        self.entries = entries

    def __repr__(self) -> str:
        return f"LogField{self.entries!r}"

    # ------------------------------------------------------------ 도메인 ID (loresentry.*, keyword)
    # 이 줄의 대상, 대상이 속한 범위, 새로 만든 대상만 여기에 넣는다.
    # 참조만 하는 다른 ID는 param("<역할>_<종류>_id", id)로 넣는다(LOG_SCHEMA.md 5절 "ID를 나누는 기준").

    @staticmethod
    def project_id(value: UUID | str | None) -> "LogField | None":
        return _uuid("loresentry.project_id", value)

    @staticmethod
    def document_id(value: UUID | str | None) -> "LogField | None":
        return _uuid("loresentry.document_id", value)

    @staticmethod
    def file_id(value: UUID | str | None) -> "LogField | None":
        return _uuid("loresentry.file_id", value)

    @staticmethod
    def chat_session_id(value: UUID | str | None) -> "LogField | None":
        """채팅 대화 세션. 인증 세션과 헷갈리지 않게 chat 아래에 둔다."""
        return _uuid("loresentry.chat.session_id", value)

    @staticmethod
    def chat_turn_id(value: UUID | str | None) -> "LogField | None":
        """질문 하나에 답하는 한 번의 시도(chat_turns 행)."""
        return _uuid("loresentry.chat.turn_id", value)

    @staticmethod
    def setting_check_run_id(value: UUID | str | None) -> "LogField | None":
        """설정 오류 검사 1회(setting_check_runs 행)."""
        return _uuid("loresentry.setting_check.run_id", value)

    @staticmethod
    def setting_check_finding_id(value: UUID | str | None) -> "LogField | None":
        """설정 오류 검사가 찾은 항목 하나(setting_check_findings 행)."""
        return _uuid("loresentry.setting_check.finding_id", value)

    @staticmethod
    def refresh_request_id(value: UUID | str | None) -> "LogField | None":
        """관계도 최신화 요청. content의 run도 같은 값이라 이 필드 하나로 따라간다."""
        return _uuid("loresentry.refresh.request_id", value)

    # ------------------------------------------------------------ 입력값 (loresentry.param.*)

    @staticmethod
    def param(name: str, value: Any) -> "LogField | None":
        """메서드에 들어온 값과 처리 중의 중간값. 값의 종류에 따라 모양을 바꾼다(모듈 설명 참고)."""
        return _converted(PARAM, name, value)

    @staticmethod
    def code(
        name: str, value: str | None, allowed: Collection[str]
    ) -> "LogField | None":
        """정해진 낱말 중 하나인 입력 코드(intent, verdict 등). 허용 목록 밖이면 other라 자유 텍스트가 남지 않는다."""
        return _of(PARAM + name, _allowed(value, allowed))

    # ------------------------------------------------------------ 결과 (loresentry.result.*)

    @staticmethod
    def result(name: str, value: Any) -> "LogField | None":
        """처리 결과(건수, 토큰 수 등). 값은 param과 같은 규칙으로 바꾼다."""
        return _converted(RESULT, name, value)

    @staticmethod
    def result_code(
        name: str, value: str | None, allowed: Collection[str]
    ) -> "LogField | None":
        """결과 코드 문자열(상태 이름 등). 허용 목록 밖이면 other."""
        return _of(RESULT + name, _allowed(value, allowed))

    # ------------------------------------------------------------ 에러 (ECS)

    @staticmethod
    def error_type(value: str | None) -> "LogField | None":
        """실패 종류. 우리 오류 코드(CHAT_MODEL_UNAVAILABLE 등)나 예외 클래스 이름."""
        return _of("error.type", value)

    @staticmethod
    def error_code(value: str | None) -> "LogField | None":
        """상대(KMS, OpenAI 등)가 준 오류 코드."""
        return _of("error.code", value)

    @staticmethod
    def safe_stack_trace(error: BaseException | None) -> "LogField | None":
        """예외 메시지를 뺀 스택(error.stack_trace).

        원인 사슬(``__cause__``, 없으면 숨기지 않은 ``__context__``)을 따라 최대 5개 예외에 대해, 예외 클래스 이름 한 줄과
        예외가 난 곳부터 거꾸로 최대 20개 프레임(``\\tat 모듈.함수(파일:줄)``)을 적는다. 예외 메시지에는 SQL·요청 값·
        본문이 섞일 수 있어 남기지 않는다.
        """
        if error is None:
            return None
        lines: list[str] = []
        seen: set[int] = set()
        current: BaseException | None = error
        while (
            current is not None and len(seen) < MAX_CAUSES and id(current) not in seen
        ):
            seen.add(id(current))
            lines.append(qualified_name(type(current)))
            lines.extend(_frames(current.__traceback__))
            current = current.__cause__ or (
                None if current.__suppress_context__ else current.__context__
            )
        return _of("error.stack_trace", "\n".join(lines))

    # ------------------------------------------------------------ Kafka·CloudEvents (loresentry.*)
    # ECS 8.11에 메시징 필드 묶음이 없어 loresentry 아래에 둔다.

    @staticmethod
    def kafka_topic(value: str | None) -> "LogField | None":
        return _of("loresentry.kafka.topic", value)

    @staticmethod
    def kafka_partition(value: int | None) -> "LogField | None":
        return _of("loresentry.kafka.partition", value)

    @staticmethod
    def kafka_offset(value: int | None) -> "LogField | None":
        return _of("loresentry.kafka.offset", value)

    @staticmethod
    def cloudevent_id(value: UUID | str | None) -> "LogField | None":
        """이벤트 ID(ce_id). 표준 표기 UUID일 때만 남긴다."""
        return _uuid("loresentry.cloudevent.id", value)

    @staticmethod
    def cloudevent_type(
        value: str | None, known: Collection[str] | None = None
    ) -> "LogField | None":
        """이벤트 종류(ce_type). 받은 메시지면 이 서비스가 처리하는 종류(known)만 그대로, 밖이면 other."""
        if known is not None:
            value = _allowed(value, known)
        return _of("loresentry.cloudevent.type", value)

    @staticmethod
    def cloudevent_subject(value: UUID | str | None) -> "LogField | None":
        """이벤트 대상(ce_subject). 표준 표기 UUID일 때만 남긴다."""
        return _uuid("loresentry.cloudevent.subject", value)

    # ------------------------------------------------------------ 스케줄 작업 (loresentry.*)

    @staticmethod
    def schedule_name(value: str | None) -> "LogField | None":
        """스케줄·폴링 작업 이름(summary-tick 등). 코드에 정해 둔 이름만 넘긴다."""
        return _of("loresentry.schedule.name", value)

    # ------------------------------------------------------------ HTTP (ECS·OpenTelemetry)

    @staticmethod
    def http_method(value: str | None) -> "LogField | None":
        return _of("http.request.method", value)

    @staticmethod
    def url_path(value: str | None) -> "LogField | None":
        """이 서비스가 받은 요청의 실제 경로(쿼리 제외). 쿼리에는 검색어가 들어갈 수 있다."""
        return _of("url.path", value)

    @staticmethod
    def route(value: str | None) -> "LogField | None":
        """경로 템플릿(/chat/sessions/{session_id}). OpenTelemetry http.route와 같은 뜻이다."""
        return _of("http.route", value)

    @staticmethod
    def status_code(value: int | None) -> "LogField | None":
        return _of("http.response.status_code", value)

    @staticmethod
    def url_domain(value: str | None) -> "LogField | None":
        """다른 서비스를 부를 때의 대상 호스트."""
        return _of("url.domain", value)

    @staticmethod
    def url_full(value: str | None) -> "LogField | None":
        """다른 서비스를 부를 때의 대상 주소. scheme://host[:port]/path만 남긴다(사용자 정보, 쿼리, fragment 제외)."""
        if value is None:
            return None
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return _of("url.full", f"{parts.scheme}://{host}{port}{parts.path}")


def duration(value: float | timedelta | None) -> LogField | None:
    """걸린 시간(event.duration, 나노초).

    시간은 로깅 팩토리(RequestLogMiddleware, LogFlow)만 잰다. 서비스 코드가 시간을 재서 넘기지 않도록 LogField가 아니라
    이 내부 모듈의 함수로 둔다(app.logs._fields는 ruff로 서비스 코드에서 막는다).
    """
    if value is None:
        return None
    seconds = value.total_seconds() if isinstance(value, timedelta) else value
    return _of("event.duration", int(seconds * 1_000_000_000))


def qualified_name(cls: type) -> str:
    """예외 클래스의 전체 이름(모듈.클래스). 내장 예외는 모듈 없이 이름만."""
    module = cls.__module__
    if module in ("builtins", "__main__"):
        return cls.__qualname__
    return f"{module}.{cls.__qualname__}"


def _of(name: str, value: Any) -> LogField | None:
    return None if value is None else LogField(((name, value),))


def _allowed(value: str | None, allowed: Collection[str]) -> str | None:
    if value is None:
        return None
    return value if value in allowed else OTHER


def _uuid(name: str, value: UUID | str | None) -> LogField | None:
    """UUID 필드. 문자열은 표준 표기(36자) UUID일 때만 받는다. UUID()는 짧은 표기도 받아 주므로 길이부터 본다."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return _of(name, str(value))
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return _of(name, str(UUID(value)))
    except ValueError:
        return None


def _converted(prefix: str, name: str, value: Any) -> LogField | None:
    """param·result 값을 LOG_SCHEMA.md 5절 "입력값과 결과" 규칙대로 바꾼다."""
    if value is None:
        return None
    # bool은 int의 하위 클래스라 숫자보다 먼저 본다
    if isinstance(value, bool | int | float):
        return _of(prefix + name, value)
    if isinstance(value, Enum):
        return _of(prefix + name, value.name)
    if isinstance(value, UUID):
        return _of(prefix + name, str(value))
    if isinstance(value, str):
        # 문자열은 내용 대신 가린 표시와 길이만 남긴다
        return LogField(
            ((prefix + name, REDACTED), (f"{prefix}{name}_length", len(value)))
        )
    if isinstance(value, bytes | bytearray | memoryview):
        return _of(f"{prefix}{name}_bytes", len(value))
    if isinstance(value, list | tuple | set | frozenset | Mapping):
        return _of(f"{prefix}{name}_count", len(value))
    raise TypeError(f"log field {name} does not take {type(value).__name__}")


def _frames(traceback: TracebackType | None) -> list[str]:
    """예외가 난 곳부터 거꾸로 최대 MAX_FRAMES개의 프레임 줄. 모듈은 프레임의 __name__, 함수는 클래스까지 붙인 이름이다."""
    frames: list[str] = []
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "?")
        file = frame.f_code.co_filename.rsplit("/", 1)[-1]
        frames.append(
            f"\tat {module}.{frame.f_code.co_qualname}({file}:{traceback.tb_lineno})"
        )
        traceback = traceback.tb_next
    frames.reverse()
    return frames[:MAX_FRAMES]
