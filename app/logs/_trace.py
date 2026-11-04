"""OpenTelemetry trace 설정과, 지금 span을 로그·저장소가 쓰는 모양으로 꺼내는 함수(LOG_SCHEMA.md 7절).

- ``setup_tracing``: SDK TracerProvider를 한 번 설정한다. sampler는 ParentBased(AlwaysOn)이다. 환경 변수
  ``OTEL_EXPORTER_OTLP_ENDPOINT``(EDOT Collector 주소)가 있을 때만 span을 OTLP/HTTP로 보낸다. 보내지 않아도 trace ID는
  만들어지고 HTTP·Kafka로 전파된다.
- 전파 형식은 W3C ``traceparent`` 하나다. ``tracestate``·``baggage``는 쓰지 않으므로 전역 propagator를 tracecontext만으로 둔다.
- 나머지 함수는 로깅 팩토리(``_logger``, ``_flow``, ``_request``)가 쓰는 내부 함수다.
"""

import os
import re
from collections.abc import Mapping, Sequence

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# W3C traceparent version 00: 00-<trace-id 32 hex>-<parent span-id 16 hex>-<flags 2 hex>
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
# 이 패키지가 span을 만들 때 쓰는 tracer 이름
TRACER = "app.logs"
# 할 일이 없던 폴링 회차의 span 표시. EDOT Collector가 이 속성이 true인 span이 든 trace를 통째로 버린다
# (gitops logging/base/otel-collector/otel.yml의 tail_sampling, LOG_SCHEMA.md 7절)
IDLE_POLLING = "loresentry.polling.idle"

# Kafka 헤더 목록(confluent-kafka의 [(이름, bytes)]) 또는 이미 풀어 둔 dict
Headers = Sequence[tuple[str, bytes | str | None]] | Mapping[str, bytes | str | None]


def setup_tracing(service_name: str) -> None:
    """SDK TracerProvider를 설정한다. 이미 SDK provider가 있으면(두 번째 호출, 테스트) 아무것도 하지 않는다.

    ``OTEL_EXPORTER_OTLP_ENDPOINT``가 있으면(dev overlay가 collector 주소를 넣는다) span을 묶어 OTLP/HTTP로 보낸다.
    없으면(로컬 실행, 테스트) 보내지 않는다. 보내지 못한 span은 버려지고 요청 처리는 멈추지 않는다.
    """
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    # 서비스 이름과 버전(이미지 태그, 로그의 service.version과 같은 값). Resource.create가 OTEL_RESOURCE_ATTRIBUTES
    # (예: deployment.environment.name=dev, Kibana APM의 환경 구분)를 함께 합친다
    attributes = {"service.name": service_name}
    if version := os.getenv("APP_VERSION"):
        attributes["service.version"] = version
    provider = TracerProvider(
        resource=Resource.create(attributes), sampler=ParentBased(ALWAYS_ON)
    )
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # exporter가 같은 환경 변수에서 주소를 읽고 /v1/traces를 붙인다
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # 헤더는 traceparent 하나만 읽고 쓴다(tracestate·baggage는 쓰지 않는다)
    propagate.set_global_textmap(TraceContextTextMapPropagator())


def traceparent() -> str | None:
    """지금 span의 W3C traceparent(55자). 나중에 처리할 일(summary_queue 행)에 저장하면 그 trace를 잇는다. span이 없으면 None."""
    span = trace.get_current_span().get_span_context()
    if not span.is_valid:
        return None
    return f"00-{span.trace_id:032x}-{span.span_id:016x}-{int(span.trace_flags):02x}"


def current_ids() -> tuple[str | None, str | None]:
    """지금 span의 (trace.id, span.id). span이 없으면 (None, None)."""
    span = trace.get_current_span().get_span_context()
    if not span.is_valid:
        return None, None
    return f"{span.trace_id:032x}", f"{span.span_id:016x}"


def parse_traceparent(value: str | None) -> SpanContext | None:
    """저장해 둔 traceparent를 원격 span 문맥으로. 값이 없거나 형식이 틀리면 None."""
    match = TRACEPARENT.fullmatch(value) if value else None
    if match is None:
        return None
    span = SpanContext(
        trace_id=int(match.group(1), 16),
        span_id=int(match.group(2), 16),
        is_remote=True,
        trace_flags=TraceFlags(int(match.group(3), 16)),
    )
    return span if span.is_valid else None


def attach_remote(span: SpanContext) -> object:
    """저장해 둔 span 문맥을 지금 문맥으로 붙인다. 그 안에서 만드는 span(DB 쿼리 등)이 그 trace의 자식이 된다. 돌려준 token으로 뗀다."""
    return otel_context.attach(trace.set_span_in_context(NonRecordingSpan(span)))


def activate(span: trace.Span) -> object:
    """이 패키지가 연 span을 지금 문맥의 span으로 둔다. 돌려준 token으로 뗀다."""
    return otel_context.attach(trace.set_span_in_context(span))


def detach(token: object) -> None:
    otel_context.detach(token)


def start_span(
    name: str, kind: trace.SpanKind, headers: Mapping[str, str] | None = None
) -> trace.Span:
    """새 span을 시작한다. headers에 traceparent가 있으면 그 trace를 잇고, 없거나 틀리면 새 trace를 시작한다.

    headers가 None이면(스케줄 회차) 지금 문맥과 상관없이 새 trace를 시작한다(LOG_SCHEMA.md 7절 "시작점").
    """
    parent = (
        propagate.extract(headers) if headers is not None else otel_context.Context()
    )
    return trace.get_tracer(TRACER).start_span(name, context=parent, kind=kind)


def header_text(headers: Headers | None) -> dict[str, str]:
    """Kafka 헤더를 {이름: 문자열}로. 같은 이름이 여럿이면 마지막 값(Java lastHeader와 같다)."""
    if headers is None:
        return {}
    items = headers.items() if isinstance(headers, Mapping) else headers
    found: dict[str, str] = {}
    for name, value in items:
        if value is None:
            continue
        found[name] = (
            value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        )
    return found
