"""테스트 전체가 쓰는 OpenTelemetry SDK. 이미지에서는 EDOT distro(docker-entrypoint.sh의 opentelemetry-instrument)가 하는 일이다.

- TracerProvider를 한 번 꽂고, 끝난 span을 메모리(``EXPORTER``)에 모은다. 테스트마다 비운다.
- 이미지와 같이 span 속성은 안정 이름(``OTEL_SEMCONV_STABILITY_OPT_IN``)으로 내고, distro가 켜는 계측 중 테스트가 지나가는 것
  (content 호출의 httpx, DB 쿼리의 psycopg)을 켠다. FastAPI는 앱마다 ``app.logs.setup_tracing``이 단다.
- ``spans`` fixture는 그 테스트에서 끝난 span 목록을 돌려주는 함수다.
"""

import os

import pytest

# 계측을 import하기 전에 둔다. 계측이 처음 쓸 때 한 번만 읽는다
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")

from opentelemetry import trace  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)
HTTPXClientInstrumentor().instrument()
PsycopgInstrumentor().instrument()


@pytest.fixture(autouse=True)
def _clear_spans():
    EXPORTER.clear()


@pytest.fixture
def spans():
    """그 테스트에서 지금까지 끝난 span 목록(끝난 순서)을 돌려주는 함수."""
    return lambda: list(EXPORTER.get_finished_spans())
