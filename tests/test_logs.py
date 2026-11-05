import ast
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import suppress
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import RateLimitError
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.instrumentation import dbapi
from opentelemetry.trace import SpanKind, StatusCode

from app.logs import (
    AppLogger,
    LogField,
    LogFlow,
    LogScope,
    Outcome,
    _metrics,
    configure_logging,
    traceparent,
    watch_connection_pool,
    watch_event_loop,
)
from app.logs._format import EcsFormatter
from app.logs._logger import layer_of
from app.logs.pipe import forward
from app.main import create_app
from app.manuscript import KeyUnavailable, ProjectKeys
from app.model import ModelFailure, create
from app.settings import Settings
from app.summarizer.consumer import consumer_config
from app.summarizer.settings import SummarizerSettings
from tests.support import FakeKms, field, lines_with, log_lines

APP = Path(__file__).parent.parent / "app"
ENTRYPOINT = APP.parent / "docker-entrypoint.sh"
TRACE = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE}-b7ad6b7169203331-01"
# A span of the tests' own, standing in for what an instrumentation opens (DB query, HTTP call)
TRACER = trace.get_tracer("tests")
# Layers whose public functions must leave an entry line (LOG_SCHEMA.md section 5)
LAYERS = {
    "controller",
    "service",
    "client",
    "repository",
    "consumer",
    "publisher",
    "scheduler",
}
ENTRY_FLOWS = {"schedule", "kafka_record", "task"}
LEVELS = {"info", "warn", "error"}


@pytest.fixture
def lines(capsys):
    """Reads the lines written so far; logging is set up as in the service, tracing by conftest."""
    configure_logging("ai-chat-test")
    capsys.readouterr()
    return lambda: log_lines(capsys.readouterr().out)


def ids(span) -> tuple[str, str]:
    """(trace.id, span.id) of a span as the log lines write them."""
    context = span.get_span_context()
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def line_ids(line: dict) -> tuple[str, str]:
    return field(line, "trace.id"), field(line, "span.id")


def named(found, name: str):
    """The one finished span with this name."""
    [span] = [span for span in found if span.name == name]
    return span


class Color(Enum):
    RED = 1


# ---------------------------------------------------------------- fields


def test_params_keep_numbers_and_ids_but_only_the_length_of_text():
    project = uuid4()
    entries = {
        name: value
        for made in (
            LogField.param("title", "레나의 비밀"),
            LogField.param("ids", ["a", "b", "c"]),
            LogField.param("map", {"a": 1}),
            LogField.param("body", b"12345"),
            LogField.param("done", True),
            LogField.param("count", 3),
            LogField.param("color", Color.RED),
            LogField.param("target_id", project),
            LogField.code("intent", "ask", {"ask"}),
            LogField.code("intent", "free text", {"ask"}),
            LogField.result_code("outcome", "DONE", {"DONE"}),
        )
        for name, value in made.entries
    }

    assert entries == {
        "loresentry.param.title": "[redacted]",
        "loresentry.param.title_length": 6,
        "loresentry.param.ids_count": 3,
        "loresentry.param.map_count": 1,
        "loresentry.param.body_bytes": 5,
        "loresentry.param.done": True,
        "loresentry.param.count": 3,
        "loresentry.param.color": "RED",
        "loresentry.param.target_id": str(project),
        "loresentry.param.intent": "other",
        "loresentry.result.outcome": "DONE",
    }
    assert LogField.param("missing", None) is None
    with pytest.raises(TypeError):
        LogField.param("object", object())


def test_ids_are_kept_only_as_canonical_uuids():
    project = uuid4()

    assert LogField.project_id(project).entries == (
        ("loresentry.project_id", str(project)),
    )
    assert LogField.project_id(str(project).upper()).entries == (
        ("loresentry.project_id", str(project)),
    )
    for wrong in ("nope", project.hex, "레나", None):
        assert LogField.project_id(wrong) is None


def test_urls_lose_credentials_query_and_fragment():
    full = LogField.url_full("http://user:secret@content:8080/a/b?q=레나#top")

    assert full.entries == (("url.full", "http://content:8080/a/b"),)


def test_a_safe_stack_trace_has_the_chain_but_no_messages():
    def inner():
        raise KeyError("secret key 레나")

    try:
        try:
            inner()
        except KeyError as error:
            raise RuntimeError("secret outer") from error
    except RuntimeError as error:
        trace = dict(LogField.safe_stack_trace(error).entries)["error.stack_trace"]

    assert "secret" not in trace and "레나" not in trace
    assert trace.splitlines()[0] == "RuntimeError"
    assert "KeyError" in trace.splitlines()
    raised_at = inner.__code__.co_firstlineno + 1
    assert f"\tat {__name__}.{inner.__qualname__}(test_logs.py:{raised_at})" in trace


# ---------------------------------------------------------------- formatter


