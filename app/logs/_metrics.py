"""로깅 팩토리가 만드는 앱 지표(ALERTING.md 7절). 서비스 코드는 인자(이름, 풀 객체)만 넘긴다.

지표 엔진도 EDOT Python distro다. distro가 프로세스 시작 때 SDK MeterProvider를 만들고, 내보내기는 OTEL_METRICS_EXPORTER가
정한다(docker-entrypoint.sh의 기본값은 none, dev overlay가 otlp로 켠다. 켜면 trace와 같은 collector를 거쳐 Elasticsearch의
``metrics-*.otel-*``에 쌓인다). 그래서 이 모듈은 SDK를 설정하지 않고 OpenTelemetry API만 쓴다. distro 없이 띄운 곳(로컬 실행)에서는
API가 아무것도 하지 않고, 테스트는 tests/conftest.py가 꽂은 SDK MeterProvider(메모리 reader)를 쓴다.

계측(instrument)은 import할 때 한 번 만든다. 그때 provider가 아직 없으면 API의 proxy meter가 받아 두었다가 provider가 꽂히는
순간 진짜 계측으로 이어 준다. 이름·단위·속성은 OpenTelemetry semantic conventions(DB 연결 풀, GenAI, messaging)를 따르고,
그 밖의 것만 ``loresentry.*``다. 지표를 남기는 일은 로그 줄도 span도 만들지 않는다.

| 지표 | 종류 | 남기는 곳 |
| --- | --- | --- |
| ``db.client.connection.count``(state used·idle), ``.max``, ``.pending_requests`` | 관측 up-down counter | ``watch_connection_pool``로 등록한 풀 |
| ``loresentry.event_loop.lag`` | histogram(s) | ``watch_event_loop``가 띄운 task |
| ``gen_ai.client.operation.duration``(s), ``gen_ai.client.token.usage``({token}) | histogram | ``LogFlow.call``(model을 넘긴 생성형 AI 호출) |
| ``messaging.process.duration``(s), ``loresentry.kafka.consume.delay``(s) | histogram | ``LogFlow.kafka_record`` |

Kafka 발행(``messaging.client.operation.duration``)은 없다. 이 서비스는 Kafka로 보내지 않는다(summarizer는 받기만 한다).
"""

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from app.logs._trace import TRACER

# 연결 풀 지표의 속성 이름(OpenTelemetry DB semantic conventions. Java HikariCP 지표와 같은 이름이다)
POOL_NAME = "db.client.connection.pool.name"
POOL_STATE = "db.client.connection.state"
# 생성형 AI 호출의 operation 이름. 우리 호출은 모두 Responses API의 대화형 생성이라 semantic conventions의 chat이다
GEN_AI_OPERATION = "chat"
# confluent-kafka Message.timestamp()의 종류 중 "값 없음"(confluent_kafka.TIMESTAMP_NOT_AVAILABLE). 이때 값은 버린다.
# API 프로세스가 librdkafka를 읽어 들이지 않도록 confluent_kafka를 import하지 않고 값만 둔다
TIMESTAMP_NOT_AVAILABLE = 0
# event loop 지연을 재는 간격(초). 이만큼 잠들었다 깨어난 시각이 늦은 만큼이 지연이다
LAG_INTERVAL = 1.0

# 버킷 경계(초·토큰 수). SDK 기본 경계는 ms 단위라 초 단위 지표에 맞지 않아, 계측마다 경계를 알려 준다(advisory)
LAG_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
# GenAI semantic conventions가 권하는 경계
TOKEN_BUCKETS = (
    1,
    4,
    16,
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
)
GEN_AI_DURATION_BUCKETS = (
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
)
# messaging semantic conventions가 권하는 경계(HTTP 지표와 같은 0.005~10초)
MESSAGING_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1,
    2.5,
    5,
    7.5,
    10,
)
# 레코드가 만들어진 뒤 처리를 시작하기까지. lag이 쌓이면 분·시간 단위가 된다
CONSUME_DELAY_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300, 900, 3600)


class ConnectionPool(Protocol):
    """지표가 읽는 연결 풀(psycopg_pool의 AsyncConnectionPool)의 모양."""

    @property
    def closed(self) -> bool: ...

    def get_stats(self) -> Mapping[str, int]: ...


# 지표를 읽을 연결 풀. {풀 이름: 풀}. 같은 이름을 다시 등록하면 새 풀로 바뀐다(테스트가 앱을 여러 번 띄운다)
_pools: dict[str, ConnectionPool] = {}


