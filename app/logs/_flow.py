"""흐름 하나의 시작·종료 줄과 span을 여는 로깅 팩토리 ``LogFlow``와, 저장된 trace를 잇는 범위 ``LogScope``(LOG_SCHEMA.md 5절 "만들기", 7절).

서비스 코드는 흐름의 재료(작업 이름, Kafka 레코드, 호출 대상)만 넘기고 ``with``로 연다. 로그 객체를 만들고 바인딩하는 일,
걸린 시간을 재는 일, 결과로 레벨을 정하는 일, 시작·종료 줄을 쓰는 일, 우리 span을 열고 지금 문맥으로 두는 일은 모두 여기서 한다.
``with`` 안의 일이 예외로 끝나도 종료 줄이 남고 span이 닫힌다.

흐름 종류와 여는 span(APM_ARCHITECTURE.md 3절 "우리가 여는 span")
- ``schedule``: 하루 한 번 도는 스케줄 작업 1회. 할 일이 없어도 시작·종료 줄을 남긴다. 예외는 삼키고 실패 줄을 남긴다.
  span ``<스케줄 이름>``(INTERNAL, root)
- ``polling``: 짧은 주기로 도는 폴링 1회. 그 회차에서 처음 줄이 찍힐 때 시작 줄을 먼저 남기고, 줄이 하나도 없으면 시작·종료
  줄을 모두 남기지 않는다. 예외는 삼키고 시작 줄과 실패 줄을 남긴다. span은 schedule과 같고, 빈 회차 span에는 표시를 단다
- ``kafka_record``: Kafka 레코드 하나. ``<event> event received``와 ``<event> event processed``(결과 outcome).
  span ``<topic> process``(CONSUMER, 레코드 헤더의 traceparent가 부모)
- ``call``: 외부 호출 하나(content, OpenAI, KMS). 시작은 호출하는 client 함수의 진입 줄이고, 결과 줄 하나를 남긴다.
  span ``<target> call``(INTERNAL, 계측이 없는 대상은 CLIENT). 계측의 CLIENT span(HTTP, KMS)이 그 자식이다
- ``task``: 흐름 안·밖의 별도 작업(채팅 답변, 설정 검사, 문서 요약, 기동 때 한 번 도는 정리·대기). ``<name> start``와
  ``<name> completed`` 또는 ``<name> failed``. span ``<name>``(INTERNAL, 지금 span의 자식. 흐름 밖에서 시작하면 root)

span은 시작 줄보다 먼저 열고 걸린 시간을 재기 시작한다. 그래서 span 길이가 종료 줄의 event.duration보다 짧지 않다. 종료 줄은 span을
닫기 전에 그 span 안에서 찍으므로, 종료 줄(호출 결과 줄 포함)의 span.id가 그 span이다. 실패로 끝난 흐름(예외, ``fail``, 상대의
5xx)은 span 상태가 ERROR다.

``with``가 돌려주는 흐름 객체로 진행 중에 알게 된 값을 알린다: ``add``(이후 모든 줄과 종료 줄에 붙일 대상 ID), ``result``
(종료 줄의 결과 값), ``outcome``(Kafka 레코드의 결과), ``status``(호출의 응답 상태), ``fail``(예외 없이 끝난 실패의 이유).

시작·종료 줄의 위치(log.logger·log.origin.function·log.layer)는 이 모듈이 아니라 흐름을 연 함수다. 실패 줄에는 예외 메시지를
싣지 않는다. 클래스 이름(error.type)과 메시지 없는 스택만 남긴다(예외 메시지에 SQL·요청 값·본문이 섞일 수 있다).

``with`` 문은 asyncio 코드 안에서도 그대로 쓴다. 바인딩과 span 문맥은 contextvars라 연 task 안에서만 보이고, ``add``는 흐름을 연
task에서 불러야 한다(다른 task의 문맥은 되돌릴 수 없다). asyncio.create_task·to_thread는 만들 때의 문맥을 복사하므로 그 안에서
계측이 여는 span도 이 흐름의 span 아래에 붙는다.
"""

