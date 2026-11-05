"""서비스의 로그·trace·지표 패키지(LOG_SCHEMA.md 5절 "앱 로그 규약", 7절 "trace", ALERTING.md 7절).

서비스 코드는 로깅·추적 로직을 모른다. 인자(값, 헤더)만 넘기고, 로그 객체 만들기·바인딩, 걸린 시간 재기, 결과로 레벨 정하기,
시작·종료 줄 쓰기, 우리 span 열기와 지금 문맥 바꾸기, 저장된 trace 잇기, 앱 지표 남기기는 모두 이 패키지가 한다. 추적·지표
엔진(SDK, exporter, 라이브러리 계측)은 EDOT Python distro가 프로세스 시작 때 설정한다(docker-entrypoint.sh의
``opentelemetry-instrument``). 서비스 코드가 가져올 수 있는 이름은 아래가 전부다(ruff가 logging·print·opentelemetry와 내부 모듈
``app.logs._*``를 이 패키지 밖에서 막는다).

| 이름 | 쓰는 곳 |
| --- | --- |
| ``AppLogger.current().info/warn/error(message, *fields)`` | 진입·진행·결과 줄 |
| ``AppLogger.current().identify(user_id)`` | 흐름 중간에 알게 된 사용자 |
| ``AppLogger.current().add_to_outcome(*fields)`` | 예외 처리기가 결과 줄에 이유를 맡긴다 |
| ``AppLogger.current().trace_id``, ``traceparent()`` | 에러 응답 본문의 trace_id, 나중에 처리할 행에 저장할 전파 값 |
| ``LogFlow.schedule/polling/kafka_record/call/task`` | 흐름 하나의 시작·종료 줄과 span(``with``로 연다) |
| ``LogScope.stored/outbox_event`` | 시작·종료 줄 없이 저장된 trace를 잇는 범위(그 trace를 부모로 span을 연다) |
| ``LogField.*`` | 필드. 정해 둔 이름만 있다 |
| ``Outcome`` | Kafka 레코드의 결과 |
| ``skip_entry_log(reason)`` | 진입 줄 규칙에서 빼는 표시 |
| ``configure_logging``, ``setup_tracing(app)``, ``RequestLogMiddleware`` | 프로세스 진입점과 앱 조립(FastAPI 계측은 앱 객체만 넘긴다) |
| ``kafka_client_logger()`` | Kafka client 설정의 "logger"(librdkafka 줄도 ECS JSON으로) |
| ``watch_connection_pool(name, pool)``, ``watch_event_loop()`` | 프로세스 진입점의 지표(연결 풀, event loop 지연) |

``python -m app.logs.pipe <logger> <service>``는 다른 프로그램(flyway)의 출력 줄을 ECS JSON으로 바꾼다.
"""

from app.logs._fields import LogField
from app.logs._flow import LogFlow, LogScope, Outcome
from app.logs._format import configure_logging, kafka_client_logger
from app.logs._logger import AppLogger, skip_entry_log
from app.logs._metrics import watch_connection_pool, watch_event_loop
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
    "watch_connection_pool",
    "watch_event_loop",
]