def test_lines_are_nested_ecs_json_and_framework_errors_lose_their_message():
    formatter = EcsFormatter("ai-chat-api", "build-7-1")
    try:
        raise ValueError("password=hunter2")
    except ValueError:
        record = logging.getLogger("uvicorn.error").makeRecord(
            "uvicorn.error", logging.WARNING, "x.py", 1, "boom", (), sys.exc_info()
        )

    line = json.loads(formatter.format(record))

    assert line["log"] == {"level": "WARN", "logger": "uvicorn.error"}
    assert line["service"] == {"name": "ai-chat-api", "version": "build-7-1"}
    assert line["ecs"] == {"version": "8.11"}
    assert line["process"]["thread"]["name"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", line["@timestamp"])
    assert line["error"]["type"] == "ValueError"
    assert line["error"]["stack_trace"].startswith("ValueError\n\tat ")
    assert "hunter2" not in json.dumps(line)
    assert "message" not in line["error"]
    assert not any("." in key for key in line if key != "@timestamp")


def test_our_lines_carry_location_fields_and_context(lines):
    def ask():
        AppLogger.current().info("chat session get", LogField.chat_session_id(uuid4()))

    ask()
    [line] = lines()

    assert line["log"]["logger"] == __name__
    assert line["log"]["origin"]["function"] == "ask"
    assert line["log"]["layer"] == "other"
    assert line["log"]["package"] == __name__.rpartition(".")[0]
    assert field(line, "loresentry.chat.session_id")


def test_the_layer_comes_from_log_layer_or_the_module_name():
    assert layer_of("app.routes", {}) == "controller"
    assert layer_of("app.setting_check.store", {}) == "repository"
    assert layer_of("app.content", {}) == "client"
    assert layer_of("app.summarizer.worker", {}) == "scheduler"
    assert layer_of("app.summarizer.consumer", {}) == "consumer"
    assert layer_of("app.turns", {"LOG_LAYER": "service"}) == "service"
    assert layer_of("app.main", {}) == "other"


def test_warn_and_error_lines_without_an_exception_carry_the_call_site_stack(lines):
    def reject():
        logger = AppLogger.current()
        logger.warn("session conflict", LogField.error_type("CHAT_TURN_RUNNING"))

    reject()
    logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.")
    logging.getLogger("opentelemetry.exporter").error("Failed to export span batch")
    AppLogger.current().info("chat session get")

    ours, uvicorn, exporter, info = lines()
    stack = field(ours, "error.stack_trace").splitlines()
    assert stack[0] == "log site: CHAT_TURN_RUNNING"
    # The newest frame is where the line was logged; the logging frames are left out
    logged_at = reject.__code__.co_firstlineno + 2
    assert (
        stack[1] == f"\tat {__name__}.{reject.__qualname__}(test_logs.py:{logged_at})"
    )
    assert stack[2].startswith(
        f"\tat {__name__}.test_warn_and_error_lines_without_an_exception_carry_"
    )
    assert 2 < len(stack) <= 31
    for frame in stack[1:]:
        # module.function(file:line) only: no source text, no message
        assert re.fullmatch(r"\tat \S+\([^/()]+:\d+\)", frame)
        assert not frame.startswith(("\tat app.logs.", "\tat logging."))
    # Framework lines (uvicorn, the OTLP exporter) are labelled with their logger
    for line, logger in (
        (uvicorn, "uvicorn.error"),
        (exporter, "opentelemetry.exporter"),
    ):
        framework = field(line, "error.stack_trace").splitlines()
        assert framework[0] == f"log site: {logger}"
        assert framework[1].startswith(f"\tat {__name__}.")
    assert "Invalid HTTP" not in json.dumps(field(uvicorn, "error"))
    assert field(info, "error.stack_trace") is None
    for line in (ours, uvicorn, exporter):
        assert "message" not in line["error"]


# ---------------------------------------------------------------- flows


def test_a_task_leaves_start_and_end_lines_even_when_it_raises(lines, spans):
    with LogFlow.task("chat turn", LogField.param("step", 1)) as task:
        task.result(LogField.result("steps", 2))
    with pytest.raises(ValueError), LogFlow.task("chat turn"):
        raise ValueError("본문 내용")
    with LogFlow.task("chat turn") as task:
        task.fail("MODEL_DEADLINE")

    found = lines()
    assert [(line["log"]["level"], line["message"]) for line in found] == [
        ("INFO", "chat turn start"),
        ("INFO", "chat turn completed"),
        ("INFO", "chat turn start"),
        ("ERROR", "chat turn failed"),
        ("INFO", "chat turn start"),
        ("WARN", "chat turn failed"),
    ]
    assert field(found[1], "loresentry.result.steps") == 2
    assert field(found[1], "event.duration") > 0
    assert field(found[3], "error.type") == "ValueError"
    assert field(found[3], "error.stack_trace").startswith("ValueError")
    assert "본문" not in json.dumps(found, ensure_ascii=False)
    assert field(found[5], "error.type") == "MODEL_DEADLINE"
    # A failure without an exception carries where it was logged
    assert field(found[5], "error.stack_trace").startswith("log site: MODEL_DEADLINE\n")
    # One span per task; its lines are inside it and a failure marks it ERROR
    done, raised, failed = spans()
    assert [span.name for span in (done, raised, failed)] == ["chat turn"] * 3
    for (start, end), span in zip(
        ((0, 1), (2, 3), (4, 5)), (done, raised, failed), strict=True
    ):
        assert line_ids(found[start]) == line_ids(found[end]) == ids(span)
        assert span.kind is SpanKind.INTERNAL
        # A task started outside any flow is a root
        assert span.parent is None
    assert done.status.status_code is StatusCode.UNSET
    assert (raised.status.status_code, raised.status.description) == (
        StatusCode.ERROR,
        "ValueError",
    )
    # The exception event names the class only: no message, no stack
    assert [dict(event.attributes) for event in raised.events] == [
        {"exception.type": "ValueError"}
    ]
    assert (failed.status.status_code, failed.status.description) == (
        StatusCode.ERROR,
        "MODEL_DEADLINE",
    )
    assert list(failed.events) == []


def test_a_line_carries_the_span_current_when_it_is_written(lines, spans):
    with LogFlow.task("chat turn"):
        AppLogger.current().info("chat turn step")
        # What an instrumentation opens inside the task (a DB query)
        with TRACER.start_as_current_span("SELECT ai_chat"):
            AppLogger.current().info("chat turn held get")
            logging.getLogger("psycopg").warning("framework line")
    with TRACER.start_as_current_span("outside"):
        AppLogger.current().info("chat turn outside")
    AppLogger.current().info("chat turn detached")

    start, step, held, framework, end, outside, detached = lines()
    found = spans()
    task, query = named(found, "chat turn"), named(found, "SELECT ai_chat")
    assert line_ids(start) == line_ids(step) == line_ids(end) == ids(task)
    assert line_ids(held) == line_ids(framework) == ids(query)
    assert query.parent.span_id == task.context.span_id
    assert line_ids(outside) == ids(named(found, "outside"))
    assert line_ids(detached) == (None, None)


def test_a_line_keeps_the_flow_ids_when_the_current_span_is_missing_or_elsewhere(
    lines, spans
):
    with LogScope.stored(TRACEPARENT, "summary job"):
        # A span of another trace, or no span at all (a thread the context did not reach)
        with TRACER.start_as_current_span("elsewhere", context=Context()):
            AppLogger.current().info("document summary other trace")
        token = otel_context.attach(Context())
        try:
            AppLogger.current().info("document summary no span")
            logging.getLogger("psycopg").warning("framework line")
        finally:
            otel_context.detach(token)

    job = named(spans(), "summary job")
    found = lines()
    assert len(found) == 3
    for line in found:
        assert line_ids(line) == ids(job)


def test_a_polling_round_without_lines_leaves_nothing(lines):
    with LogFlow.polling("summary-tick"):
        pass
    assert lines() == []

    with LogFlow.polling("summary-tick") as tick:
        AppLogger.current().info("summary jobs claimed", LogField.result("jobs", 1))
        tick.result(LogField.result("summarized", 1))
    with LogFlow.polling("summary-tick"):
        raise RuntimeError("swallowed")

    found = lines()
    assert [line["message"] for line in found] == [
        "schedule run start",
        "summary jobs claimed",
        "schedule run completed",
        "schedule run start",
        "schedule run failed",
    ]
    assert {field(line, "loresentry.schedule.name") for line in found} == {
        "summary-tick"
    }
    assert field(found[0], "trace.id") == field(found[2], "trace.id")
    assert field(found[3], "trace.id") != field(found[0], "trace.id")
    assert field(found[4], "error.type") == "RuntimeError"


def test_a_round_span_opens_with_the_round_and_only_an_idle_one_is_marked(lines, spans):
    with LogFlow.polling("summary-tick"):
        # The queue claim query of the round
        with TRACER.start_as_current_span("SELECT ai_chat"):
            pass
    with LogFlow.polling("summary-tick"):
        AppLogger.current().info("summary jobs claimed")
    with LogFlow.polling("summary-tick") as failed:
        failed.fail("SUMMARY_FAILED")

    claim, idle, busy, failing = spans()
    start, claimed, completed, failed_start, failed_end = lines()
    # Every round is a new root, open before its first query
    assert {span.name for span in (idle, busy, failing)} == {"summary-tick"}
    assert [span.parent for span in (idle, busy, failing)] == [None] * 3
    assert claim.parent.span_id == idle.context.span_id
    # Only the round that left no line is marked; the collector drops that trace
    assert idle.attributes.get("loresentry.polling.idle") is True
    assert "loresentry.polling.idle" not in busy.attributes
    assert "loresentry.polling.idle" not in failing.attributes
    assert failing.status.status_code is StatusCode.ERROR
    assert line_ids(start) == line_ids(claimed) == line_ids(completed) == ids(busy)
    assert line_ids(failed_start) == line_ids(failed_end) == ids(failing)


def test_a_kafka_record_continues_the_header_trace(lines, spans):
    event = uuid4()
    headers = [
        ("ce_type", b"FileChanged"),
        ("ce_id", str(event).encode()),
        ("ce_subject", b"not-a-uuid"),
        ("traceparent", TRACEPARENT.encode()),
    ]
    # Another flow's span is current; a record's parent is its header alone
    with TRACER.start_as_current_span("other flow"):
        with LogFlow.kafka_record(
            "content.project.changed.v2",
            2,
            41,
            headers,
            {"FileChanged"},
            "project changed",
            "ai-chat-summarizer",
        ) as flow:
            stored = traceparent()
            logging.getLogger("psycopg").warning("framework line")
            flow.outcome(Outcome.SKIPPED)
        with (
            pytest.raises(KeyError),
            LogFlow.kafka_record(
                "t",
                0,
                1,
                [("ce_type", b"Other")],
                {"FileChanged"},
                "project changed",
                "ai-chat-summarizer",
            ),
        ):
            raise KeyError("x")

    found = lines()
    received, framework, processed, _, failed = found
    consumer, unheaded, _other = spans()
    assert stored.startswith(f"00-{TRACE}-{ids(consumer)[1]}-") and len(stored) == 55
    for line in (received, framework, processed):
        assert line_ids(line) == ids(consumer)
        assert field(line, "loresentry.kafka") == {
            "topic": "content.project.changed.v2",
            "partition": 2,
            "offset": 41,
        }
        assert field(line, "loresentry.cloudevent") == {
            "id": str(event),
            "type": "FileChanged",
        }
    assert received["message"] == "project changed event received"
    assert framework["log"]["logger"] == "psycopg"
    assert field(processed, "loresentry.result.outcome") == "SKIPPED"
    assert field(failed, "loresentry.cloudevent.type") == "other"
    assert (failed["log"]["level"], field(failed, "error.type")) == ("WARN", "KeyError")
    assert field(failed, "loresentry.result.outcome") == "FAILED"
    # The failed record's end line keeps the message-free stack
    assert field(failed, "error.stack_trace").startswith("KeyError\n")
    # A CONSUMER span under the header's span, with the messaging attributes
    assert (consumer.name, consumer.kind) == (
        "content.project.changed.v2 process",
        SpanKind.CONSUMER,
    )
    assert ids(consumer)[0] == TRACE
    assert consumer.parent.span_id == 0xB7AD6B7169203331 and consumer.parent.is_remote
    assert dict(consumer.attributes) == {
        "messaging.system": "kafka",
        "messaging.destination.name": "content.project.changed.v2",
        "messaging.consumer.group.name": "ai-chat-summarizer",
        "messaging.destination.partition.id": "2",
        "messaging.kafka.offset": 41,
        "messaging.operation.type": "process",
    }
    assert consumer.status.status_code is StatusCode.UNSET
    # Without a traceparent the record starts a new trace, not a child of the current span
    assert unheaded.parent is None
    assert ids(unheaded)[0] != ids(_other)[0]
    assert line_ids(failed) == ids(unheaded)
    assert (unheaded.status.status_code, unheaded.status.description) == (
        StatusCode.ERROR,
        "KeyError",
    )


def test_a_stored_scope_opens_a_span_under_the_stored_trace(lines, spans):
    document = uuid4()
    with LogFlow.polling("summary-tick"):
        AppLogger.current().info("summary jobs claimed")
        with LogScope.stored(
            TRACEPARENT, "summary job", LogField.document_id(document)
        ):
            AppLogger.current().info("document summary previous get")
            # What psycopg opens for the job's query
            with TRACER.start_as_current_span("SELECT ai_chat"):
                pass
        with LogScope.stored(None, "summary job"):
            AppLogger.current().info("document summary previous get")
        with pytest.raises(RuntimeError), LogScope.stored("00-wrong", "summary job"):
            raise RuntimeError("x")

    query, job, fresh, broken, tick = spans()
    _start, _claimed, stored, unstored, completed = lines()
    # Parented on the stored traceparent, linked to the round of another trace
    assert (job.name, job.kind) == ("summary job", SpanKind.INTERNAL)
    assert ids(job)[0] == TRACE
    assert job.parent.span_id == 0xB7AD6B7169203331
    assert [link.context.span_id for link in job.links] == [tick.context.span_id]
    # The job's own spans attach to it, and its lines carry it
    assert query.parent.span_id == job.context.span_id
    assert line_ids(stored) == ids(job)
    assert field(stored, "loresentry.document_id") == str(document)
    # No stored value, or a wrong one: that job alone starts a new trace
    for span in (fresh, broken):
        assert span.parent is None
        assert ids(span)[0] not in (TRACE, ids(tick)[0])
        assert [link.context.span_id for link in span.links] == [tick.context.span_id]
    assert line_ids(unstored) == ids(fresh)
    assert broken.status.status_code is StatusCode.ERROR
    # Closing the scope goes back to the round
    assert line_ids(completed) == ids(tick)


# ---------------------------------------------------------------- outgoing calls


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def create(self, **_):
        if self.error is not None:
            raise self.error
        return self.response


def openai(response=None, error=None):
    return SimpleNamespace(
        responses=FakeResponses(response, error),
        base_url=httpx2.URL("https://api.openai.com/v1/"),
    )


def test_a_call_writes_its_result_line_inside_its_span(lines, spans):
    with LogFlow.task("chat turn"):
        with LogFlow.call("content", "http://content-api/internal/x?q=1") as call:
            # What httpx opens for the request
            with TRACER.start_as_current_span("GET", kind=SpanKind.CLIENT):
                pass
            call.status(200)
        with LogFlow.call("content") as call:
            call.status(503)
        with LogFlow.call("kms") as call:
            call.fail("AccessDeniedException")
        with pytest.raises(OSError), LogFlow.call("kms"):
            raise OSError("secret path")
        with LogFlow.call("openai", server="api.openai.com", model="gpt-6-luna"):
            pass

    http, content, upstream, denied, raised, answered, task = spans()
    _start, completed, unavailable, refused, failed, replied, _end = lines()
    assert [span.name for span in (content, upstream, denied, raised, answered)] == [
        "content call",
        "content call",
        "kms call",
        "kms call",
        "openai call",
    ]
    # Each result line is written inside its call span, a child of the current span
    for span, line in zip(
        (content, upstream, denied, raised, answered),
        (completed, unavailable, refused, failed, replied),
        strict=True,
    ):
        assert span.parent.span_id == task.context.span_id
        assert line_ids(line) == ids(span)
    # The instrumentation's CLIENT span is the call span's child
    assert http.parent.span_id == content.context.span_id
    assert content.kind is SpanKind.INTERNAL
    assert content.status.status_code is StatusCode.UNSET
    assert (upstream.status.status_code, upstream.status.description) == (
        StatusCode.ERROR,
        "UPSTREAM_SERVER_ERROR",
    )
    assert (denied.status.status_code, denied.status.description) == (
        StatusCode.ERROR,
        "AccessDeniedException",
    )
    assert (raised.status.status_code, raised.status.description) == (
        StatusCode.ERROR,
        "OSError",
    )
    # A target without an instrumentation: our span is its CLIENT span, a GenAI one here
    assert answered.kind is SpanKind.CLIENT
    assert dict(answered.attributes) == {
        "server.address": "api.openai.com",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-6-luna",
    }
    assert field(unavailable, "error.stack_trace").startswith(
        "log site: UPSTREAM_SERVER_ERROR\n"
    )
    assert field(failed, "error.stack_trace").startswith("OSError\n")
    assert "secret" not in json.dumps([unavailable, failed])


def test_openai_call_lines_carry_model_effort_and_tokens(lines, spans):
    usage = SimpleNamespace(
        input_tokens=1200,
        output_tokens=90,
        input_tokens_details=SimpleNamespace(cached_tokens=800),
        output_tokens_details=SimpleNamespace(reasoning_tokens=30),
    )
    done = SimpleNamespace(status="completed", usage=usage)
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    limited = RateLimitError(
        "rate limited, prompt: 레나",
        response=httpx2.Response(429, request=request),
        body={"error": {"code": "rate_limit_exceeded"}},
    )

    asyncio.run(
        create(
            openai(done), model="gpt-6-luna", reasoning={"effort": "low"}, input="레나"
        )
    )
    with pytest.raises(ModelFailure):
        asyncio.run(create(openai(error=limited), model="gpt-6-luna", tools=[]))

    completed, failed = lines()
    assert completed["message"] == "openai call completed"
    assert (completed["log"]["logger"], completed["log"]["layer"]) == (
        "app.model",
        "client",
    )
    assert field(completed, "loresentry.result") == {
        "model": "gpt-6-luna",
        "effort": "low",
        "kind": "text",
        "input_tokens": 1200,
        "cached_tokens": 800,
        "output_tokens": 90,
        "reasoning_tokens": 30,
    }
    assert field(completed, "event.duration") > 0
    assert (failed["message"], failed["log"]["level"]) == ("openai call failed", "WARN")
    assert field(failed, "error.type") == "MODEL_RATE_LIMITED"
    assert field(failed, "http.response.status_code") == 429
    assert "레나" not in json.dumps([completed, failed], ensure_ascii=False)
    # The Responses API has no instrumentation; the call span is the OpenAI CLIENT span
    done_span, failed_span = spans()
    for span, line in ((done_span, completed), (failed_span, failed)):
        assert (span.name, span.kind) == ("openai call", SpanKind.CLIENT)
        assert span.attributes["server.address"] == "api.openai.com"
        assert span.attributes["gen_ai.request.model"] == "gpt-6-luna"
        assert line_ids(line) == ids(span)
    assert (failed_span.status.status_code, failed_span.status.description) == (
        StatusCode.ERROR,
        "MODEL_RATE_LIMITED",
    )


def test_kms_call_lines_carry_the_kms_code_on_failure(lines, spans):
    kms = FakeKms()
    project = str(uuid4())
    wrapped = kms.wrap(project, b"k" * 32)
    keys = ProjectKeys(kms, kms.key_id)

    keys.unwrap(project, wrapped)
    keys.unwrap(project, wrapped)
    kms.fail_with = "AccessDeniedException"
    with pytest.raises(KeyUnavailable):
        ProjectKeys(kms, kms.key_id).unwrap(project, wrapped)

    found = lines()
    assert [line["message"] for line in found] == [
        "kms project key unwrap call",
        "kms call completed",
        "kms project key unwrap call",
        "kms project key unwrap call",
        "kms call failed",
    ]
    assert field(found[1], "event.duration") > 0
    assert field(found[0], "loresentry.project_id") == project
    assert (found[4]["log"]["level"], field(found[4], "error.type")) == (
        "WARN",
        "ClientError",
    )
    assert field(found[4], "error.code") == "AccessDeniedException"
    # Only the calls that reach KMS (not the cached key) have a span
    unwrapped, refused = spans()
    assert (unwrapped.name, refused.name) == ("kms call", "kms call")
    assert line_ids(found[1]) == ids(unwrapped)
    assert line_ids(found[4]) == ids(refused)
    assert refused.status.status_code is StatusCode.ERROR


# ---------------------------------------------------------------- requests


def test_request_lines_carry_the_route_user_and_outcome(lines, spans):
    app = create_app(Settings())

    @app.get("/boom/{item_id}")
    def boom(item_id: str) -> None:
        raise RuntimeError("secret detail 레나")

    client = TestClient(app)
    user = uuid4()
    session = uuid4()
    rejected = client.get(f"/chat/sessions/{session}/messages")
    failed = client.get("/boom/7", headers={"X-User-Id": str(user)})
    health = client.get("/health")
    unknown = client.get("/nope")

    assert rejected.status_code == 401
    assert failed.status_code == 500
    assert failed.json()["code"] == "INTERNAL_ERROR"
    assert health.status_code == 200
    assert unknown.status_code == 404
    found = lines()
    received = lines_with(found, "request received")
    completed = lines_with(found, "request completed")
    assert len(received) == len(completed) == 4
    assert field(received[0], "url.path") == f"/chat/sessions/{session}/messages"
    assert field(received[0], "http.route") == "/chat/sessions/{session_id}/messages"
    assert field(received[0], "http.request.method") == "GET"
    assert (received[0]["log"]["layer"], received[0]["log"]["logger"]) == (
        "filter",
        "app.logs._request",
    )
    assert field(completed[0], "http.response.status_code") == 401
    assert field(completed[0], "error.type") == "USER_CONTEXT_REQUIRED"
    assert completed[0]["log"]["level"] == "WARN"
    assert field(completed[0], "event.duration") > 0
    assert len(field(completed[0], "trace.id")) == 32
    assert rejected.json()["trace_id"] == field(completed[0], "trace.id")
    # an unexpected exception: one error line, the result line, without the message
    errors = [line for line in found if line["log"]["level"] == "ERROR"]
    assert errors == [completed[1]]
    assert field(completed[1], "http.response.status_code") == 500
    assert field(completed[1], "error.type") == "RuntimeError"
    assert "boom" in field(completed[1], "error.stack_trace")
    assert field(completed[1], "user.id") == str(user)
    assert field(completed[1], "http.route") == "/boom/{item_id}"
    assert "secret" not in json.dumps(found, ensure_ascii=False)
    assert failed.json()["trace_id"] == field(completed[1], "trace.id")
    assert field(completed[2], "tags") == ["health"]
    assert (completed[3]["log"]["level"], field(completed[3], "error.type")) == (
        "WARN",
        "HTTPException",
    )
    assert field(completed[3], "http.route") is None
    assert field(completed[3], "url.path") == "/nope"
    assert lines_with(found, "health check requested")[0]["tags"] == ["health"]
    # One server span per request, with the stable HTTP names and no inner ASGI spans
    servers = [span for span in spans() if span.kind is SpanKind.SERVER]
    assert len(servers) == len(spans()) == 4
    crashed = next(
        s for s in servers if s.attributes.get("http.route") == "/boom/{item_id}"
    )
    assert crashed.name == "GET /boom/{item_id}"
    assert {
        name: crashed.attributes.get(name)
        for name in ("http.request.method", "url.path", "http.response.status_code")
    } == {
        "http.request.method": "GET",
        "url.path": "/boom/7",
        "http.response.status_code": 500,
    }
    assert "http.method" not in crashed.attributes
    assert crashed.status.status_code is StatusCode.ERROR
    assert line_ids(received[1]) == line_ids(completed[1]) == ids(crashed)


# ---------------------------------------------------------------- other processes


def test_kafka_client_lines_go_through_the_json_handler(lines):
    logger = consumer_config(SummarizerSettings(base=Settings()))["logger"]
    logger.error("%s [%s] %s", "FAIL", "rdkafka#consumer-1", "Connect failed")

    [line] = lines()
    assert line["log"] == {"level": "ERROR", "logger": "librdkafka"}
    assert line["message"] == "FAIL [rdkafka#consumer-1] Connect failed"
    assert field(line, "error.stack_trace").startswith("log site: librdkafka\n")


def test_piped_lines_become_json_lines_with_their_level(lines):
    forward(
        "flyway",
        [
            "Flyway OSS Edition 12.11.0 by Redgate\n",
            "\n",
            "WARNING: Connection error: 100% retry\n",
            "ERROR: Unable to obtain connection from database\r\n",
            'Successfully applied 1 migration to schema "public"\n',
        ],
    )

    found = lines()
    assert [(line["log"]["level"], line["message"]) for line in found] == [
        ("INFO", "Flyway OSS Edition 12.11.0 by Redgate"),
        ("WARN", "WARNING: Connection error: 100% retry"),
        ("ERROR", "ERROR: Unable to obtain connection from database"),
        ("INFO", 'Successfully applied 1 migration to schema "public"'),
    ]
    for line in found:
        assert line["log"]["logger"] == "flyway"
        assert line["ecs"] == {"version": "8.11"}
        assert "origin" not in line["log"]


def test_the_pipe_module_runs_as_a_command():
    done = subprocess.run(
        [sys.executable, "-m", "app.logs.pipe", "flyway", "ai-chat-api"],
        input="Database: jdbc:postgresql://db:5432/ai_chat (PostgreSQL 18.6)\n",
        capture_output=True,
        text=True,
        cwd=APP.parent,
        check=True,
    )

    [line] = log_lines(done.stdout)
    assert line["service"]["name"] == "ai-chat-api"
    assert line["log"] == {"level": "INFO", "logger": "flyway"}
    assert line["message"].startswith("Database: jdbc:postgresql://db:5432/")
    assert done.stderr == ""


# ---------------------------------------------------------------- metrics

KAFKA = {
    "messaging.system": "kafka",
    "messaging.destination.name": "content.project.changed.v2",
    "messaging.consumer.group.name": "ai-chat-summarizer",
}


def points(metric) -> dict[frozenset, object]:
    """A metric's data points by their attributes."""
    return {
        frozenset(point.attributes.items()): point for point in metric.data.data_points
    }


def key(attributes: dict) -> frozenset:
    return frozenset(attributes.items())


class FakePool:
    """What the pool metrics read of psycopg_pool's AsyncConnectionPool: closed and get_stats()."""

    def __init__(self, size, available, waiting, maximum=10, closed=False):
        self.closed = closed
        self.stats = {
            "pool_min": 1,
            "pool_max": maximum,
            "pool_size": size,
            "pool_available": available,
            "requests_waiting": waiting,
            # A counter; get_stats() keeps it (pop_stats() would reset it)
            "requests_num": 7,
        }

    def get_stats(self):
        return dict(self.stats)


def test_pool_metrics_read_the_open_pools_by_name(metrics, monkeypatch):
    monkeypatch.setattr(_metrics, "_pools", {})
    watch_connection_pool("ai-chat-test", FakePool(size=4, available=1, waiting=2))
    # The same name again (the app started again) replaces the old pool
    watch_connection_pool(
        "ai-chat-test", FakePool(size=3, available=1, waiting=1, maximum=5)
    )
    # A closed pool keeps its connection count, so it is left out
    watch_connection_pool(
        "ai-chat-closed", FakePool(size=2, available=0, waiting=0, closed=True)
    )

    found = metrics()
    count = found["db.client.connection.count"]
    maximum = found["db.client.connection.max"]
    pending = found["db.client.connection.pending_requests"]
    pool = {"db.client.connection.pool.name": "ai-chat-test"}
    assert {name: point.value for name, point in points(count).items()} == {
        key({**pool, "db.client.connection.state": "used"}): 2,
        key({**pool, "db.client.connection.state": "idle"}): 1,
    }
    assert {name: point.value for name, point in points(maximum).items()} == {
        key(pool): 5
    }
    assert {name: point.value for name, point in points(pending).items()} == {
        key(pool): 1
    }
    # Up-down counters, as the semantic conventions (and Java HikariCP) have them
    assert [
        (metric.unit, metric.data.is_monotonic) for metric in (count, maximum, pending)
    ] == [
        ("{connection}", False),
        ("{connection}", False),
        ("{request}", False),
    ]


def test_the_event_loop_lag_task_records_how_late_it_woke(
    metrics, lines, spans, monkeypatch
):
    monkeypatch.setattr(_metrics, "LAG_INTERVAL", 0.05)

    async def run():
        # Started inside a request's span; the task carries none of it
        with TRACER.start_as_current_span("request"):
            stop = watch_event_loop()
        # Let the task start sleeping, then hold the loop with blocking code
        await asyncio.sleep(0)
        time.sleep(0.3)
        # The task wakes up late and records it
        await asyncio.sleep(0.1)
        await stop()
        return asyncio.all_tasks()

    left = asyncio.run(run())

    [lag] = metrics()["loresentry.event_loop.lag"].data.data_points
    # Only run() itself is left: stop() cancelled the task and waited for it
    assert len(left) == 1
    assert dict(lag.attributes) == {}
    assert lag.count >= 1 and lag.max >= 0.2 and lag.min >= 0
    assert tuple(lag.explicit_bounds) == (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2.5,
        5,
        10,
    )
    # Not recorded under the request's span (an exemplar would carry it)
    assert lag.exemplars == []
    # No line, no span of its own
    assert lines() == []
    assert [span.name for span in spans()] == ["request"]


def test_a_genai_call_records_its_duration_and_tokens(metrics, spans):
    usage = SimpleNamespace(
        input_tokens=1200,
        output_tokens=90,
        input_tokens_details=SimpleNamespace(cached_tokens=800),
        output_tokens_details=SimpleNamespace(reasoning_tokens=30),
    )
    served = "gpt-6-luna-2026-10-01"
    done = SimpleNamespace(status="completed", usage=usage, model=served)
    cut = SimpleNamespace(
        status="incomplete",
        usage=usage,
        model=served,
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    limited = RateLimitError(
        "rate limited, prompt: 레나",
        response=httpx2.Response(429, request=request),
        body={"error": {"code": "rate_limit_exceeded"}},
    )

    asyncio.run(create(openai(done), model="gpt-6-luna", input="레나"))
    with pytest.raises(ModelFailure):
        asyncio.run(create(openai(cut), model="gpt-6-luna", input="레나"))
    with pytest.raises(ModelFailure):
        asyncio.run(create(openai(error=limited), model="gpt-6-luna", input="레나"))
    # A call to a target that is not generative AI records no GenAI metrics
    with LogFlow.call("content") as call:
        call.status(200)

    found = metrics()
    duration = found["gen_ai.client.operation.duration"]
    tokens = found["gen_ai.client.token.usage"]
    request_attributes = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-6-luna",
    }
    answered = {**request_attributes, "gen_ai.response.model": served}
    incomplete = {**answered, "error.type": "MODEL_INCOMPLETE"}
    # Every call has its duration, failures with their error.type
    timed = points(duration)
    assert set(timed) == {
        key(answered),
        key(incomplete),
        key({**request_attributes, "error.type": "MODEL_RATE_LIMITED"}),
    }
    assert all(point.count == 1 and point.sum > 0 for point in timed.values())
    # Tokens of the calls that got a usage back, input and output apart
    assert {
        name: (point.count, point.sum) for name, point in points(tokens).items()
    } == {
        key({**answered, "gen_ai.token.type": "input"}): (1, 1200),
        key({**answered, "gen_ai.token.type": "output"}): (1, 90),
        key({**incomplete, "gen_ai.token.type": "input"}): (1, 1200),
        key({**incomplete, "gen_ai.token.type": "output"}): (1, 90),
    }
    assert (duration.unit, tokens.unit) == ("s", "{token}")
    assert tuple(timed[key(answered)].explicit_bounds) == (
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
    used = points(tokens)[key({**answered, "gen_ai.token.type": "input"})]
    assert tuple(used.explicit_bounds) == (
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
    # Recorded inside the call span, before it closes (the exemplar carries it)
    answered_span = spans()[0]
    assert answered_span.name == "openai call"
    [exemplar] = timed[key(answered)].exemplars
    assert exemplar.span_id == answered_span.context.span_id
    # No prompt or answer text
    assert "레나" not in repr(
        [point.attributes for point in [*timed.values(), *points(tokens).values()]]
    )


def test_a_kafka_record_records_its_delay_and_processing_time(metrics):
    def record(timestamp=None, raises=False):
        with (
            suppress(KeyError),
            LogFlow.kafka_record(
                "content.project.changed.v2",
                0,
                1,
                [],
                {"FileChanged"},
                "project changed",
                "ai-chat-summarizer",
                timestamp,
            ),
        ):
            if raises:
                raise KeyError("x")

    now_ms = int(time.time() * 1000)
    # Created five seconds ago
    record((1, now_ms - 5000))
    # A clock ahead of ours (log append time here) is no delay, not a negative one
    record((2, now_ms + 60_000), raises=True)
    # Without a timestamp there is no delay
    record((0, -1))
    record()

    found = metrics()
    delay_metric = found["loresentry.kafka.consume.delay"]
    process_metric = found["messaging.process.duration"]
    [delay] = delay_metric.data.data_points
    assert dict(delay.attributes) == KAFKA
    assert delay.count == 2 and delay.min == 0 and 5 <= delay.max < 6
    assert tuple(delay.explicit_bounds) == (
        0.01,
        0.05,
        0.1,
        0.5,
        1,
        5,
        10,
        30,
        60,
        300,
        900,
        3600,
    )
    processed = points(process_metric)
    done = {**KAFKA, "messaging.operation.name": "process"}
    assert {name: point.count for name, point in processed.items()} == {
        key(done): 3,
        key({**done, "error.type": "KeyError"}): 1,
    }
    assert tuple(processed[key(done)].explicit_bounds) == (
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
    assert {metric.unit for metric in (delay_metric, process_metric)} == {"s"}


def test_the_db_bucket_swap_leaves_a_changed_instrumentation_alone(monkeypatch):
    # The buckets themselves are checked against PostgreSQL in test_chat_api and test_summarizer.
    # An instrumentation without the name must not stop the app from starting
    monkeypatch.delattr(dbapi, "create_db_client_operation_duration")

    _metrics._give_db_duration_buckets()

    assert not hasattr(dbapi, "create_db_client_operation_duration")


# ---------------------------------------------------------------- tracing engine (image entrypoint)

# Run under the image's entrypoint and the real distro: which calls carry a traceparent, what is on
DISTRO_CHECK = """
import asyncio, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from openai import AsyncOpenAI
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import (
    HTTPX2ClientInstrumentor,
    HTTPXClientInstrumentor,
)
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

seen = {}


class Handler(BaseHTTPRequestHandler):
    def answer(self):
        seen[self.path] = [name.lower() for name in self.headers]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = answer

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"


async def calls():
    with trace.get_tracer("check").start_as_current_span("request"):
        async with httpx.AsyncClient(base_url=base) as content:
            await content.get("/internal/projects")
        client = AsyncOpenAI(api_key="test", base_url=base + "/v1", max_retries=0)
        try:
            await client.responses.create(model="test", input="test")
        except Exception:
            pass
        await client.close()


asyncio.run(calls())
provider = trace.get_tracer_provider()
instrumentors = {
    "httpx": HTTPXClientInstrumentor,
    "httpx2": HTTPX2ClientInstrumentor,
    "psycopg": PsycopgInstrumentor,
    "botocore": BotocoreInstrumentor,
    "fastapi": FastAPIInstrumentor,
    "system_metrics": SystemMetricsInstrumentor,
}
print(json.dumps({
    "content": seen["/internal/projects"],
    "openai": seen["/v1/responses"],
    "sampler": provider.sampler.get_description(),
    "meter_provider": type(metrics.get_meter_provider()).__module__,
    "resource": {
        name: provider.resource.attributes.get(name)
        for name in ("service.name", "service.version", "deployment.environment.name")
    },
    "instrumented": {
        name: instrumentor().is_instrumented_by_opentelemetry
        for name, instrumentor in instrumentors.items()
    },
}))
"""


def entrypoint_env(path: str, **values: str) -> dict[str, str]:
    """The image's environment: no OTEL_* from this shell, the image tag as APP_VERSION."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OTEL_", "ELASTIC_OTEL_"))
    }
    env.update(PATH=path, APP_VERSION="build-9-1", **values)
    return env


def test_the_entrypoint_sets_safe_defaults_and_the_process_name(tmp_path):
    # Stand-ins for flyway and opentelemetry-instrument; the latter prints what the process would get
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "flyway").write_text("#!/bin/sh\necho 'Flyway OSS Edition stub'\n")
    (stubs / "opentelemetry-instrument").write_text(
        '#!/bin/sh\nenv | grep "^OTEL_"\necho "COMMAND=$*"\n'
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    path = f"{stubs}:{Path(sys.executable).parent}:{os.environ['PATH']}"

    def run(*command: str, **values: str) -> list[str]:
        done = subprocess.run(
            ["bash", str(ENTRYPOINT), *command],
            env=entrypoint_env(path, **values),
            cwd=APP.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.splitlines()

    api = run("uvicorn", "app.main:app", "--no-access-log")
    summarizer = run("python", "-m", "app.summarizer")
    dev = run(
        "python",
        "-m",
        "app.summarizer",
        OTEL_TRACES_EXPORTER="otlp",
        OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=dev",
    )

    # The API migrates first (its output as an ECS JSON line), then runs under the distro
    [flyway] = log_lines("\n".join(api))
    assert (flyway["log"]["logger"], flyway["service"]["name"]) == (
        "flyway",
        "ai-chat-api",
    )
    assert "COMMAND=uvicorn app.main:app --no-access-log" in api
    assert {
        "OTEL_SERVICE_NAME=ai-chat-api",
        "OTEL_TRACES_EXPORTER=none",
        "OTEL_METRICS_EXPORTER=none",
        "OTEL_LOGS_EXPORTER=none",
        "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",
        "OTEL_TRACES_SAMPLER=parentbased_always_on",
        "OTEL_PROPAGATORS=tracecontext",
        "OTEL_SEMCONV_STABILITY_OPT_IN=http,database",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false",
        # system_metrics stays on: process.runtime.cpython.* and cpython.gc.*
        "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi,httpx2,logging,confluent_kafka",
        # No cloud detectors: aws_eks prints a plain 403 line on EKS
        "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS="
        "process_runtime,os,telemetry_distro,service_instance,containerid,otel",
        "OTEL_RESOURCE_ATTRIBUTES=service.version=build-9-1",
    } <= set(api)
    # The summarizer does not migrate and keeps its own name
    assert log_lines("\n".join(summarizer)) == []
    assert "OTEL_SERVICE_NAME=ai-chat-summarizer" in summarizer
    assert "COMMAND=python -m app.summarizer" in summarizer
    # What the overlay sets wins; the image tag goes in front of its resource attributes
    assert "OTEL_TRACES_EXPORTER=otlp" in dev
    assert (
        "OTEL_RESOURCE_ATTRIBUTES="
        "service.version=build-9-1,deployment.environment.name=dev"
    ) in dev


def test_under_the_distro_only_content_calls_carry_a_traceparent():
    done = subprocess.run(
        ["bash", str(ENTRYPOINT), sys.executable, "-c", DISTRO_CHECK],
        env=entrypoint_env(
            f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=dev",
        ),
        cwd=APP.parent,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    found = json.loads(done.stdout.splitlines()[-1])
    # content gets the traceparent and nothing else of ours; OpenAI (httpx2) gets neither
    assert "traceparent" in found["content"]
    assert not {"traceparent", "tracestate", "baggage"} & set(found["openai"])
    assert "tracestate" not in found["content"]
    assert found["sampler"].startswith("ParentBased{root:AlwaysOnSampler")
    # The app metrics record into the distro's SDK MeterProvider (exported only when an overlay turns it on)
    assert found["meter_provider"].startswith("opentelemetry.sdk.metrics")
    assert found["resource"] == {
        "service.name": "ai-chat-api",
        "service.version": "build-9-1",
        "deployment.environment.name": "dev",
    }
    assert found["instrumented"] == {
        "httpx": True,
        "httpx2": False,
        "psycopg": True,
        "botocore": True,
        "fastapi": False,
        "system_metrics": True,
    }
    # The distro itself writes no plain lines
    assert done.stderr == ""


# ---------------------------------------------------------------- enforcement


def modules():
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(APP)
        if relative.parts[0] == "logs":
            continue
        name = ".".join(("app", *relative.with_suffix("").parts))
        yield path, name, ast.parse(path.read_text(), str(path))


def declared_layer(name: str, tree: ast.Module) -> str:
    names: dict[str, object] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "LOG_LAYER"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            names["LOG_LAYER"] = node.value.value
    return layer_of(name, names)


def own_nodes(function: ast.AST):
    """The nodes of a function body, without nested functions and classes."""
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.ClassDef):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def logger_call(node: ast.AST) -> str | None:
    """'info', 'warn' or 'error' for AppLogger.current().<level>(...), else None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    target = node.func.value
    if (
        node.func.attr in LEVELS
        and isinstance(target, ast.Call)
        and isinstance(target.func, ast.Attribute)
        and target.func.attr == "current"
        and isinstance(target.func.value, ast.Name)
        and target.func.value.id == "AppLogger"
    ):
        return node.func.attr
    return None


def opens_entry_flow(node: ast.AST) -> bool:
    if not isinstance(node, ast.With | ast.AsyncWith):
        return False
    for item in node.items:
        call = item.context_expr
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "LogFlow"
            and call.func.attr in ENTRY_FLOWS
        ):
            return True
    return False


def skipped(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in function.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "skip_entry_log"
        ):
            [reason] = decorator.args
            assert isinstance(reason.value, str) and reason.value.strip()
            return True
    return False


def is_property(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in function.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "property":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "setter":
            return True
    return False


def is_stub(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return all(
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        for node in function.body
    )


def public_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.name, node
        if isinstance(node, ast.ClassDef):
            if any(
                isinstance(base, ast.Name) and base.id == "Protocol"
                for base in node.bases
            ):
                continue
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield f"{node.name}.{member.name}", member


def test_every_public_layer_function_leaves_an_entry_line():
    missing = []
    checked = 0
    for _path, name, tree in modules():
        if declared_layer(name, tree) not in LAYERS:
            continue
        for qualified, function in public_functions(tree):
            if function.name.startswith("_") or is_property(function):
                continue
            if is_stub(function) or skipped(function):
                continue
            checked += 1
            if not any(
                logger_call(node) or opens_entry_flow(node)
                for node in own_nodes(function)
            ):
                missing.append(f"{name}.{qualified}")

    assert checked > 80
    assert missing == []


def test_messages_are_fixed_lowercase_text_and_warnings_have_an_error_type():
    wrong = []
    for _path, name, tree in modules():
        for node in ast.walk(tree):
            level = logger_call(node)
            if level is None:
                continue
            message = node.args[0] if node.args else None
            if not (
                isinstance(message, ast.Constant)
                and isinstance(message.value, str)
                and re.fullmatch(r"[a-z][a-z0-9 \-]*", message.value)
            ):
                wrong.append(f"{name}:{node.lineno} message")
            if level in {"warn", "error"} and not any(
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "error_type"
                for arg in node.args
            ):
                wrong.append(f"{name}:{node.lineno} error.type")

    assert wrong == []
