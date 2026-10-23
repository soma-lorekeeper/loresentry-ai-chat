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


def changes_of(messages) -> tuple[list[Change], int]:
    changes: list[Change] = []
    skipped = 0
    for message in messages:
        error = message.error()
        if error is not None:
            if error.code() == KafkaError._PARTITION_EOF:
                continue
            raise KafkaException(error)
        headers: dict[str, str] = {}
        try:
            headers = header_map(message.headers())
            change = parse(headers, message.value())
        except (ValueError, KeyError, TypeError) as reason:
            # A summary is a cache the document's next event rebuilds. One event off
            # the contract is logged without its text and passed over, instead of
            # stopping every project's summaries on the same offset.
            skipped += 1
            log.error(
                "event skipped",
                extra={
                    "topic": message.topic(),
                    "partition": message.partition(),
                    "offset": message.offset(),
                    "ce_type": headers.get("ce_type"),
                    "reason": repr(reason),
                },
            )
            continue
        if change is not None:
            changes.append(change)
    return changes, skipped


def consume(settings: SummarizerSettings, stop: threading.Event) -> None:
    consumer = Consumer(consumer_config(settings))
    consumer.subscribe([settings.topic])
    try:
        with psycopg.connect(settings.base.conninfo) as conn:
            while not stop.is_set():
                messages = consumer.consume(num_messages=BATCH, timeout=1.0)
                if not messages:
                    continue
                changes, skipped = changes_of(messages)
                if changes:
                    apply(conn, changes, settings.prompt_version)
                for message in messages:
                    if message.error() is None:
                        consumer.store_offsets(message)
                consumer.commit(asynchronous=False)
                log.info(
                    "changes applied",
                    extra={
                        "messages": len(messages),
                        "changes": len(changes),
                        "skipped": skipped,
                    },
                )
    finally:
        consumer.close()
