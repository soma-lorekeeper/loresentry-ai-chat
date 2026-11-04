#!/bin/bash
# 컨테이너 시작 스크립트. 두 프로세스(API, summarizer)를 EDOT Python distro로 띄운다(docs LOG_SCHEMA.md 7절 "추적 엔진").
#
# - 프로세스: 넘겨받은 명령이 python -m app.summarizer면 summarizer(ai-chat-summarizer), 그 밖(이미지 CMD의 uvicorn)은
#   API(ai-chat-api)다. 로그의 service.name(app/main.py, app/summarizer/__main__.py의 SERVICE)과 같은 Kubernetes 이름을
#   span resource의 service.name(OTEL_SERVICE_NAME)으로 둔다. 환경 변수로 이미 주었으면 그 값이 먼저다.
# - migration: API(uvicorn)일 때만 먼저 flyway migrate를 돌린다. summarizer 파드는 돌리지 않는다. flyway의 줄은 앱과 같은
#   ECS JSON으로 바꾼다(python -m app.logs.pipe. 평문 줄은 log.level unknown이 된다). 이 변환 프로세스는 distro로 띄우지 않는다.
#   pipefail이 flyway의 종료 코드를 지킨다. 접속 URL에는 비밀번호를 넣지 않고 FLYWAY_PASSWORD로만 넘긴다.
# - 추적 기본값: 아래 OTEL_* 값은 비어 있을 때만 넣는다. gitops overlay가 준 값(dev의 OTEL_TRACES_EXPORTER=otlp, collector 주소,
#   resource의 환경·파드 이름)이 먼저다. 그래서 여기에는 어디서 돌아도 안전한 값만 둔다.
#   - 보내지 않음(prod 포함). 꺼 두어도 trace ID는 만들어지고 전파된다. 지표는 Prometheus, 로그는 stdout → Filebeat가 모은다
#   - 보낼 때는 OTLP/HTTP protobuf(collector 4318). distro의 기본값(grpc)을 쓰지 않는다
#   - sampler는 parentbased_always_on(LOG_SCHEMA.md 7절 "span 저장"의 dev 100%). distro 기본 sampler(composite)는 root span
#     문맥에 tracestate(ot=th:0)를 넣는데 우리 규약은 traceparent 하나다. 전파기도 W3C traceparent(tracecontext)만 쓴다(baggage는
#     쓰지 않는다)
#   - span 속성은 OpenTelemetry 안정 이름(http.request.method, url.path, http.route, db.query.text 등)
#   - GenAI 대화 내용(프롬프트, 모델 응답)은 span에 담지 않는다(collector 규칙은 잘못 켜졌을 때의 두 번째 방어선)
#   - 계측은 requirements.txt에 둔 것만 설치하고, distro가 저절로 켜는 것 중 쓰지 않는 것을 끈다:
#     fastapi(로깅 팩토리의 setup_tracing이 앱에 직접 단다), httpx2(OpenAI SDK의 HTTP client. OpenAI 호출에는 traceparent를
#     싣지 않는다), logging(로그 줄의 ID는 로깅 팩토리가 넣는다), system_metrics(distro가 함께 설치하는 지표 계측),
#     confluent_kafka(설치하지 않는다. traceparent를 바꾸지 않고 하나 더 붙여, Kafka는 로깅 팩토리가 맡는다)
#   - resource detector는 네트워크를 쓰지 않는 기본 목록만 쓴다(distro의 기본 목록에서 클라우드 detector를 뺀 것). Kubernetes에서
#     distro가 더 켜는 aws_eks는 파드 서비스 계정으로 클러스터 정보를 읽다가 403을 받아 stderr에 평문 줄(log.level unknown)을
#     남기고, gcp_resource_detector는 이 클러스터와 상관없다. 파드·namespace·인스턴스·환경은 overlay가 OTEL_RESOURCE_ATTRIBUTES로
#     넣는다. otel(OTEL_RESOURCE_ATTRIBUTES·OTEL_SERVICE_NAME)을 맨 뒤에 두어 그 값이 다른 detector의 값보다 먼저다
# - service.version: 이미지 태그(APP_VERSION, Dockerfile의 ENV)를 span resource 앞에 더한다. 로그의 service.version과 같은 값이다.
#   overlay가 준 OTEL_RESOURCE_ATTRIBUTES가 있으면 그 뒤에 이어 붙인다.
# - exec: opentelemetry-instrument가 distro를 붙인 뒤 명령을 exec하므로 그 프로세스(uvicorn, python)가 PID 1이 되어
#   Kubernetes의 종료 신호(SIGTERM)를 직접 받는다(graceful shutdown).
set -eo pipefail

if [ "$1" = python ] && [ "$2" = -m ] && [ "$3" = app.summarizer ]; then
    service=ai-chat-summarizer
else
    service=ai-chat-api
fi

if [ "$1" = uvicorn ]; then
    export FLYWAY_URL="jdbc:postgresql://${DB_HOST:-localhost}:${DB_PORT:-5432}/${DB_NAME:-ai_chat}"
    export FLYWAY_USER="${DB_USERNAME:-ai_chat_svc}"
    export FLYWAY_PASSWORD="${DB_PASSWORD:-}"
    export FLYWAY_LOCATIONS="filesystem:/app/db/migration"
    flyway migrate 2>&1 | python -m app.logs.pipe flyway ai-chat-api
fi

export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-$service}"
export OTEL_TRACES_EXPORTER="${OTEL_TRACES_EXPORTER:-none}"
export OTEL_METRICS_EXPORTER="${OTEL_METRICS_EXPORTER:-none}"
export OTEL_LOGS_EXPORTER="${OTEL_LOGS_EXPORTER:-none}"
export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-http/protobuf}"
export OTEL_TRACES_SAMPLER="${OTEL_TRACES_SAMPLER:-parentbased_always_on}"
export OTEL_PROPAGATORS="${OTEL_PROPAGATORS:-tracecontext}"
export OTEL_SEMCONV_STABILITY_OPT_IN="${OTEL_SEMCONV_STABILITY_OPT_IN:-http,database}"
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-false}"
export OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="${OTEL_PYTHON_DISABLED_INSTRUMENTATIONS:-fastapi,httpx2,logging,system_metrics,confluent_kafka}"
export OTEL_EXPERIMENTAL_RESOURCE_DETECTORS="${OTEL_EXPERIMENTAL_RESOURCE_DETECTORS:-process_runtime,os,telemetry_distro,service_instance,containerid,otel}"
export OTEL_RESOURCE_ATTRIBUTES="service.version=${APP_VERSION:-unknown}${OTEL_RESOURCE_ATTRIBUTES:+,${OTEL_RESOURCE_ATTRIBUTES}}"

exec opentelemetry-instrument "$@"
