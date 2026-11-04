"""서비스의 로그·trace 패키지(LOG_SCHEMA.md 5절 "앱 로그 규약", 7절 "trace").

서비스 코드는 로깅 로직을 모른다. 인자만 넘기고, 로그 객체 만들기·바인딩, 걸린 시간 재기, 결과로 레벨 정하기, 시작·종료 줄
쓰기는 모두 이 패키지가 한다. 서비스 코드가 가져올 수 있는 이름은 아래가 전부다(ruff가 logging·print와 내부 모듈
``app.logs._*``를 이 패키지 밖에서 막는다).

| 이름 | 쓰는 곳 |
| --- | --- |
| ``AppLogger.current().info/warn/error(message, *fields)`` | 진입·진행·결과 줄 |
| ``AppLogger.current().identify(user_id)`` | 흐름 중간에 알게 된 사용자 |
| ``AppLogger.current().add_to_outcome(*fields)`` | 예외 처리기가 결과 줄에 이유를 맡긴다 |
| ``AppLogger.current().trace_id``, ``traceparent()`` | 에러 응답 본문의 trace_id, 나중에 처리할 행에 저장할 전파 값 |
| ``LogFlow.schedule/polling/kafka_record/call/task`` | 흐름 하나의 시작·종료 줄(``with``로 연다) |
| ``LogScope.stored/outbox_event`` | 시작·종료 줄 없이 바인딩만 하는 범위 |
| ``LogField.*`` | 필드. 정해 둔 이름만 있다 |
| ``Outcome`` | Kafka 레코드의 결과 |
| ``skip_entry_log(reason)`` | 진입 줄 규칙에서 빼는 표시 |
| ``configure_logging``, ``setup_tracing``, ``RequestLogMiddleware`` | 프로세스 진입점과 앱 조립 |
| ``kafka_client_logger()`` | Kafka client 설정의 "logger"(librdkafka 줄도 ECS JSON으로) |

``python -m app.logs.pipe <logger> <service>``는 다른 프로그램(flyway)의 출력 줄을 ECS JSON으로 바꾼다.
"""

from app.logs._fields import LogField
from app.logs._flow import LogFlow, LogScope, Outcome
from app.logs._format import configure_logging, kafka_client_logger
from app.logs._logger import AppLogger, skip_entry_log
from app.logs._request import RequestLogMiddleware
from app.logs._trace import setup_tracing, traceparent

__all__ = [
    "AppLogger",
    "LogField",
    "LogFlow",
    "LogScope",
    "Outcome",
    "RequestLogMiddleware",
    "configure_logging",
    "kafka_client_logger",
    "setup_tracing",
    "skip_entry_log",
    "traceparent",
]
