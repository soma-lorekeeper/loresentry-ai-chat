import ast
import asyncio
import json
import logging
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import RateLimitError

from app.logs import (
    AppLogger,
    LogField,
    LogFlow,
    LogScope,
    Outcome,
    configure_logging,
    setup_tracing,
    traceparent,
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
TRACE = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE}-b7ad6b7169203331-01"
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
    """Reads the lines written so far; logging and tracing are set up as in the service."""
    configure_logging("ai-chat-test")
    setup_tracing("ai-chat-test")
    capsys.readouterr()
    return lambda: log_lines(capsys.readouterr().out)


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


# ---------------------------------------------------------------- flows


def test_a_task_leaves_start_and_end_lines_even_when_it_raises(lines):
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
    assert field(found[5], "error.stack_trace") is None


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


def test_only_an_idle_polling_round_marks_its_span_for_the_collector_to_drop(lines):
    # 빈 회차의 span에만 표시가 붙어 collector가 그 trace를 버린다. 일이 있던 회차와 실패한 회차는 남는다
    with LogFlow.polling("summary-tick") as idle:
        pass
    with LogFlow.polling("summary-tick") as busy:
        AppLogger.current().info("summary jobs claimed")
    with LogFlow.polling("summary-tick") as failed:
        failed.fail("SUMMARY_FAILED")

    assert idle._span.attributes.get("loresentry.polling.idle") is True
    assert "loresentry.polling.idle" not in busy._span.attributes
    assert "loresentry.polling.idle" not in failed._span.attributes


def test_spans_are_sent_only_when_the_collector_address_is_set(monkeypatch):
    # setup_tracing이 provider를 새로 만들게 하고(이미 SDK provider가 있으면 건너뛴다), 만든 provider를 받아 본다
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    made = []
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: None)
    monkeypatch.setattr(otel_trace, "set_tracer_provider", made.append)
    monkeypatch.setenv("APP_VERSION", "build-1-1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup_tracing("ai-chat-test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    setup_tracing("ai-chat-test")

    local, dev = made
    assert local._active_span_processor._span_processors == ()
    assert [type(p) for p in dev._active_span_processor._span_processors] == [
        BatchSpanProcessor
    ]
    assert dev.resource.attributes["service.version"] == "build-1-1"
    for provider in made:
        provider.shutdown()


def test_a_kafka_record_continues_the_header_trace(lines):
    event = uuid4()
    headers = [
        ("ce_type", b"FileChanged"),
        ("ce_id", str(event).encode()),
        ("ce_subject", b"not-a-uuid"),
        ("traceparent", TRACEPARENT.encode()),
    ]
    with LogFlow.kafka_record(
        "content.project.changed.v2", 2, 41, headers, {"FileChanged"}, "project changed"
    ) as flow:
        stored = traceparent()
        logging.getLogger("psycopg").warning("framework line")
        flow.outcome(Outcome.SKIPPED)
    with (
        pytest.raises(KeyError),
        LogFlow.kafka_record(
            "t", 0, 1, [("ce_type", b"Other")], {"FileChanged"}, "project changed"
        ),
    ):
        raise KeyError("x")

    found = lines()
    received, framework, processed, _, failed = found
    assert stored.startswith(f"00-{TRACE}-") and len(stored) == 55
    for line in (received, framework, processed):
        assert field(line, "trace.id") == TRACE
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


def test_a_stored_scope_logs_under_the_stored_trace(lines):
    document = uuid4()
    with LogScope.stored(TRACEPARENT, LogField.document_id(document)):
        AppLogger.current().info("document summary previous get")
    with LogScope.stored(None):
        AppLogger.current().info("document summary previous get")

    stored, unstored = lines()
    assert field(stored, "trace.id") == TRACE
    assert field(stored, "span.id") == "b7ad6b7169203331"
    assert field(stored, "loresentry.document_id") == str(document)
    assert field(unstored, "trace.id") is None


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
    return SimpleNamespace(responses=FakeResponses(response, error))


def test_openai_call_lines_carry_model_effort_and_tokens(lines):
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


def test_kms_call_lines_carry_the_kms_code_on_failure(lines):
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


# ---------------------------------------------------------------- requests


def test_request_lines_carry_the_route_user_and_outcome(lines):
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


# ---------------------------------------------------------------- other processes


def test_kafka_client_lines_go_through_the_json_handler(lines):
    logger = consumer_config(SummarizerSettings(base=Settings()))["logger"]
    logger.error("%s [%s] %s", "FAIL", "rdkafka#consumer-1", "Connect failed")

    [line] = lines()
    assert line["log"] == {"level": "ERROR", "logger": "librdkafka"}
    assert line["message"] == "FAIL [rdkafka#consumer-1] Connect failed"


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
