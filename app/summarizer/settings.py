import os
from dataclasses import dataclass

from app.settings import Settings, _float, _int

TOPICS = ("content.project.changed.v1", "content.project.changed.v2")


def _topics() -> tuple[str, ...]:
    raw = os.getenv("SUMMARY_TOPICS") or os.getenv("SUMMARY_TOPIC")
    if not raw:
        return TOPICS
    return tuple(topic.strip() for topic in raw.split(",") if topic.strip())


@dataclass(frozen=True)
class SummarizerSettings:
    base: Settings
    kafka_bootstrap_servers: str = "localhost:9092"
    topics: tuple[str, ...] = TOPICS
    group_id: str = "ai-chat-summarizer"
    openai_api_key: str = ""
    model: str = "gpt-6-luna"
    reasoning_effort: str = "low"
    max_output_tokens: int = 1000
    prompt_version: str = "summary-v1"
    idle_seconds: float = 600.0
    tick_seconds: float = 60.0
    max_per_tick: int = 30
    workers: int = 2
    lease_seconds: int = 600
    short_chars: int = 500
    min_change_chars: int = 1000
    min_change_ratio: float = 0.3
    max_attempts: int = 3
    heartbeat_path: str = "/tmp/summarizer-heartbeat"
    manuscript_key_id: str = ""
    manuscript_key_region: str = "ap-northeast-2"

    @classmethod
    def from_environment(cls) -> "SummarizerSettings":
        base = Settings.from_environment()
        return cls(
            base=base,
            kafka_bootstrap_servers=os.getenv(
                "KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers
            ),
            topics=_topics(),
            group_id=os.getenv("SUMMARY_GROUP_ID", cls.group_id),
            openai_api_key=os.getenv("SUMMARY_OPENAI_API_KEY") or base.openai_api_key,
            model=os.getenv("SUMMARY_MODEL") or base.openai_model,
            reasoning_effort=os.getenv(
                "SUMMARY_REASONING_EFFORT", cls.reasoning_effort
            ),
            max_output_tokens=_int("SUMMARY_MAX_OUTPUT_TOKENS", cls.max_output_tokens),
            prompt_version=os.getenv("SUMMARY_PROMPT_VERSION", cls.prompt_version),
            idle_seconds=_float("SUMMARY_IDLE_MINUTES", cls.idle_seconds / 60) * 60,
            tick_seconds=_float("SUMMARY_TICK_SECONDS", cls.tick_seconds),
            max_per_tick=_int("SUMMARY_MAX_PER_MINUTE", cls.max_per_tick),
            workers=_int("SUMMARY_WORKERS", cls.workers),
            short_chars=_int("SUMMARY_SHORT_CHARS", cls.short_chars),
            min_change_chars=_int("SUMMARY_MIN_CHANGE_CHARS", cls.min_change_chars),
            min_change_ratio=_float("SUMMARY_MIN_CHANGE_RATIO", cls.min_change_ratio),
            heartbeat_path=os.getenv("SUMMARY_HEARTBEAT_PATH", cls.heartbeat_path),
            manuscript_key_id=os.getenv("MANUSCRIPT_KEY_ID", cls.manuscript_key_id),
            manuscript_key_region=os.getenv(
                "MANUSCRIPT_KEY_REGION", cls.manuscript_key_region
            ),
        )
