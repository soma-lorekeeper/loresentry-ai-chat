import logging
import threading

import psycopg
from confluent_kafka import Consumer, KafkaError, KafkaException

from app.summarizer.events import Change, header_map, parse
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import apply

log = logging.getLogger("ai_chat.summarizer")

BATCH = 50
MESSAGE_BYTES = 4 * 1024 * 1024


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
    }


def changes_of(messages) -> list[Change]:
    changes: list[Change] = []
    for message in messages:
        error = message.error()
        if error is not None:
            if error.code() == KafkaError._PARTITION_EOF:
                continue
            raise KafkaException(error)
        change = parse(header_map(message.headers()), message.value())
        if change is not None:
            changes.append(change)
    return changes


def consume(settings: SummarizerSettings, stop: threading.Event) -> None:
    consumer = Consumer(consumer_config(settings))
    consumer.subscribe([settings.topic])
    try:
        with psycopg.connect(settings.base.conninfo) as conn:
            while not stop.is_set():
                messages = consumer.consume(num_messages=BATCH, timeout=1.0)
                if not messages:
                    continue
                changes = changes_of(messages)
                if changes:
                    apply(conn, changes, settings.prompt_version)
                for message in messages:
                    if message.error() is None:
                        consumer.store_offsets(message)
                consumer.commit(asynchronous=False)
                log.info(
                    "changes applied",
                    extra={"messages": len(messages), "changes": len(changes)},
                )
    finally:
        consumer.close()