import logging
import sys
import time
from collections.abc import Collection
from contextvars import Token
from enum import Enum
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

from opentelemetry import trace
from opentelemetry.context import Context

from app.logs._fields import LogField, duration
from app.logs._logger import (
    AppLogger,
    Origin,
    bind,
    new_logger,
    origin_of,
    reset,
)
from app.logs._trace import (
    IDLE_POLLING,
    Headers,
    activate,
    current_span,
    detach,
    header_parent,
    header_text,
    mark_failed,
    start_span,
    stored_span,
)

# 스케줄 작업 1회의 시작·종료 줄 문구. 작업은 문구가 아니라 loresentry.schedule.name으로 가린다
SCHEDULE_START = "schedule run start"
SCHEDULE_COMPLETED = "schedule run completed"
SCHEDULE_FAILED = "schedule run failed"
# 상대가 5xx로 답한 호출의 error.type. 응답 상태 자체는 http.response.status_code에 있다
UPSTREAM_SERVER_ERROR = "UPSTREAM_SERVER_ERROR"

# 종료 줄의 레벨(표준 logging 레벨 번호)
INFO, WARN, ERROR = logging.INFO, logging.WARNING, logging.ERROR


class Outcome(Enum):
    """Kafka 레코드 하나의 처리 결과. 종료 줄의 loresentry.result.outcome이다."""

    # 처리했다. 따로 알리지 않으면 이 값이다
    PROCESSED = "PROCESSED"
    # 이미 처리한 이벤트가 다시 와서 건너뛰었다
    DUPLICATE = "DUPLICATE"
    # 처리하지 않는 종류이거나 계약에 맞지 않아 건너뛰었다
    SKIPPED = "SKIPPED"
    # 처리 중 예외가 났다
    FAILED = "FAILED"