def watch_connection_pool(name: str, pool: ConnectionPool) -> None:
    """연결 풀을 지표(db.client.connection.*)로 읽게 등록한다. 풀을 연 뒤 한 번 부른다. name은 프로세스마다 고정된 이름(서비스 이름)이다.

    지표 reader가 내보낼 때마다(distro의 주기 reader, 다른 스레드) 풀의 ``get_stats()``를 읽는다. ``get_stats()``는 누적 카운터를
    비우지 않는다(비우는 것은 ``pop_stats()``다). 닫힌 풀(앱 종료 뒤, 아직 열지 않은 풀)은 건너뛴다. 닫힌 풀은 연결 수를 0으로
    되돌리지 않아 used 값이 틀리기 때문이다.
    """
    _pools[name] = pool


def watch_event_loop() -> Callable[[], Awaitable[None]]:
    """event loop 지연(loresentry.event_loop.lag)을 재는 task를 지금 loop에 띄우고, 그 task를 멈추는 함수를 돌려준다.

    task는 LAG_INTERVAL(1초)씩 잠들었다 깨어나, 예정보다 늦게 깨어난 만큼(0 이상)을 남긴다. loop를 막는 동기 코드(CPU 작업,
    blocking I/O)가 있으면 그만큼 늦게 깨어난다. 빈 contextvars 문맥에서 띄워, 부른 곳의 span·로그 바인딩이 task에 옮겨 가지
    않는다. 돌려준 함수를 종료할 때 await하면 task를 취소하고 끝날 때까지 기다린다(API lifespan, summarizer main).
    """
    task = asyncio.get_running_loop().create_task(
        _measure_lag(), context=contextvars.Context()
    )

    async def stop() -> None:
        task.cancel()
        # wait는 task의 취소를 밖으로 던지지 않는다. 이 함수를 부른 쪽이 취소되면 그 취소는 그대로 올라간다
        await asyncio.wait([task])

    return stop


def record_generation(
    provider: str,
    request_model: str,
    response_model: str | None,
    tokens: tuple[int, int] | None,
    seconds: float,
    error_type: str | None,
) -> None:
    """생성형 AI 호출 하나의 걸린 시간과 토큰 수. ``LogFlow.call``이 호출 span을 닫기 전에 부른다.

    걸린 시간은 실패한 호출(error.type)도 남긴다. 토큰 수는 응답의 usage를 알린 호출만, 입력·출력을 따로(gen_ai.token.type)
    남긴다. 속성은 operation·provider·요청 모델과, 알면 응답 모델·실패 분류뿐이다. 프롬프트와 모델 응답은 받지도 않는다.
    """
    attributes: dict[str, str] = {
        "gen_ai.operation.name": GEN_AI_OPERATION,
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": request_model,
    }
    if response_model:
        attributes["gen_ai.response.model"] = response_model
    if error_type:
        attributes["error.type"] = error_type
    _OPERATION_DURATION.record(seconds, attributes)
    if tokens is not None:
        input_tokens, output_tokens = tokens
        _TOKEN_USAGE.record(input_tokens, {**attributes, "gen_ai.token.type": "input"})
        _TOKEN_USAGE.record(
            output_tokens, {**attributes, "gen_ai.token.type": "output"}
        )


def record_consume_delay(
    topic: str, group: str, timestamp: tuple[int, int] | None
) -> None:
    """Kafka 레코드 하나가 만들어진 뒤 처리를 시작하기까지 걸린 시간(지금 − 레코드 timestamp, 0 이상).

    timestamp는 confluent-kafka ``Message.timestamp()``의 (종류, epoch ms)다. 값이 없으면(종류 NOT_AVAILABLE, 넘기지 않음)
    남기지 않는다. producer와 이 파드의 시계가 어긋나 음수가 되면 0으로 둔다.
    """
    if timestamp is None:
        return
    kind, epoch_ms = timestamp
    if kind == TIMESTAMP_NOT_AVAILABLE or epoch_ms <= 0:
        return
    delay = max(0.0, time.time() - epoch_ms / 1000)
    _CONSUME_DELAY.record(delay, _kafka(topic, group))


def record_process(
    topic: str, group: str, seconds: float, error_type: str | None
) -> None:
    """Kafka 레코드 하나를 처리하는 데 걸린 시간. ``LogFlow.kafka_record``가 consumer span을 닫기 전에 부른다. 실패면 error.type."""
    attributes = {**_kafka(topic, group), "messaging.operation.name": "process"}
    if error_type:
        attributes["error.type"] = error_type
    _PROCESS_DURATION.record(seconds, attributes)


