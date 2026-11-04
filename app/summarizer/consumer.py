import threading

import psycopg
from confluent_kafka import Consumer, KafkaError, KafkaException

from app.logs import (
    AppLogger,
    LogField,
    LogFlow,
    Outcome,
    kafka_client_logger,
    skip_entry_log,
    traceparent,
)
from app.summarizer.events import TYPES, header_map, parse
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import apply

BATCH = 50
MESSAGE_BYTES = 6 * 1024 * 1024
# 레코드 하나의 시작·종료 줄 문구: project changed event received, project changed event processed
EVENT = "project changed"


@skip_entry_log("Only builds the consumer configuration")
def consumer_config(settings: SummarizerSettings) -> dict:
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": settings.group_id,
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "auto.offset.reset": "earliest",
        "allow.auto.create.topics": False,
        "group.protocol": "classic",
        "partition.assignment.strategy": "range",
        "max.partition.fetch.bytes": MESSAGE_BYTES,
        "fetch.max.bytes": 2 * MESSAGE_BYTES,
        # librdkafka's own lines go through the ECS JSON handler, not plain stderr
        "logger": kafka_client_logger(),
    }


def handle(conn: psycopg.Connection, message, settings: SummarizerSettings) -> None:
    """레코드 하나를 반영한다. 레코드마다 LogFlow.kafka_record가 흐름 하나(consumer span)를 연다.

    헤더의 traceparent를 이은 trace로 시작·종료 줄을 남기고, 그 trace를 summary_queue 행에 저장해 나중에 하는 요약
    작업도 같은 trace로 남게 한다. 처리하지 않는 종류는 SKIPPED, 계약에 맞지 않는 레코드는 SKIPPED와 error.type(warn)이다.
    consumer group은 span의 messaging 속성이 된다.
    """
    error = message.error()
    if error is not None:
        if error.code() == KafkaError._PARTITION_EOF:
            return
        if error.code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
            AppLogger.current().warn(
                "topic not available",
                LogField.kafka_topic(message.topic()),
                LogField.error_type("UNKNOWN_TOPIC_OR_PART"),
            )
            return
        raise KafkaException(error)
    with LogFlow.kafka_record(
        message.topic(),
        message.partition(),
        message.offset(),
        message.headers(),
        TYPES,
        EVENT,
        settings.group_id,
    ) as flow:
        try:
            change = parse(header_map(message.headers()), message.value())
        except (ValueError, KeyError, TypeError) as reason:
            # A summary is a cache the document's next event rebuilds. One event off
            # the contract is logged without its text and passed over, instead of
            # stopping every project's summaries on the same offset.
            flow.outcome(Outcome.SKIPPED).fail(type(reason).__name__)
            return
        if change is None:
            flow.outcome(Outcome.SKIPPED)
            return
        flow.add(
            LogField.document_id(getattr(change, "document_id", None)),
            LogField.project_id(getattr(change, "project_id", None)),
        )
        apply(conn, [change], settings.prompt_version, traceparent())


def consume(settings: SummarizerSettings, stop: threading.Event) -> None:
    AppLogger.current().info(
        "summary consumer start", LogField.param("topics", list(settings.topics))
    )
    consumer = Consumer(consumer_config(settings))
    consumer.subscribe(list(settings.topics))
    try:
        with psycopg.connect(settings.base.conninfo) as conn:
            while not stop.is_set():
                messages = consumer.consume(num_messages=BATCH, timeout=1.0)
                if not messages:
                    continue
                for message in messages:
                    handle(conn, message, settings)
                    if message.error() is None:
                        consumer.store_offsets(message)
                consumer.commit(asynchronous=False)
    finally:
        consumer.close()