class LogFlow:
    """흐름 하나. 아래 정적 함수(schedule, polling, kafka_record, call, task)로 만들고 ``with``로 연다."""

    def __init__(
        self,
        kind: str,
        name: str,
        start: tuple[LogField | None, ...] = (),
        end: tuple[LogField | None, ...] = (),
    ) -> None:
        # 흐름을 연 함수의 위치. 시작·종료 줄의 위치다. LogFlow.xxx(...)를 부른 프레임(이 모듈 밖)이다
        self._origin: Origin = origin_of(sys._getframe(2))
        # schedule·polling·kafka_record·call·task
        self._kind = kind
        # 작업 이름(스케줄 이름, 이벤트 이름, 호출 대상, 작업 이름)
        self._name = name
        # 시작 줄에 붙일 값
        self._start = start
        # 종료 줄에 늘 붙일 값(호출의 url.domain·url.full)
        self._end_always = end
        # 종료 줄에 붙일 결과 값(result)
        self._results: list[LogField] = []
        # Kafka 레코드의 결과. 알리지 않으면 PROCESSED다
        self._outcome: Outcome | None = None
        # 외부 호출의 응답 상태. 모르면 None
        self._status: int | None = None
        # 예외 없이 끝난 실패의 이유(fail). 실패가 아니면 None
        self._failure: str | None = None
        # 이 흐름이 연 바인딩(시작점 객체, add로 더한 객체). 끝날 때 연 반대 순서로 닫는다
        self._tokens: list[Token[AppLogger | None]] = []
        # 이 흐름이 연 span과, 그 span을 지금 문맥으로 둔 token. 끝날 때 종료 줄을 찍은 뒤 떼고 닫는다
        self._span: trace.Span | None = None
        self._span_token: object | None = None
        # 폴링 회차에서 시작 줄을 남겼는가
        self._began = False
        # Kafka 레코드의 문맥 필드와 span 속성을 만들 재료: topic, partition, offset, 헤더, 처리하는 종류, consumer group
        self._record: (
            tuple[str, int, int, dict[str, str], Collection[str], str] | None
        ) = None
        # 계측이 없는 호출 대상의 호스트(call의 server). 있으면 호출 span이 CLIENT이고 server.address가 붙는다
        self._server: str | None = None
        # 계측이 없는 생성형 AI 호출의 모델 이름(call의 model). 있으면 호출 span에 gen_ai 속성이 붙는다
        self._model: str | None = None
        # 걸린 시간을 재는 시작점(흘러간 시간만 재는 시계)
        self._started = 0.0

    # ------------------------------------------------------------ 흐름 종류

    @staticmethod
    def schedule(name: str, *start: LogField | None) -> "LogFlow":
        """하루 한 번 도는 스케줄 작업 1회. 회차마다 새 trace(span ``<name>``)를 시작하고 작업 이름(schedule.name)을 붙여 바인딩한다.

        시작 줄(schedule run start, 시작할 때 아는 값)과 종료 줄(schedule run completed, 결과 값·걸린 시간)을 늘 남긴다.
        예외는 밖으로 내보내지 않고 실패 줄(schedule run failed, error, 메시지 없는 스택)을 남긴다. 다음 회차에 다시 한다.
        """
        return LogFlow("schedule", name, start)

    @staticmethod
    def polling(name: str) -> "LogFlow":
        """짧은 주기로 도는 폴링 1회. schedule과 같지만 시작 줄을 그 회차의 첫 줄 직전에 남긴다.

        회차 span은 회차를 시작할 때 열려 지금 문맥이 된다. 그래서 회차 안의 조회(대기열 claim 쿼리)가 그 자식이 된다.
        할 일이 없는 회차(줄이 하나도 없음)는 시작·종료 줄을 모두 남기지 않고, 회차 span에 빈 회차 표시를 달아 collector가 그
        trace를 버리게 한다. 예외로 끝난 회차는 줄이 없었어도 시작 줄과 실패 줄을 남기고, 예외는 삼킨다.
        """
        return LogFlow("polling", name)

    @staticmethod
    def kafka_record(
        topic: str,
        partition: int,
        offset: int,
        headers: Headers | None,
        known_types: Collection[str],
        event: str,
        group: str,
    ) -> "LogFlow":
        """Kafka 레코드 하나. 헤더의 traceparent를 이은 consumer span을 열고, 그 span으로 로그 객체를 만들어 바인딩한다.

        traceparent는 빈 문맥에서 꺼낸다. 지금 문맥(같은 스레드의 다른 흐름)이 부모로 섞이지 않고, 헤더가 없거나 틀리면 새 trace다.
        span ``<topic> process``(CONSUMER)에는 messaging 속성(system, topic, consumer group, partition, offset, 작업 종류)을 넣는다.
        레코드를 처리하는 모든 줄에 topic·partition·offset과 이벤트의 ID·종류·대상(ce_id·ce_type·ce_subject)이 붙는다.
        ce_type은 이 서비스가 처리하는 종류(known_types)만 그대로, 밖이면 other다. ID·대상은 표준 표기 UUID일 때만 남긴다.
        시작 줄 ``<event> event received``, 종료 줄 ``<event> event processed``(outcome, 걸린 시간). 예외면 종료 줄을
        warn·FAILED·error.type·메시지 없는 스택으로 남기고 span을 ERROR로 표시한 뒤 예외를 그대로 다시 던진다.
        """
        flow = LogFlow("kafka_record", event)
        flow._record = (
            topic,
            partition,
            offset,
            header_text(headers),
            known_types,
            group,
        )
        return flow

    @staticmethod
    def call(
        target: str,
        url: str | None = None,
        server: str | None = None,
        model: str | None = None,
    ) -> "LogFlow":
        """외부 호출 하나. 새 로그 객체를 만들지 않고 지금 흐름의 줄로 남는다.

        호출을 감싸는 span ``<target> call``을 열어 지금 문맥으로 둔다. 계측이 여는 CLIENT span(httpx의 content 호출, botocore의
        KMS 호출)이 그 자식이 된다. 계측이 없는 대상(OpenAI Responses API)은 server에 그 호스트를 넘긴다. 그러면 이 span이
        CLIENT가 되고 server.address가 붙어, 의존 대상의 span이 빠지지 않는다(LOG_SCHEMA.md 7절 "외부 호출"). 그 대상이 생성형 AI면
        model(설정 값인 모델 이름)도 넘긴다. 계측이 넣었을 gen_ai.provider.name(target)·gen_ai.request.model을 이 span에 넣는다.
        프롬프트와 모델 응답은 넣지 않는다(LOG_SCHEMA.md 4절).

        끝나면 그 span 안에서 결과 줄 하나를 남긴다: 성공은 ``<target> call completed``(걸린 시간, 응답 상태, url), 상대의 5xx는
        같은 문구의 warn과 error.type UPSTREAM_SERVER_ERROR, fail이나 예외는 ``<target> call failed``(warn, error.type, 예외면
        메시지 없는 스택). 실패면 span이 ERROR다. 예외는 그대로 다시 던진다.
        """
        end: tuple[LogField | None, ...] = ()
        if url is not None:
            end = (LogField.url_domain(_host(url)), LogField.url_full(url))
        flow = LogFlow("call", target, end=end)
        flow._server = server
        flow._model = model
        return flow

    @staticmethod
    def task(name: str, *start: LogField | None) -> "LogFlow":
        """흐름 안·밖의 별도 작업. 새 로그 객체를 만들지 않고 지금 흐름의 줄로 남는다.

        작업의 span ``<name>``을 열어 지금 문맥으로 둔다. 지금 span의 자식이고, 흐름 밖에서 시작한 작업(기동 때 한 번 도는 정리·대기)은
        root다. 그 안에서 시작 줄 ``<name> start``(시작할 때 아는 값), 종료 줄 ``<name> completed``(결과 값·걸린 시간)를 남긴다.
        fail로 알린 실패는 ``<name> failed``(warn), 예외는 ``<name> failed``(error, 메시지 없는 스택)이고 span이 ERROR다.
        예외는 그대로 다시 던진다.
        """
        return LogFlow("task", name, start)

    # ------------------------------------------------------------ 진행 중에 알리는 값

    def add(self, *fields: LogField | None) -> Self:
        """진행 중에 알게 된 대상 ID를 이 흐름의 이후 모든 줄과 종료 줄에 붙인다(Java with)."""
        self._tokens.append(bind(AppLogger.current().with_fields(*fields)))
        return self

    def result(self, *fields: LogField | None) -> Self:
        """종료 줄에 붙일 결과 값(건수, 토큰 수 등). 여러 번 부르면 모두 붙는다."""
        self._results.extend(field for field in fields if field is not None)
        return self

    def outcome(self, outcome: Outcome) -> Self:
        """Kafka 레코드의 결과(건너뜀 등). 알리지 않으면 PROCESSED다."""
        self._outcome = outcome
        return self

    def status(self, status: int) -> Self:
        """외부 호출의 응답 상태. 5xx면 결과 줄이 warn이 된다."""
        self._status = status
        return self

    def fail(self, error_type: str) -> Self:
        """예외 없이 끝난 실패를 알린다. 종료 줄이 warn이 되고 이 값이 error.type이다.

        fail을 부른 뒤 예외를 던지면(상대의 오류를 우리 예외로 바꿔 던지는 client) 종료 줄은 이 값을 error.type으로 쓰고
        메시지 없는 스택 대신 호출 위치 스택을 붙인다. 이미 분류한 실패라서다.
        """
        self._failure = error_type
        return self

    # ------------------------------------------------------------ with

    def __enter__(self) -> Self:
        # span(과 시작점의 바인딩)을 먼저 열고 시계를 잰다. 반대 순서면 span을 여는 시간이 event.duration에만 들어가
        # span 길이와 어긋난다
        if self._kind in ("schedule", "polling"):
            self._enter_schedule()
        elif self._kind == "kafka_record":
            self._enter_record()
        elif self._kind == "call":
            # 계측이 없는 대상(server를 넘긴 호출)은 이 span이 그 의존 대상의 CLIENT span이다. 생성형 AI 호출(model)이면
            # 계측이 넣었을 gen_ai 속성(대상 이름이 provider)도 넣는다
            client = self._server is not None
            attributes: dict[str, Any] = {}
            if client:
                attributes["server.address"] = self._server
            if self._model is not None:
                attributes["gen_ai.provider.name"] = self._name
                attributes["gen_ai.request.model"] = self._model
            self._open_span(
                f"{self._name} call",
                trace.SpanKind.CLIENT if client else trace.SpanKind.INTERNAL,
                None,
                attributes or None,
            )
        else:
            self._open_span(self._name, trace.SpanKind.INTERNAL, None)
        self._started = time.perf_counter()
        if self._kind == "schedule":
            self._began = True
            self._line(INFO, SCHEDULE_START, self._start)
        elif self._kind == "kafka_record":
            self._line(INFO, f"{self._name} event received", ())
        elif self._kind == "task":
            self._line(INFO, f"{self._name} start", self._start)
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return self._finish(error)
        finally:
            for token in reversed(self._tokens):
                reset(token)
            self._tokens.clear()
            if self._span_token is not None:
                detach(self._span_token)
            if self._span is not None:
                self._span.end()

    # ------------------------------------------------------------ 내부

    def _enter_schedule(self) -> None:
        """회차마다 새 trace(span)를 시작해 지금 문맥으로 두고, 그 span과 작업 이름으로 새 로그 객체를 바인딩한다."""
        # 빈 문맥이 부모라 지금 문맥과 상관없이 새 trace의 root다(LOG_SCHEMA.md 7절 "시작점")
        self._open_span(self._name, trace.SpanKind.INTERNAL, Context())
        logger = new_logger().with_fields(LogField.schedule_name(self._name))
        if self._kind == "polling":
            # 시작 줄은 이 회차의 첫 줄 직전에 남긴다. 줄이 없으면 시작 줄도 없다
            def begin() -> None:
                self._began = True
                logger.emit(self._origin, INFO, SCHEDULE_START, ())

            logger.before_first_line(begin)
        self._tokens.append(bind(logger))

    def _enter_record(self) -> None:
        """헤더의 traceparent를 이은 consumer span을 열고, 레코드 문맥 필드를 붙인 새 로그 객체를 바인딩한다."""
        assert self._record is not None
        topic, partition, offset, headers, known, group = self._record
        self._open_span(
            f"{topic} process",
            trace.SpanKind.CONSUMER,
            header_parent(headers),
            {
                "messaging.system": "kafka",
                "messaging.destination.name": topic,
                "messaging.consumer.group.name": group,
                # semantic conventions에서 partition ID는 문자열이다
                "messaging.destination.partition.id": str(partition),
                "messaging.kafka.offset": offset,
                "messaging.operation.type": "process",
            },
        )
        logger = new_logger().with_fields(
            LogField.kafka_topic(topic),
            LogField.kafka_partition(partition),
            LogField.kafka_offset(offset),
            LogField.cloudevent_id(headers.get("ce_id")),
            LogField.cloudevent_type(headers.get("ce_type"), known),
            LogField.cloudevent_subject(headers.get("ce_subject")),
        )
        self._tokens.append(bind(logger))

    def _open_span(
        self,
        name: str,
        kind: trace.SpanKind,
        parent: Context | None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """span을 열어 지금 문맥으로 둔다. parent가 None이면 지금 span의 자식(없으면 root)이다."""
        self._span = start_span(name, kind, parent, attributes)
        self._span_token = activate(self._span)

    def _finish(self, error: BaseException | None) -> bool:
        """종료 줄을 남기고 실패면 span을 ERROR로 표시한다. 돌려준 값이 True면 예외를 삼킨다(스케줄·폴링)."""
        # 예외의 분류: fail로 알린 이유가 먼저이고, 없으면 예외 클래스 이름이다
        failed = error is not None or self._failure is not None
        error_type = self._failure or (type(error).__name__ if error else None)
        # 메시지 없는 스택은 분류하지 않은 Exception에만 붙인다. 취소(CancelledError) 같은 BaseException은 실패가 아니라 중단이다.
        # 스택을 붙이지 않은 warn·error 줄에는 formatter가 호출 위치 스택을 붙인다
        unexpected = isinstance(error, Exception) and self._failure is None
        stack = error if unexpected else None
        if failed:
            self._failed(error_type, error)
        if self._kind in ("schedule", "polling"):
            if unexpected:
                self._end(ERROR, SCHEDULE_FAILED, error_type, stack)
                return True
            if failed:
                self._end(WARN, SCHEDULE_FAILED, error_type, None)
            elif self._began:
                self._end(INFO, SCHEDULE_COMPLETED, None, None)
            elif self._span is not None:
                # 줄이 하나도 없던 빈 폴링 회차. 로그처럼 trace도 남지 않도록 회차 span에 표시를 달아, collector가 이
                # trace(회차 안의 대기열 조회 DB span 포함)를 통째로 버리게 한다
                self._span.set_attribute(IDLE_POLLING, True)
            return False
        if self._kind == "kafka_record":
            processed = f"{self._name} event processed"
            if error is not None:
                self._outcome = Outcome.FAILED
            if self._outcome is None:
                self._outcome = Outcome.PROCESSED
            # 예외로 끝난 레코드는 원인 줄과 겹쳐도 메시지 없는 스택을 붙인다(LOG_SCHEMA.md 5절 "warn·error 스택")
            self._end(WARN if failed else INFO, processed, error_type, stack)
            return False
        if self._kind == "call":
            if failed:
                self._end(WARN, f"{self._name} call failed", error_type, stack)
            elif self._status is not None and self._status >= 500:
                self._failed(UPSTREAM_SERVER_ERROR, None)
                self._end(
                    WARN, f"{self._name} call completed", UPSTREAM_SERVER_ERROR, None
                )
            else:
                self._end(INFO, f"{self._name} call completed", None, None)
            return False
        # task
        if unexpected:
            self._end(ERROR, f"{self._name} failed", error_type, stack)
        elif failed:
            self._end(WARN, f"{self._name} failed", error_type, None)
        else:
            self._end(INFO, f"{self._name} completed", None, None)
        return False

    def _failed(self, error_type: str | None, error: BaseException | None) -> None:
        if self._span is not None:
            mark_failed(self._span, error_type, error)

    def _end(
        self,
        level: int,
        message: str,
        error_type: str | None,
        error: BaseException | None,
    ) -> None:
        """종료 줄 하나. 결과 값, Kafka 결과, 응답 상태, 실패 이유, 메시지 없는 스택, 걸린 시간을 붙인다.

        지금 바인딩된 객체로 찍으므로 흐름 문맥과 add로 더한 ID가 함께 붙는다. 이 흐름의 span이 아직 지금 문맥이라 줄의 span.id가
        그 span이다.
        """
        fields: list[LogField | None] = [*self._results, *self._end_fields()]
        fields.append(LogField.error_type(error_type))
        fields.append(LogField.safe_stack_trace(error))
        fields.append(duration(time.perf_counter() - self._started))
        self._line(level, message, tuple(fields))

    def _end_fields(self) -> list[LogField | None]:
        fields: list[LogField | None] = list(self._end_always)
        if self._outcome is not None:
            fields.append(LogField.result("outcome", self._outcome))
        fields.append(LogField.status_code(self._status))
        return fields

    def _line(self, level: int, message: str, fields: tuple[Any, ...]) -> None:
        AppLogger.current().emit(self._origin, level, message, fields)


class LogScope:
    """시작·종료 줄 없이 저장된 trace를 잇는 범위. ``with``로 연다(LOG_SCHEMA.md 7절 "저장했다 나중에 처리").

    범위를 열면 저장된 traceparent를 부모로 한 INTERNAL span을 열어 지금 문맥으로 두고, 그 span으로 로그 객체를 만들어 바인딩한다.
    그래서 범위 안의 모든 줄(부른 repository 줄 포함)이 저장된 trace와 문맥 필드로 남고, 범위 안에서 계측이 여는 span(DB 쿼리,
    KMS·OpenAI 호출)과 로깅 팩토리의 span(작업, 호출)이 이 span의 자식이 된다. 닫으면 span을 닫고 이전 바인딩·문맥으로 돌아간다.
    예외로 닫히면 span이 ERROR다.

    - ``stored``: 저장했다 나중에 처리하는 일(summary_queue 행 하나). span ``<작업 이름>``
    - ``outbox_event``: outbox 행 하나의 줄(발행, 실패 기록, 격리). span ``outbox <event type>``

    저장된 값이 없거나 형식이 틀리면(추적을 붙이기 전에 쌓인 행) 그 일만 새 trace로 시작한다. 범위를 연 곳(폴링 회차)의 span과
    trace가 다르면 그 span으로 link를 남겨, APM에서 회차와 작업 사이를 건너갈 수 있게 한다.
    """

    def __init__(
        self,
        traceparent: str | None,
        name: str,
        context: tuple[LogField | None, ...],
    ):
        # 저장해 둔 traceparent의 span. 형식이 틀리거나 없으면 유효하지 않은 문맥이고, 그때는 새 trace로 시작한다
        self._stored = stored_span(traceparent)
        # 이 범위가 여는 span 이름
        self._name = name
        self._context = context
        self._token: Token[AppLogger | None] | None = None
        self._span: trace.Span | None = None
        self._span_token: object | None = None

    @staticmethod
    def stored(
        traceparent: str | None, name: str, *context: LogField | None
    ) -> "LogScope":
        """저장해 둔 traceparent를 부모로 span ``<name>``을 열고, 그 span으로 바인딩한다.

        범위 안에서 만드는 span(DB 쿼리, 호출)이 그 span의 자식이라 저장한 trace에 붙는다. traceparent가 없는 행(추적을 붙이기 전에
        쌓인 행)은 그 행만 새 trace로 남는다.
        """
        return LogScope(traceparent, name, context)

    @staticmethod
    def outbox_event(
        traceparent: str | None,
        schedule: str,
        event_id: UUID | str | None,
        event_type: str | None,
        subject: UUID | str | None,
        *ids: LogField | None,
    ) -> "LogScope":
        """outbox 행 하나. 행의 traceparent를 부모로 span을 열고, relay 작업 이름, 이벤트의 ID·종류·대상과 도메인 ID(event key)를 붙인다."""
        return LogScope(
            traceparent,
            f"outbox {event_type}",
            (
                LogField.schedule_name(schedule),
                LogField.cloudevent_id(event_id),
                LogField.cloudevent_type(event_type),
                LogField.cloudevent_subject(subject),
                *ids,
            ),
        )

    def __enter__(self) -> Self:
        # 범위를 연 곳의 span(폴링 회차). 저장된 trace와 다르면 link로 남긴다
        opener = current_span()
        links: list[trace.Link] = []
        if opener.is_valid and opener.trace_id != self._stored.trace_id:
            links.append(trace.Link(opener))
        # 저장된 span을 부모로 둔 빈 문맥. 유효하지 않으면 빈 문맥이라 새 trace의 root다. 지금 문맥(회차)은 부모로 섞이지 않는다
        parent = Context()
        if self._stored.is_valid:
            parent = trace.set_span_in_context(
                trace.NonRecordingSpan(self._stored), parent
            )
        self._span = start_span(
            self._name, trace.SpanKind.INTERNAL, parent, links=links
        )
        self._span_token = activate(self._span)
        self._token = bind(new_logger().with_fields(*self._context))
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._token is not None:
            reset(self._token)
        if self._span_token is not None:
            detach(self._span_token)
        if self._span is not None:
            if error is not None:
                mark_failed(self._span, type(error).__name__, error)
            self._span.end()


def _host(url: str) -> str | None:
    return urlsplit(url).hostname