# ------------------------------------------------------------ 내부


def _kafka(topic: str, group: str) -> dict[str, str]:
    """Kafka 지표의 공통 속성. consumer group이 없으면(빈 값) 뺀다."""
    attributes = {"messaging.system": "kafka", "messaging.destination.name": topic}
    if group:
        attributes["messaging.consumer.group.name"] = group
    return attributes


async def _measure_lag() -> None:
    """watch_event_loop의 task. 취소될 때까지 LAG_INTERVAL마다 늦게 깨어난 만큼을 남긴다. 줄도 span도 남기지 않는다."""
    loop = asyncio.get_running_loop()
    while True:
        interval = LAG_INTERVAL
        start = loop.time()
        await asyncio.sleep(interval)
        _LOOP_LAG.record(max(0.0, loop.time() - start - interval))


def _open_pools() -> list[tuple[str, Mapping[str, int]]]:
    """등록한 풀 중 열린 것의 (이름, get_stats()).

    reader 스레드가 도는 동안 loop 스레드가 풀을 등록해도 되게 목록을 복사해 돈다.
    """
    return [
        (name, pool.get_stats())
        for name, pool in list(_pools.items())
        if not pool.closed
    ]


def _observe_count(options: CallbackOptions) -> list[Observation]:
    # pool_size는 풀이 가진 연결 전부(빌려 간 것, 준비 중인 것 포함), pool_available은 풀에서 쉬는 연결이다
    found: list[Observation] = []
    for name, stats in _open_pools():
        idle = stats.get("pool_available", 0)
        used = max(0, stats.get("pool_size", 0) - idle)
        found.append(Observation(used, {POOL_NAME: name, POOL_STATE: "used"}))
        found.append(Observation(idle, {POOL_NAME: name, POOL_STATE: "idle"}))
    return found


def _observe_max(options: CallbackOptions) -> list[Observation]:
    return [
        Observation(stats.get("pool_max", 0), {POOL_NAME: name})
        for name, stats in _open_pools()
    ]


def _observe_pending(options: CallbackOptions) -> list[Observation]:
    # 연결이 없어 기다리는 요청 수(지금 값. 누적 카운터 requests_queued가 아니다)
    return [
        Observation(stats.get("requests_waiting", 0), {POOL_NAME: name})
        for name, stats in _open_pools()
    ]


# ------------------------------------------------------------ 계측

# 이 패키지의 계측 이름(scope.name). 우리 span을 여는 tracer와 같은 이름이다
_meter = metrics.get_meter(TRACER)

# 연결 풀: semantic conventions의 계측 종류(up-down counter)를 관측형으로 둔다. 내보낼 때 지금 값을 읽는다
_meter.create_observable_up_down_counter(
    "db.client.connection.count",
    callbacks=[_observe_count],
    unit="{connection}",
    description="The number of connections that are currently in state described by the state attribute",
)
_meter.create_observable_up_down_counter(
    "db.client.connection.max",
    callbacks=[_observe_max],
    unit="{connection}",
    description="The maximum number of open connections allowed",
)
_meter.create_observable_up_down_counter(
    "db.client.connection.pending_requests",
    callbacks=[_observe_pending],
    unit="{request}",
    description="The number of current pending requests for an open connection",
)
_LOOP_LAG = _meter.create_histogram(
    "loresentry.event_loop.lag",
    unit="s",
    description="How late the asyncio event loop woke a task that slept for a fixed interval",
    explicit_bucket_boundaries_advisory=LAG_BUCKETS,
)
_TOKEN_USAGE = _meter.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Number of input and output tokens used",
    explicit_bucket_boundaries_advisory=TOKEN_BUCKETS,
)
_OPERATION_DURATION = _meter.create_histogram(
    "gen_ai.client.operation.duration",
    unit="s",
    description="GenAI operation duration",
    explicit_bucket_boundaries_advisory=GEN_AI_DURATION_BUCKETS,
)
_PROCESS_DURATION = _meter.create_histogram(
    "messaging.process.duration",
    unit="s",
    description="Duration of processing operation",
    explicit_bucket_boundaries_advisory=MESSAGING_DURATION_BUCKETS,
)
_CONSUME_DELAY = _meter.create_histogram(
    "loresentry.kafka.consume.delay",
    unit="s",
    description="Time from a record's timestamp until its processing started",
    explicit_bucket_boundaries_advisory=CONSUME_DELAY_BUCKETS,
)
